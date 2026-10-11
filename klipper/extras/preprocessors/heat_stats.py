# Heat Stats
# Measures per tool how long the printer stands still waiting for the nozzle
# temperature at tool changes, and suggests a slicer preheat time and standby
# temperature at the end of the print.
#
# Both parts are configured in [gcode_preprocessor heat_stats]:
# - HeatStats (processor, runs at upload): inserts _KTC_HEAT_MARK lines into
#   the G-code: RESET at the start, PREHEAT before the slicer's preheat M104,
#   BEGIN before each tool change, END after the heat wait that follows it and
#   REPORT at the end of the file.
# - HeatStatsRuntime (Klipper, runs during the print): created by the Klipper
#   module for that section. It samples the heater of each tracked tool and
#   the toolhead, and stores one record per tool change.

import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    # Loaded by Klipper as extras.preprocessors.heat_stats
    from ..gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities,
        ProcessorConfig,
    )
except ImportError:
    # Loaded standalone (Moonraker script) with the extras dir on sys.path
    from gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities,
        ProcessorConfig,
    )


MARK = '_KTC_HEAT_MARK'

# Preheat line written by the slicer some seconds before a tool change, e.g.
# PrusaSlicer fork BOSS: "M104 S225 T1 ; preheat T1 time: 15s"
PREHEAT_LINE = re.compile(
    r'^M104\b(?=[^;]*\bT(\d+))(?=[^;]*\bS([\d.]+))[^;]*;\s*preheat\b(?:.*?time:\s*([\d.]+)\s*s)?',
    re.IGNORECASE)
# Commands that block until a nozzle temperature is reached
HEAT_WAIT = re.compile(r'^(?:M109|M116|TEMPERATURE_WAIT|SET_TEMPERATURE_WITH_DEADBAND)\b',
                       re.IGNORECASE)
MOTION_COMMANDS = ('G0', 'G1', 'G2', 'G3')


def _motion_params(command: str) -> Optional[Dict[str, str]]:
    """Return the axis words of a G0-G3 command, None for other commands"""
    words = command.upper().split()
    if not words or words[0] not in MOTION_COMMANDS:
        return None
    return {w[0]: w[1:] for w in words[1:] if w[0] in 'XYZE'}


def _positive_extrusion(params: Dict[str, str]) -> bool:
    try:
        return float(params.get('E', '0') or 0) > 0
    except ValueError:
        return False


class HeatStats(GcodePreprocessorPlugin):
    """
    Inserts the markers that HeatStatsRuntime needs to measure heat waits.
    Tool changes before the first extrusion (initial tool selection) are not
    measured.
    """

    def __init__(self, config, logger):
        super().__init__(config, logger)
        self.insert_report = config.getboolean('insert_report', True)
        self._reset_state()

    def _reset_state(self):
        # input line index -> (original line, marks)
        self.before: Dict[int, Tuple[str, List[str]]] = {}
        self.after: Dict[int, Tuple[str, List[str]]] = {}
        self.changes = 0
        self.preheats = 0

    def get_name(self) -> str:
        return "heat_stats"

    def get_description(self) -> str:
        return (f"Inserts {MARK} lines at tool changes; Klipper measures the heat wait "
                f"per tool (KTC_HEAT_STATS)")

    def _add(self, table, index: int, line: str, mark: str):
        table.setdefault(index, (line, []))[1].append(mark)

    def _close_change(self, pending: Dict[str, Any], index: int, line: str):
        """Place END after the last heat wait, else before the first move"""
        mark = f"{MARK} EVENT=END T={pending['tool']}"
        if pending['wait'] is not None:
            self._add(self.after, *pending['wait'], mark)
        elif pending['motion'] is not None:
            self._add(self.before, *pending['motion'], mark)
        else:
            self._add(self.before, index, line, mark)

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        self._reset_state()
        pending: Optional[Dict[str, Any]] = None
        printing = False
        first: Optional[Tuple[int, str]] = None
        last: Optional[Tuple[int, str]] = None

        for index, raw in enumerate(PreprocessorUtilities.iter_file_lines(file_path)):
            last = (index, raw)
            line = raw.strip()
            if not line or line.startswith(';'):
                continue
            if first is None:
                first = (index, raw)

            preheat = PREHEAT_LINE.match(line)
            if preheat:
                mark = f"{MARK} EVENT=PREHEAT T={int(preheat.group(1))} TARGET={float(preheat.group(2)):g}"
                if preheat.group(3):
                    mark += f" LEAD={float(preheat.group(3)):g}"
                self._add(self.before, index, raw, mark)
                self.preheats += 1
                continue

            command, _ = GcodePatterns.strip_comment(line)
            tool = GcodePatterns.extract_tool_number(command)
            if tool is not None:
                if pending is not None:
                    self._close_change(pending, index, raw)
                    pending = None
                if printing:
                    self._add(self.before, index, raw, f"{MARK} EVENT=BEGIN T={tool}")
                    pending = {'tool': tool, 'wait': None, 'motion': None}
                    self.changes += 1
                continue

            params = _motion_params(command)
            if pending is not None:
                if HEAT_WAIT.match(command):
                    pending['wait'] = (index, raw)
                elif params and pending['motion'] is None:
                    pending['motion'] = (index, raw)
            if params is not None and _positive_extrusion(params) and ('X' in params or 'Y' in params):
                printing = True
                if pending is not None:
                    self._close_change(pending, index, raw)
                    pending = None

        if pending is not None and last is not None:
            self._close_change(pending, *last)

        if self.changes == 0 or first is None or last is None:
            self._reset_state()
            self.logger.info("heat_stats: No tool changes during the print - no markers inserted")
            return True

        self.before.setdefault(first[0], (first[1], []))[1].insert(0, f"{MARK} EVENT=RESET")
        if self.insert_report:
            self._add(self.after, *last, f"{MARK} EVENT=REPORT")
        context.set_metadata('heat_stats_changes', self.changes)
        self.logger.info(f"heat_stats: Marking {self.changes} tool changes "
                         f"({self.preheats} slicer preheat lines)")
        return True

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        index = context.current_line
        out = [line]
        # Compare with the original line, so lines inserted at the same index
        # by an earlier processor do not get the marks
        entry = self.before.get(index)
        if entry is not None and line == entry[0]:
            out = [mark + '\n' for mark in entry[1]] + out
            del self.before[index]
        entry = self.after.get(index)
        if entry is not None and line == entry[0]:
            if not line.endswith('\n'):
                out[-1] = line + '\n'
            out += [mark + '\n' for mark in entry[1]]
            del self.after[index]
        return out

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        missed = len(self.before) + len(self.after)
        if missed:
            self.logger.warning(f"heat_stats: {missed} marker position(s) not found - "
                                f"another processor changed those lines")
        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return HeatStats(config, logger)


# ---------------------------------------------------------------------------
# Evaluation (pure functions, used by the runtime and the tests)
# ---------------------------------------------------------------------------

def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(math.ceil(pct / 100. * len(ordered))))
    return ordered[rank - 1]


def summarize(records: List[Dict[str, Any]], cancelled: Optional[Dict[int, int]] = None,
              margin: float = 3., safety_factor: float = .9,
              wait_threshold: float = .5, max_preheat: float = 120.) -> Dict[str, Any]:
    """
    Evaluate the change records of one print.

    A record holds (seconds relative to BEGIN where noted):
      tool, wait (standstill while the nozzle was off target), standstill,
      duration, target, begin_temp, lead (slicer preheat time or None),
      real_lead (seconds from PREHEAT to BEGIN or None), heat_start (rel.),
      heat_start_temp, heat_reached (rel., None if not reached by END)
    """
    cancelled = cancelled or {}
    tools: Dict[int, Dict[str, Any]] = {}
    by_tool: Dict[int, List[Dict[str, Any]]] = {}
    for record in records:
        by_tool.setdefault(record['tool'], []).append(record)

    for tool, recs in sorted(by_tool.items()):
        waits = [r['wait'] for r in recs]
        # Changes whose slicer preheat was not undone: only these say
        # something about preheat_time and the standby temperature
        lead_waits, rates, available, slack, ratios, leads, targets = [], [], [], [], [], [], []
        for r in recs:
            if r.get('target'):
                targets.append(r['target'])
            start, start_temp, reached = r.get('heat_start'), r.get('heat_start_temp'), r.get('heat_reached')
            heat_time = None
            if start is not None and reached is not None:
                heat_time = reached - start
                delta = (r.get('target') or 0) - (start_temp if start_temp is not None else 0)
                if delta >= 10. and heat_time >= .5:
                    rates.append(delta / heat_time)
            if not r.get('lead'):
                continue
            leads.append(r['lead'])
            lead_waits.append(r['wait'])
            if r.get('real_lead') is not None and r['real_lead'] > 0:
                ratios.append(r['real_lead'] / r['lead'])
            if r['wait'] > wait_threshold and heat_time is not None:
                # Seconds the heater had from the preheat until the tool was needed
                available.append(max(0., heat_time - r['wait']))
            elif r['wait'] <= wait_threshold and reached is not None and reached < 0:
                # Tool was hot that long before its change started
                slack.append(-reached)

        lead = _median(leads)
        ratio = _median(ratios) or 1.
        rate = _median(rates)
        p90 = _percentile(lead_waits, 90) or 0.
        target = _median(targets)
        summary = {
            'changes': len(recs),
            'wait_total': sum(waits),
            'wait_avg': sum(waits) / len(waits),
            'wait_p90': _percentile(waits, 90),
            'wait_max': max(waits),
            'waits_over_threshold': sum(1 for w in waits if w > wait_threshold),
            'standstill_avg': sum(r['standstill'] for r in recs) / len(recs),
            'heat_rate': rate,
            'target': target,
            'slicer_lead': lead,
            'real_to_slicer': ratio,
            'cancelled_preheats': cancelled.get(tool, 0),
            'suggest_preheat': None,
            'suggest_standby_delta': None,
            'suggest_idle_temp': None,
        }
        if lead is not None:
            if p90 > wait_threshold:
                summary['suggest_preheat'] = min(max_preheat, math.ceil(lead + (p90 + margin) / ratio))
            elif slack and _median(slack) > 2 * margin:
                summary['suggest_preheat'] = max(math.ceil(margin),
                                                 math.ceil(lead - (_median(slack) - margin) / ratio))
        if rate is not None and available and target:
            delta = rate * _median(available) * safety_factor
            delta = math.floor(delta / 5.) * 5.
            summary['suggest_standby_delta'] = -delta
            summary['suggest_idle_temp'] = target - delta
        tools[tool] = summary

    preheats = [s['suggest_preheat'] for s in tools.values() if s['suggest_preheat'] is not None]
    deltas = [s['suggest_standby_delta'] for s in tools.values() if s['suggest_standby_delta'] is not None]
    all_waits = [r['wait'] for r in records]
    return {
        'changes': len(records),
        'wait_total': sum(all_waits),
        'wait_avg': sum(all_waits) / len(all_waits) if all_waits else 0.,
        'tools': tools,
        # One global slicer value: the longest preheat / the smallest standby drop
        'suggest_preheat': max(preheats) if preheats else None,
        'suggest_standby_delta': max(deltas) if deltas else None,
        'max_preheat': max_preheat,
    }


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_report(summary: Dict[str, Any]) -> str:
    if not summary['changes']:
        return "KTC heat stats: no tool changes measured"
    lines = [f"KTC heat stats: {summary['changes']} tool changes, heat wait "
             f"{_fmt_duration(summary['wait_total'])} (avg {summary['wait_avg']:.1f} s)"]
    leads, ratios = [], []
    for tool, s in summary['tools'].items():
        text = (f"T{tool}: {s['changes']} changes | wait avg {s['wait_avg']:.1f} s, "
                f"p90 {s['wait_p90']:.1f} s, max {s['wait_max']:.1f} s, "
                f"total {_fmt_duration(s['wait_total'])}")
        if s['heat_rate'] is not None:
            text += f" | heat rate {s['heat_rate']:.1f} °C/s"
        if s['cancelled_preheats']:
            text += f" | preheats undone by cooldown: {s['cancelled_preheats']}"
        lines.append(text)
        if s['slicer_lead'] is not None:
            leads.append(s['slicer_lead'])
            ratios.append(s['real_to_slicer'])

    preheat, delta = summary['suggest_preheat'], summary['suggest_standby_delta']
    lead = _median(leads)
    if preheat is None and delta is None:
        lines.append("No change suggested: no relevant heat waits measured")
        return "\n".join(lines)
    head = "Suggestion"
    if lead is not None:
        head += f" (slicer preheat time now {lead:g} s, real/slicer time {_median(ratios):.2f})"
    lines.append(head + ":")
    if preheat is not None:
        text = f"  A) preheat_time: {preheat:g} s (keep standby temperature)"
        if preheat >= summary['max_preheat']:
            text += f" - limit {summary['max_preheat']:g} s reached, also use B"
        lines.append(text)
    if delta is not None:
        text = "  B) standby_temperature_delta: "
        text += f"{delta:g} °C" if delta < 0 else "0 (keep tools at print temperature)"
        if lead is not None:
            text += f" (keep preheat_time {lead:g} s)"
        idle = [f"T{t} {s['suggest_idle_temp']:g} °C" for t, s in summary['tools'].items()
                if s['suggest_idle_temp'] is not None]
        if idle:
            text += "; per filament idle_temperature: " + ", ".join(idle)
        lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Klipper runtime
# ---------------------------------------------------------------------------

class _ToolTrack:
    """Heater state of one tool between PREHEAT/BEGIN and END"""

    def __init__(self, tool: int, heater, now: float):
        self.tool = tool
        self.heater = heater
        self.last_seen = now
        self.last_target: Optional[float] = None
        # Current heat segment (target raised until temperature reached)
        self.seg_target = 0.
        self.seg_start: Optional[float] = None
        self.seg_start_temp: Optional[float] = None
        self.seg_reached: Optional[float] = None
        # Slicer preheat waiting for its tool change
        self.preheat_at: Optional[float] = None
        self.lead: Optional[float] = None
        # Tool change in progress
        self.begin_at: Optional[float] = None
        self.begin_temp: Optional[float] = None
        self.standstill = 0.
        self.wait = 0.


class HeatStatsRuntime:
    """Klipper side of heat_stats: collects one record per tool change"""

    def __init__(self, config, values: Dict[str, str], name: Optional[str] = None):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = name or config.get_name()
        self.values = dict(values)
        options = ProcessorConfig(self.name, self.values, logging)
        self.interval = max(.05, options.getfloat('sample_interval', .2))
        self.tolerance = options.getfloat('temp_tolerance', 1.)
        self.margin = options.getfloat('margin', 3.)
        self.safety_factor = options.getfloat('safety_factor', .9)
        self.track_timeout = options.getfloat('track_timeout', 900.)
        self.history_file = os.path.expanduser(
            options.get('history_file', '~/printer_data/logs/ktc_heat_stats.jsonl') or '')
        self.tool_heaters = self._parse_tool_heaters(options.get('tool_heaters', ''))

        self.toolhead = None
        self.heaters: Dict[int, Any] = {}
        self.tracks: Dict[int, _ToolTrack] = {}
        self.active: Optional[_ToolTrack] = None
        self.records: List[Dict[str, Any]] = []
        self.cancelled: Dict[int, int] = {}
        self.last_summary: Optional[Dict[str, Any]] = None
        self._summary_cache: Optional[Dict[str, Any]] = None
        self.last_sample: Optional[float] = None
        self.synced_print_time: Optional[float] = None
        self.timer = self.reactor.register_timer(self._sample, self.reactor.NEVER)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("toolhead:sync_print_time", self._handle_sync)
        self.gcode.register_command(MARK, self.cmd_MARK, desc=self.cmd_MARK_help)
        self.gcode.register_command('KTC_HEAT_STATS', self.cmd_KTC_HEAT_STATS,
                                    desc=self.cmd_KTC_HEAT_STATS_help)

    @staticmethod
    def _parse_tool_heaters(value: str) -> Dict[int, str]:
        mapping: Dict[int, str] = {}
        for entry in (value or '').split(','):
            tool_str, sep, heater = entry.strip().partition('=')
            if not sep:
                tool_str, sep, heater = entry.strip().partition(':')
            try:
                tool = int(tool_str.strip().lstrip('Tt'))
            except ValueError:
                continue
            if sep and heater.strip():
                mapping[tool] = heater.strip()
        return mapping

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _handle_sync(self, curtime, est_print_time, print_time):
        # The toolhead was idle and its print_time was moved ahead without a
        # move (Klipper's M109 loop does this every second while waiting)
        self.synced_print_time = print_time

    def _heater_name(self, tool: int) -> str:
        if tool in self.tool_heaters:
            return self.tool_heaters[tool]
        toolchanger = self.printer.lookup_object('toolchanger', None)
        lookup = getattr(toolchanger, 'lookup_tool', None)
        if lookup is not None:
            name = getattr(lookup(tool), 'extruder_name', None)
            if name:
                return name
        return 'extruder' if tool == 0 else f'extruder{tool}'

    def _heater(self, tool: int):
        if tool not in self.heaters:
            name = self._heater_name(tool)
            try:
                self.heaters[tool] = self.printer.lookup_object('heaters').lookup_heater(name)
            except Exception:
                logging.warning(f"heat_stats: No heater '{name}' for T{tool} (see tool_heaters)")
                self.heaters[tool] = None
        return self.heaters[tool]

    def _track(self, tool: int, now: float) -> Optional[_ToolTrack]:
        track = self.tracks.get(tool)
        if track is None:
            heater = self._heater(tool)
            if heater is None:
                return None
            track = self.tracks[tool] = _ToolTrack(tool, heater, now)
            self._update_track(track, now, 0.)
            if self.last_sample is None:
                self.last_sample = now
                self.reactor.update_timer(self.timer, now + self.interval)
        track.last_seen = now
        return track

    def _update_track(self, track: _ToolTrack, now: float, dt: float, standstill: bool = False):
        temp, target = track.heater.get_temp(now)
        if track.last_target is None or target > track.last_target + .5:
            # Target raised (preheat or tool change): a new heat segment starts
            if target > 0:
                track.seg_target, track.seg_start, track.seg_start_temp = target, now, temp
                track.seg_reached = None
        elif target < track.last_target - .5:
            # Target lowered (e.g. slicer cooldown after the preheat)
            if track.preheat_at is not None and track.begin_at is None:
                self.cancelled[track.tool] = self.cancelled.get(track.tool, 0) + 1
                self._summary_cache = None
                track.preheat_at = track.lead = None
            track.seg_start = track.seg_reached = None
        track.last_target = target
        if (track.seg_start is not None and track.seg_reached is None
                and temp >= track.seg_target - self.tolerance):
            track.seg_reached = now
        if track is self.active and standstill:
            track.standstill += dt
            if target > 0 and (abs(temp - target) > self.tolerance or self._heater_busy(track, now)):
                track.wait += dt

    @staticmethod
    def _heater_busy(track: _ToolTrack, now: float) -> bool:
        # Same condition M109 waits on (PID: +-1 °C and settled slope, MPC: +-1 °C)
        check_busy = getattr(track.heater, 'check_busy', None)
        try:
            return bool(check_busy(now)) if check_busy is not None else False
        except Exception:
            return False

    def _sample(self, eventtime: float) -> float:
        if not self.tracks:
            self.last_sample = None
            return self.reactor.NEVER
        dt = 0.
        if self.last_sample is not None:
            dt = min(max(0., eventtime - self.last_sample), 4. * self.interval)
        self.last_sample = eventtime
        standstill = False
        if self.active is not None and self.toolhead is not None:
            print_time, est_print_time, lookahead_empty = self.toolhead.check_busy(eventtime)
            standstill = lookahead_empty and (est_print_time > print_time
                                              or print_time == self.synced_print_time)
        for tool, track in list(self.tracks.items()):
            if track is not self.active and eventtime - track.last_seen > self.track_timeout:
                del self.tracks[tool]
                continue
            self._update_track(track, eventtime, dt, standstill)
        return eventtime + self.interval

    # Events --------------------------------------------------------------

    def reset(self):
        self.tracks.clear()
        self.active = None
        self.records = []
        self.cancelled = {}
        self._summary_cache = None

    def preheat(self, tool: int, now: float, lead: Optional[float]):
        track = self._track(tool, now)
        if track is not None and track.begin_at is None:
            track.preheat_at, track.lead = now, lead

    def begin(self, tool: int, now: float):
        if self.active is not None and self.active.tool != tool:
            # END of the previous change missing: drop it
            self.tracks.pop(self.active.tool, None)
        track = self._track(tool, now)
        self.active = track
        if track is None:
            return
        track.begin_at = now
        track.begin_temp = track.heater.get_temp(now)[0]
        track.standstill = track.wait = 0.

    def end(self, tool: int, now: float):
        track = self.active
        if track is None or track.tool != tool or track.begin_at is None:
            return
        self._update_track(track, now, 0.)
        rel = lambda t: None if t is None else t - track.begin_at  # noqa: E731
        self.records.append({
            'tool': tool,
            'wait': track.wait,
            'standstill': track.standstill,
            'duration': now - track.begin_at,
            'target': track.last_target,
            'begin_temp': track.begin_temp,
            'lead': track.lead,
            'real_lead': None if track.preheat_at is None else track.begin_at - track.preheat_at,
            'heat_start': rel(track.seg_start),
            'heat_start_temp': track.seg_start_temp if track.seg_start is not None else None,
            'heat_reached': rel(track.seg_reached),
        })
        self.active = None
        self.tracks.pop(tool, None)
        self._summary_cache = None

    def summary(self) -> Dict[str, Any]:
        if self._summary_cache is None:
            self._summary_cache = summarize(self.records, self.cancelled, self.margin,
                                            self.safety_factor)
        return self._summary_cache

    def report(self, save: bool = True) -> str:
        self.last_summary = self.summary()
        text = format_report(self.last_summary)
        if save and self.history_file and self.records:
            self._save_history(self.last_summary)
        return text

    def _save_history(self, summary: Dict[str, Any]):
        filename = None
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is not None:
            filename = print_stats.get_status(self.reactor.monotonic()).get('filename')
        entry = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'file': filename, 'summary': summary}
        try:
            with open(self.history_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError as e:
            logging.warning(f"heat_stats: Could not write {self.history_file}: {e}")

    # G-code commands ------------------------------------------------------

    cmd_MARK_help = "Internal marker inserted by the heat_stats processor"
    def cmd_MARK(self, gcmd):
        event = gcmd.get('EVENT').upper()
        now = self.reactor.monotonic()
        if event == 'RESET':
            self.reset()
        elif event == 'REPORT':
            gcmd.respond_info(self.report())
        elif event == 'PREHEAT':
            lead = gcmd.get_float('LEAD', None)
            self.preheat(gcmd.get_int('T', minval=0), now, lead)
        elif event == 'BEGIN':
            self.begin(gcmd.get_int('T', minval=0), now)
        elif event == 'END':
            self.end(gcmd.get_int('T', minval=0), now)
        else:
            raise gcmd.error(f"{MARK}: unknown EVENT '{event}'")

    cmd_KTC_HEAT_STATS_help = "Show heat wait per tool and the suggested preheat/standby (RESET=1 clears)"
    def cmd_KTC_HEAT_STATS(self, gcmd):
        if gcmd.get_int('RESET', 0):
            self.reset()
            gcmd.respond_info("KTC heat stats cleared")
            return
        gcmd.respond_info(self.report(save=False))

    def get_status(self, eventtime):
        summary = self.summary() if self.records else None
        return {
            'options': dict(self.values),
            'changes': len(self.records),
            'tracking': sorted(self.tracks),
            'summary': summary,
        }
