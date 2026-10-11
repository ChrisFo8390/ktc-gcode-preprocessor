# Heat Wait Statistics Preprocessor
# Marks every tool change in the G-code so that Klipper can measure how long
# the print waits for the new tool's nozzle temperature. At the end of the
# print KTC_HEAT_STATS reports the waiting time per tool and suggests a
# standby temperature and a preheat time.
#
# The processor only inserts _KTC_HEAT_MARK commands. The measurement is done
# by HeatWaitRecorder (below), which the Klipper module gcode_preprocessor.py
# registers; the analysis functions are plain Python so they can be tested.

from typing import Any, Dict, List, Optional, Tuple
import json
import logging
import math
import os
import re
import statistics
import time

try:
    # Loaded by Klipper as extras.preprocessors.heat_wait_stats
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


MARK_COMMAND = '_KTC_HEAT_MARK'
TEMP_COMMAND = re.compile(r'^M10([49])(?:\s|$)', re.IGNORECASE)
MOVE_COMMAND = re.compile(r'^G[0-3](?:\s|$)', re.IGNORECASE)
TOOL_PARAM = re.compile(r'\sT(\d+)', re.IGNORECASE)
TEMP_PARAM = re.compile(r'\sS([-+]?\d*\.?\d+)', re.IGNORECASE)

# The heater counts as "at temperature" within this band (as Klipper's M109)
REACHED_TOLERANCE = 1.0
# Waiting times below this are treated as no wait (timing noise)
MIN_WAIT = 0.5
# Heating slows down close to the target: suggestions only use this share of
# the measured average heating rate
SUGGESTION_MARGIN = 0.85
# Smallest temperature rise that gives a usable heating rate
MIN_RATE_DELTA = 5.0


# ==============================================================================
# Preprocessor: inserts the markers
# ==============================================================================

class HeatWaitStats(GcodePreprocessorPlugin):
    """
    Inserts around every change to another tool:
      _KTC_HEAT_MARK EVENT=PREHEAT TOOL=n  after the slicer's M104 T<n> S<temp>
      _KTC_HEAT_MARK EVENT=CHANGE TOOL=n   before the tool change command
      _KTC_HEAT_MARK EVENT=READY TOOL=n    after the M109 following the change,
                                           or before the first move without M109
    plus EVENT=START before the first and EVENT=END after the last line.
    """

    def __init__(self, config, logger):
        super().__init__(config, logger)
        # Lines after a tool change in which the M109 of the new tool is searched
        self.wait_search_lines = config.getint('wait_search_lines', 50)
        self._reset_state()

    def _reset_state(self):
        """Reset all per-file state (the processor instance is reused)"""
        self.before: Dict[int, List[str]] = {}
        self.after: Dict[int, List[str]] = {}
        self.originals: Dict[int, str] = {}
        self.last_line = -1
        self.changes = 0
        self.preheats = 0
        self.start_done = False
        self.end_done = False

    def get_name(self) -> str:
        return "heat_wait_stats"

    def get_description(self) -> str:
        return "Measures the time spent waiting for nozzle temperature at tool changes (KTC_HEAT_STATS)"

    @staticmethod
    def _mark(event: str, tool: Optional[int] = None) -> str:
        if tool is None:
            return f"{MARK_COMMAND} EVENT={event}\n"
        return f"{MARK_COMMAND} EVENT={event} TOOL={tool}\n"

    def _add(self, table: Dict[int, List[str]], line_num: int, original: str, text: str):
        table.setdefault(line_num, []).append(text)
        self.originals[line_num] = original

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        self._reset_state()

        current_tool: Optional[int] = None
        # tool -> (line number, text) of the last M104 T<tool> S>0 since the last tool change
        preheat_lines: Dict[int, Tuple[int, str]] = {}
        pending: Optional[Tuple[int, int]] = None  # (tool, change line) waiting for its M109
        last_text = ''

        for line_num, line in enumerate(PreprocessorUtilities.iter_file_lines(file_path)):
            self.last_line = line_num
            text = last_text = line.strip()
            if not text or text.startswith(';'):
                continue
            command, _ = GcodePatterns.strip_comment(text)

            tool = GcodePatterns.extract_tool_number(text)
            if tool is not None:
                pending = None
                # Re-selecting the active tool is not a tool change
                if current_tool is not None and tool != current_tool:
                    self.changes += 1
                    if tool in preheat_lines:
                        self.preheats += 1
                        preheat_line, preheat_text = preheat_lines[tool]
                        self._add(self.after, preheat_line, preheat_text, self._mark('PREHEAT', tool))
                    self._add(self.before, line_num, text, self._mark('CHANGE', tool))
                    pending = (tool, line_num)
                current_tool = tool
                preheat_lines = {}
                continue

            temp = TEMP_COMMAND.match(command)
            if temp:
                args = ' ' + command[4:]
                tool_match = TOOL_PARAM.search(args)
                temp_match = TEMP_PARAM.search(args)
                target = float(temp_match.group(1)) if temp_match else 0.0
                tool_param = int(tool_match.group(1)) if tool_match else None
                if temp.group(1) == '4':
                    if tool_param is not None and target > 0:
                        preheat_lines[tool_param] = (line_num, text)
                elif pending is not None and tool_param in (None, pending[0]) and target > 0:
                    self._add(self.after, line_num, text, self._mark('READY', pending[0]))
                    pending = None
                continue

            if pending is not None and (MOVE_COMMAND.match(command)
                                        or line_num - pending[1] > self.wait_search_lines):
                # No M109 for the new tool: printing resumes with this line
                self._add(self.before, line_num, text, self._mark('READY', pending[0]))
                pending = None

        if self.last_line >= 0:
            self.originals.setdefault(self.last_line, last_text)

        self.logger.info(f"heat_wait_stats: Marked {self.changes} tool changes "
                         f"({self.preheats} with slicer preheat)")
        return True

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        line_num = context.current_line
        output = [line]

        if not self.start_done:
            self.start_done = True
            output.insert(0, self._mark('START'))

        # Earlier processors may have added lines for the same input line, so
        # the markers go next to the original line only
        if line_num in self.originals and line.strip() == self.originals[line_num]:
            del self.originals[line_num]
            index = output.index(line)
            if not line.endswith('\n'):
                line += '\n'  # last line of the file, markers follow it
            output[index:index + 1] = (self.before.pop(line_num, []) + [line]
                                       + self.after.pop(line_num, []))
            if line_num == self.last_line and not self.end_done:
                self.end_done = True
                output.append(self._mark('END'))
        return output

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        if self.last_line >= 0 and not self.end_done:
            self.logger.warning("heat_wait_stats: End marker could not be inserted")
        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return HeatWaitStats(config, logger)


# ==============================================================================
# Analysis: plain functions on the recorded samples
# ==============================================================================
#
# A sample is a dict recorded per tool change:
#   tool, target    new tool and its target temperature
#   start_temp      nozzle temperature when heating started (slicer preheat,
#                   or the tool change itself without preheat)
#   lead            seconds from the slicer preheat to the start of the change
#                   (None without preheat)
#   change_time     seconds from the start of the change until printing resumed
#   heat_time       seconds from heating start until the target was reached
#                   (None if it was not reached before printing resumed)
#   reached_after   seconds from the start of the change until the target was
#                   reached (<= 0: hot before the change; None: not reached)

def _median(values: List[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def mechanical_change_time(samples: List[Dict[str, Any]]) -> Optional[float]:
    """Duration of a tool change that did not wait for temperature"""
    hot = [s['change_time'] for s in samples
           if s.get('reached_after') is not None and s['reached_after'] <= 0]
    if hot:
        return _median(hot)
    times = [s['change_time'] for s in samples if s.get('change_time') is not None]
    return min(times) if times else None


def sample_wait(sample: Dict[str, Any], mechanical: Optional[float]) -> float:
    """Seconds the tool change took longer because of heating"""
    reached_after = sample.get('reached_after')
    if mechanical is None or (reached_after is not None and reached_after <= 0):
        return 0.0
    wait = sample['change_time'] - mechanical
    return wait if wait >= MIN_WAIT else 0.0


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step) * step


def _round_down(value: float, step: float) -> float:
    return math.floor(value / step) * step


def analyze(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Waiting times per tool and suggestions for standby temperature and preheat time"""
    mechanical = mechanical_change_time(samples)
    tools: Dict[int, Dict[str, Any]] = {}

    for tool in sorted({s['tool'] for s in samples}):
        tool_samples = [s for s in samples if s['tool'] == tool]
        waits = [sample_wait(s, mechanical) for s in tool_samples]
        rates = [(s['target'] - s['start_temp']) / s['heat_time'] for s in tool_samples
                 if s.get('heat_time') and s['target'] - s['start_temp'] >= MIN_RATE_DELTA]
        deltas = [s['target'] - s['start_temp'] for s in tool_samples if s.get('lead') is not None]
        leads = [s['lead'] for s in tool_samples if s.get('lead') is not None]

        result = {
            'changes': len(tool_samples),
            'waits': sum(1 for w in waits if w > 0),
            'wait_avg': sum(waits) / len(waits),
            'wait_max': max(waits),
            'wait_total': sum(waits),
            'target': _median([s['target'] for s in tool_samples]),
            'rate': _median(rates),
            'standby_delta': _median(deltas),
            'lead': _median(leads),
            'not_reached': sum(1 for s in tool_samples if s.get('reached_after') is None),
        }

        rate, delta = result['rate'], result['standby_delta']
        if rate and delta is not None and mechanical is not None:
            usable_rate = rate * SUGGESTION_MARGIN
            # Heating also continues during the mechanical tool change
            result['preheat_suggestion'] = max(0.0, _round_up(delta / usable_rate - mechanical, 5))
            coverable = usable_rate * ((result['lead'] or 0.0) + mechanical)
            result['standby_suggestion'] = (None if coverable >= delta else
                                            result['target'] - _round_down(coverable, 5))
        tools[tool] = result

    return {
        'changes': len(samples),
        'mechanical': mechanical,
        'wait_total': sum(t['wait_total'] for t in tools.values()),
        'tools': tools,
    }


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def format_report(analysis: Dict[str, Any], title: str) -> str:
    if not analysis['changes']:
        return f"KTC heat stats ({title}): no tool changes recorded"

    lines = [f"KTC heat stats ({title}): {analysis['changes']} tool changes, "
             f"waiting for temperature {format_duration(analysis['wait_total'])}"]
    if analysis['mechanical'] is not None:
        lines.append(f"Tool change without waiting: {analysis['mechanical']:.1f}s")

    for tool, t in analysis['tools'].items():
        lines.append(f"T{tool}: {t['changes']} changes, waited {t['waits']}x - "
                     f"avg {t['wait_avg']:.1f}s, max {t['wait_max']:.1f}s, "
                     f"total {format_duration(t['wait_total'])}")
        details = []
        if t['rate']:
            details.append(f"heats {t['rate']:.1f}C/s")
        if t['standby_delta'] is not None:
            details.append(f"from {t['target'] - t['standby_delta']:.0f}C to {t['target']:.0f}C")
        if t['lead'] is not None:
            details.append(f"slicer preheat {t['lead']:.1f}s before the change")
        if t['not_reached']:
            details.append(f"{t['not_reached']}x printing started below target")
        if details:
            lines.append("    " + ", ".join(details))
        if 'preheat_suggestion' in t:
            suggestion = f"    suggestion: preheat {t['preheat_suggestion']:.0f}s before the tool change"
            if t['standby_suggestion'] is None:
                suggestion += " - current standby temperature causes no wait"
            else:
                suggestion += (f", or with the current preheat a standby of "
                               f"{t['standby_suggestion']:.0f}C "
                               f"(delta -{t['target'] - t['standby_suggestion']:.0f}C)")
            lines.append(suggestion)
        elif t['waits']:
            lines.append("    suggestion: not enough data (needs heating from standby)")
    return "\n".join(lines)


# ==============================================================================
# Klipper side: records the samples (registered by gcode_preprocessor.py)
# ==============================================================================

def parse_tool_heaters(value: Optional[str], logger=logging) -> Dict[int, str]:
    """Parse '0=extruder, 1=extruder1' into {0: 'extruder', 1: 'extruder1'}"""
    mapping: Dict[int, str] = {}
    for entry in (value or '').split(','):
        entry = entry.strip()
        if not entry:
            continue
        tool_str, sep, heater = entry.partition('=')
        if not sep:
            tool_str, sep, heater = entry.partition(':')
        try:
            mapping[int(tool_str.strip().lstrip('Tt'))] = heater.strip()
        except ValueError:
            logger.warning(f"heat_wait_stats: Ignoring invalid tool_heaters entry '{entry}'")
    return {tool: heater for tool, heater in mapping.items() if heater}


class HeatWaitRecorder:
    """Handles _KTC_HEAT_MARK and KTC_HEAT_STATS inside Klipper"""

    POLL_INTERVAL = 0.25
    # A pending tool change older than this is dropped (e.g. cancelled print)
    PENDING_TIMEOUT = 1800.

    def __init__(self, printer, gcode):
        self.printer = printer
        self.gcode = gcode
        self.reactor = printer.get_reactor()
        self.tool_heaters: Dict[int, str] = {}
        self.stats_file: Optional[str] = None
        self.report_at_end = True
        self.max_samples = 500
        self.samples: List[Dict[str, Any]] = []
        self.print_id: Optional[str] = None
        self.pending: Dict[int, Dict[str, Any]] = {}
        self.unsaved = 0
        self.timer = None
        gcode.register_command(MARK_COMMAND, self.cmd_KTC_HEAT_MARK,
                               desc=self.cmd_KTC_HEAT_MARK_help)
        gcode.register_command('KTC_HEAT_STATS', self.cmd_KTC_HEAT_STATS,
                               desc=self.cmd_KTC_HEAT_STATS_help)

    def configure(self, values: Dict[str, str], default_dir: Optional[str]):
        config = ProcessorConfig('gcode_preprocessor heat_wait_stats', values, logging)
        self.tool_heaters = parse_tool_heaters(config.get('tool_heaters', ''))
        self.report_at_end = config.getboolean('report_at_end', True)
        self.max_samples = max(10, config.getint('max_samples', 500))
        stats_file = config.get('stats_file', '')
        if stats_file:
            self.stats_file = os.path.expanduser(stats_file)
        elif default_dir:
            self.stats_file = os.path.join(default_dir, 'heat_stats.json')
        self._load()
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._poll)

    # --- persistence -----------------------------------------------------------

    def _load(self):
        if not self.stats_file or not os.path.exists(self.stats_file):
            return
        try:
            with open(self.stats_file, 'r') as f:
                data = json.load(f)
            self.samples = list(data.get('samples', []))
            for sample in self.samples:
                sample['tool'] = int(sample['tool'])
        except (OSError, ValueError, KeyError, TypeError):
            logging.exception(f"heat_wait_stats: Could not read {self.stats_file}")
            self.samples = []

    def _save(self):
        self.unsaved = 0
        if not self.stats_file:
            return
        try:
            os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
            temp_path = self.stats_file + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump({'version': 1, 'samples': self.samples}, f)
            os.replace(temp_path, self.stats_file)
        except OSError:
            logging.exception(f"heat_wait_stats: Could not write {self.stats_file}")

    def _add_sample(self, sample: Dict[str, Any]):
        self.samples.append(sample)
        # Keep the newest samples per tool
        tool_samples = [s for s in self.samples if s['tool'] == sample['tool']]
        if len(tool_samples) > self.max_samples:
            self.samples.remove(tool_samples[0])
        self.unsaved += 1
        if self.unsaved >= 25:
            self._save()

    # --- heater access -----------------------------------------------------------

    def _heater(self, tool: int):
        name = self.tool_heaters.get(tool)
        if name is None:
            # klipper-toolchanger: [tool Tn] knows its extruder
            tool_object = self.printer.lookup_object(f'tool T{tool}', None)
            if tool_object is not None and hasattr(tool_object, 'get_status'):
                try:
                    name = tool_object.get_status(self.reactor.monotonic()).get('extruder')
                except Exception:
                    name = None
        if not name:
            name = 'extruder' if tool == 0 else f'extruder{tool}'
        heaters = self.printer.lookup_object('heaters', None)
        if heaters is None:
            return None
        try:
            return heaters.lookup_heater(name)
        except Exception:
            return None

    @staticmethod
    def _is_reached(temp: float, target: float) -> bool:
        return target > 0 and temp >= target - REACHED_TOLERANCE

    def _motion_delay(self, eventtime: float) -> float:
        """Seconds until the already queued moves are done"""
        toolhead = self.printer.lookup_object('toolhead', None)
        mcu = self.printer.lookup_object('mcu', None)
        if toolhead is None or mcu is None:
            return 0.0
        try:
            return max(0.0, toolhead.get_last_move_time() - mcu.estimated_print_time(eventtime))
        except Exception:
            return 0.0

    def _poll(self, eventtime):
        active = False
        for tool, entry in list(self.pending.items()):
            if eventtime - entry['heat_start'] > self.PENDING_TIMEOUT:
                del self.pending[tool]
                continue
            if entry['reached'] is not None:
                continue
            temp, target = entry['heater'].get_temp(eventtime)
            if self._is_reached(temp, target):
                entry['reached'] = eventtime
            else:
                active = True
        return eventtime + self.POLL_INTERVAL if active else self.reactor.NEVER

    def _start_polling(self):
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NOW)

    # --- commands ----------------------------------------------------------------

    cmd_KTC_HEAT_MARK_help = "Tool change marker inserted by the heat_wait_stats preprocessor"
    def cmd_KTC_HEAT_MARK(self, gcmd):
        event = gcmd.get('EVENT', '').upper()
        now = self.reactor.monotonic()

        if event == 'START':
            self.print_id = time.strftime('%Y-%m-%d %H:%M:%S')
            self.pending.clear()
            return
        if event == 'END':
            self.pending.clear()
            self._save()
            if self.report_at_end:
                self.gcode.respond_info(self._report('PRINT'))
            return

        tool = gcmd.get_int('TOOL', None)
        heater = self._heater(tool) if tool is not None else None
        if heater is None:
            return
        temp, target = heater.get_temp(now)

        if event == 'PREHEAT':
            self.pending[tool] = {'heater': heater, 'heat_start': now, 'start_temp': temp,
                                  'preheat': now, 'reached': None}
            self._start_polling()
        elif event == 'CHANGE':
            entry = self.pending.get(tool)
            if entry is None:
                entry = self.pending[tool] = {'heater': heater, 'heat_start': now, 'start_temp': temp,
                                              'preheat': None, 'reached': None}
            if entry['reached'] is None and self._is_reached(temp, target):
                entry['reached'] = now
            entry['change'] = now + self._motion_delay(now)
            self._start_polling()
        elif event == 'READY':
            entry = self.pending.pop(tool, None)
            if entry is None or 'change' not in entry:
                return
            if entry['reached'] is None and self._is_reached(temp, target):
                entry['reached'] = now
            ready = now + self._motion_delay(now)
            reached = entry['reached']
            self._add_sample({
                'print': self.print_id,
                'tool': tool,
                'target': target,
                'start_temp': round(entry['start_temp'], 1),
                'lead': None if entry['preheat'] is None else round(entry['change'] - entry['preheat'], 2),
                'change_time': round(ready - entry['change'], 2),
                'heat_time': None if reached is None else round(reached - entry['heat_start'], 2),
                'reached_after': None if reached is None else round(reached - entry['change'], 2),
            })

    def _report(self, scope: str, tool: Optional[int] = None) -> str:
        if scope == 'ALL':
            samples, title = self.samples, "all recorded prints"
        else:
            last_print = self.print_id or (self.samples[-1]['print'] if self.samples else None)
            samples = [s for s in self.samples if s.get('print') == last_print]
            title = f"print {last_print}" if last_print else "last print"
        if tool is not None:
            samples = [s for s in samples if s['tool'] == tool]
        return format_report(analyze(samples), title)

    cmd_KTC_HEAT_STATS_help = ("Report the time spent waiting for nozzle temperature at tool changes "
                               "[SCOPE=PRINT|ALL] [TOOL=<n>] [RESET=1]")
    def cmd_KTC_HEAT_STATS(self, gcmd):
        if gcmd.get_int('RESET', 0):
            self.samples = []
            self._save()
            gcmd.respond_info("KTC heat stats cleared")
            return
        scope = gcmd.get('SCOPE', 'PRINT').upper()
        if scope not in ('PRINT', 'ALL'):
            raise gcmd.error("SCOPE must be PRINT or ALL")
        gcmd.respond_info(self._report(scope, gcmd.get_int('TOOL', None)))
