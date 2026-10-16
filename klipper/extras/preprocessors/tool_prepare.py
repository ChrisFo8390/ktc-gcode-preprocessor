# Tool Prepare
# Brings every tool a print uses into a defined state at print start: loaded
# into the extruder (AFC), purged and brushed, and retracted by the slicer's
# tool change retraction. The first use of each tool in the G-code then pushes
# exactly that length back - only if KTC_PREPARE_TOOLS retracted that tool.
#
# Both parts are configured in [gcode_preprocessor tool_prepare]:
# - ToolPrepare (processor, runs at upload): inserts
#   "_KTC_PREPARE_TOOLS_SET TOOLS=.. TEMPS=.. RETRACT=.. STANDBY=.. INITIAL=.." before the
#   print start macro, and inserts "_KTC_PREPARE_PUSH T=<n> E=<retraction>
#   F=<speed>" right before the first extrusion after the first tool change of
#   every tool except the initial one (the slicer only pushes its normal travel
#   unretract there, or nothing when the wipe tower primes the tool).
# - ToolPrepareRuntime (Klipper): KTC_PREPARE_TOOLS, called from PRINT_START
#   after homing. Checks the AFC lanes of all tools, heats the tools staggered,
#   then picks up each tool: lanes not loaded into the extruder are loaded by
#   AFC (which purges/brushes), loaded tools are purged/brushed with the AFC
#   macros (or purge_cmd/wipe_cmd); then the tool is retracted and set to the
#   standby temperature.
#   Works without AFC and without purge/brush macros: a tool that was neither
#   loaded nor purged is not retracted (its filament position is unknown). The
#   commands are always registered, so PRINT_START can call KTC_PREPARE_TOOLS
#   even when tool_prepare is not listed in processors.

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    # Loaded by Klipper as extras.preprocessors.tool_prepare
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


SET_COMMAND = '_KTC_PREPARE_TOOLS_SET'
PUSH_COMMAND = '_KTC_PREPARE_PUSH'
# Initial tool passed to the print start macro (TOOL=0, T=T1, INITIAL_TOOL=2, ...)
START_TOOL_PARAM = re.compile(r'(?:^|\s)(?:TOOL|T|INITIAL_TOOL|INITIAL_EXTRUDER)=T?(\d+)(?=\s|$)',
                              re.IGNORECASE)
MOVE = re.compile(r'^G[0-3](?:\s|$)', re.IGNORECASE)
# Positive E word: extrusion or unretract (not "E-1")
EXTRUDE_WORD = re.compile(r'\sE\+?(\d*\.?\d+)', re.IGNORECASE)


def _split(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip().strip('"') for v in re.split('[;,]', value)]


def _number(values: List[str], index: int) -> Optional[float]:
    if 0 <= index < len(values):
        try:
            return float(values[index])
        except ValueError:
            return None
    return None


class ToolPrepare(GcodePreprocessorPlugin):
    """Inserts the data for KTC_PREPARE_TOOLS and the push back at the first use of each tool"""

    def __init__(self, config, logger):
        super().__init__(config, logger)
        macros = config.get('print_start_macros', 'PRINT_START, START_PRINT') or ''
        self.print_start_macros = {m.strip().upper() for m in macros.split(',') if m.strip()}
        # Fixed retraction instead of the slicer's retract_length_toolchange
        self.retract_length = config.getfloat('retract_length', 0.)
        self._reset_state()

    def _reset_state(self):
        # input line index -> (original line, replacement lines)
        self.replace: Dict[int, Tuple[str, List[str]]] = {}
        self.set_line: Optional[str] = None
        self.fixed = 0

    def get_name(self) -> str:
        return "tool_prepare"

    def get_description(self) -> str:
        return (f"Passes the used tools to KTC_PREPARE_TOOLS ({SET_COMMAND} before the print start "
                f"macro) and pushes the prepare retraction back at the first use of each tool")

    @staticmethod
    def _speeds(slicer_config: Dict[str, str], tools: List[int]) -> Dict[int, float]:
        """Unretract speed in mm/s per tool (deretract_speed, 0 = retract_speed)"""
        deretract = _split(slicer_config.get('deretract_speed'))
        retract = _split(slicer_config.get('retract_speed'))
        return {tool: _number(deretract, tool) or _number(retract, tool) or 35.
                for tool in tools}

    @staticmethod
    def _standby(slicer_config: Dict[str, str], tools: List[int],
                 temps: Dict[int, float]) -> Dict[int, float]:
        """Slicer standby per tool: idle_temperature, else temperature + standby_temperature_delta"""
        idle = _split(slicer_config.get('idle_temperature'))
        delta = _number(_split(slicer_config.get('standby_temperature_delta')), 0)
        result = {}
        for tool in tools:
            value = _number(idle, tool)  # 'nil' = not set
            if value is None or value <= 0:
                value = temps[tool] + delta if delta is not None and temps[tool] > 0 else 0.
            result[tool] = max(0., value)
        return result

    def _retractions(self, slicer_config: Dict[str, str], tools: List[int]) -> Dict[int, float]:
        printer = _split(slicer_config.get('retract_length_toolchange'))
        filament = _split(slicer_config.get('filament_retract_length_toolchange'))
        result = {}
        for tool in tools:
            if self.retract_length > 0:
                result[tool] = self.retract_length
                continue
            value = _number(filament, tool)  # 'nil' = no filament override
            if value is None:
                value = _number(printer, tool)
            result[tool] = value if value is not None else 0.
        return result

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        self._reset_state()
        slicer_config: Optional[Dict[str, str]] = context.get_metadata('slicer_config')
        if slicer_config is None:
            self.logger.warning("tool_prepare: No slicer data - list token_replacer before tool_prepare "
                                "in [gcode_preprocessor] processors")
            return True

        start_line: Optional[Tuple[int, str]] = None
        initial: Optional[int] = None
        first_tool: Optional[int] = None
        tools: List[int] = []
        seen = set()
        pending: Optional[int] = None   # tool whose first extrusion is searched
        fixes: List[Tuple[int, int, str]] = []  # (line, tool, text)

        for index, raw in enumerate(PreprocessorUtilities.iter_file_lines(file_path)):
            line = raw.strip()
            if not line or line.startswith(';'):
                continue
            command, _ = GcodePatterns.strip_comment(line)
            upper = command.upper()
            words = command.split(None, 1)

            if start_line is None and words and words[0].upper() in self.print_start_macros:
                start_line = (index, raw)
                match = START_TOOL_PARAM.search(words[1] if len(words) > 1 else '')
                if match:
                    initial = int(match.group(1))
                continue

            tool = GcodePatterns.extract_tool_number(command)
            if tool is not None:
                pending = None
                if first_tool is None:
                    first_tool = tool
                if tool not in seen:
                    seen.add(tool)
                    tools.append(tool)
                    if start_line is not None and tool != (initial if initial is not None else first_tool):
                        pending = tool
                continue

            if pending is not None and MOVE.match(command) and EXTRUDE_WORD.search(' ' + command):
                fixes.append((index, pending, raw))
                pending = None

        if start_line is None or not tools:
            self.logger.info("tool_prepare: No print start macro or no tool found - nothing inserted")
            return True
        if initial is None:
            initial = first_tool
        if initial not in tools:
            tools.insert(0, initial)

        temperatures = _split(','.join(context.get_metadata('temperatures', []) or []))
        temps = {tool: _number(temperatures, tool) or 0. for tool in tools}
        retract = self._retractions(slicer_config, tools)
        standby = self._standby(slicer_config, tools, temps)

        def values(table):
            return ','.join(f'{table[t]:g}' for t in tools)
        self.set_line = (f"{SET_COMMAND} TOOLS={','.join(map(str, tools))} TEMPS={values(temps)} "
                         f"RETRACT={values(retract)} STANDBY={values(standby)} INITIAL={initial}\n")
        self.replace[start_line[0]] = (start_line[1], [self.set_line, start_line[1]])

        speeds = self._speeds(slicer_config, tools)
        for index, tool, raw in fixes:
            length = retract.get(tool, 0.)
            if length <= 0:
                continue
            # Only pushes in Klipper if KTC_PREPARE_TOOLS retracted this tool
            push = f"{PUSH_COMMAND} T={tool} E={length:g} F={speeds[tool] * 60:g}\n"
            self.replace[index] = (raw, [push, raw])
            self.fixed += 1
        self.logger.info(f"tool_prepare: Tools {tools}, initial T{initial}, "
                         f"push back inserted at {self.fixed} first tool use(s)")
        return True

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        entry = self.replace.get(context.current_line)
        # Only the original line, not lines inserted by earlier processors
        if entry is not None and line == entry[0]:
            del self.replace[context.current_line]
            return list(entry[1])
        return [line]

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        if self.replace:
            self.logger.warning(f"tool_prepare: {len(self.replace)} line(s) not found - "
                                f"another processor changed them")
        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return ToolPrepare(config, logger)


# ---------------------------------------------------------------------------
# Klipper runtime
# ---------------------------------------------------------------------------

def heating_plan(tools: List[int], max_heating: int) -> List[Tuple[int, Optional[int]]]:
    """
    Order in which the tools start heating: (tool, tool to wait for first).
    Not more than max_heating tools heat up at the same time.
    """
    plan = []
    for index, tool in enumerate(tools):
        wait_for = tools[index - max_heating] if max_heating > 0 and index >= max_heating else None
        plan.append((tool, wait_for))
    return plan


class ToolPrepareRuntime:
    """Klipper side of tool_prepare: _KTC_PREPARE_TOOLS_SET and KTC_PREPARE_TOOLS"""

    def __init__(self, config, values: Dict[str, str], name: Optional[str] = None):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = name or config.get_name()
        self.values = dict(values)
        options = ProcessorConfig(self.name, self.values, logging)
        self.heat_stagger = max(0., options.getfloat('heat_stagger', 10.))
        self.max_heating = max(0, options.getint('max_heating', 3))
        # None: slicer standby (STANDBY from the processor); a value overrides it
        self.standby_temp = options.getfloat('standby_temp', None)
        self.temp_tolerance = options.getfloat('temp_tolerance', 2.)
        self.retract_speed = options.getfloat('retract_speed', 35.)
        # Travel height before picking up a tool and before purging (0 = off)
        self.safe_z = max(0., options.getfloat('safe_z', 5.))
        self.z_speed = options.getfloat('z_speed', 10.)
        self.purge_initial = options.getboolean('purge_initial', False)
        # Purge/brush macros: empty = AFC poop_cmd/wipe_cmd (if enabled in [AFC]), 'none' = off
        self.purge_cmd = (options.get('purge_cmd', '') or '').strip()
        self.wipe_cmd = (options.get('wipe_cmd', '') or '').strip()
        self.abort_on_empty = options.getboolean('abort_on_empty', True)
        self.tool_heaters = self._parse_tool_heaters(options.get('tool_heaters', ''))
        self.data: Optional[Dict[str, Any]] = None
        # tool -> length KTC_PREPARE_TOOLS retracted it in the current print
        self.retracted: Dict[int, float] = {}
        self.gcode.register_command(SET_COMMAND, self.cmd_SET, desc=self.cmd_SET_help)
        self.gcode.register_command('KTC_PREPARE_TOOLS', self.cmd_KTC_PREPARE_TOOLS,
                                    desc=self.cmd_KTC_PREPARE_TOOLS_help)
        self.gcode.register_command(PUSH_COMMAND, self.cmd_PUSH, desc=self.cmd_PUSH_help)

    @staticmethod
    def _parse_tool_heaters(value: str) -> Dict[int, str]:
        mapping: Dict[int, str] = {}
        for entry in (value or '').split(','):
            tool_str, sep, heater = entry.strip().partition('=')
            try:
                tool = int(tool_str.strip().lstrip('Tt'))
            except ValueError:
                continue
            if sep and heater.strip():
                mapping[tool] = heater.strip()
        return mapping

    # Printer state -----------------------------------------------------------

    def _heater_name(self, tool: int) -> str:
        if tool in self.tool_heaters:
            return self.tool_heaters[tool]
        tool_object = self.printer.lookup_object(f'tool T{tool}', None)
        if tool_object is not None and hasattr(tool_object, 'get_status'):
            name = tool_object.get_status(self.reactor.monotonic()).get('extruder')
            if name:
                return name
        return 'extruder' if tool == 0 else f'extruder{tool}'

    def _lane(self, tool: int) -> Optional[Dict[str, Any]]:
        """Status of the AFC lane mapped to T<tool>, None without AFC or lane"""
        afc = self.printer.lookup_object('AFC', None)
        lanes = getattr(afc, 'lanes', None) if afc is not None else None
        if not lanes:
            return None
        now = self.reactor.monotonic()
        for name, lane in lanes.items():
            status = lane.get_status(now) if hasattr(lane, 'get_status') else {}
            if status.get('map') == f'T{tool}':
                status = dict(status)
                status.setdefault('name', name)
                return status
        return None

    def _command_exists(self, name: str) -> bool:
        if self.printer.lookup_object(f'gcode_macro {name}', None) is not None:
            return True
        handlers = getattr(self.gcode, 'ready_gcode_handlers', None)
        return handlers is not None and name.upper() in handlers

    def _macro(self, kind: str) -> Optional[str]:
        """Purge ('poop') or brush ('wipe') command, None if not configured or not defined"""
        name = self.purge_cmd if kind == 'poop' else self.wipe_cmd
        if not name:
            afc = self.printer.lookup_object('AFC', None)
            if afc is None or not getattr(afc, kind, False):
                return None
            name = getattr(afc, f'{kind}_cmd', None) or ''
        if not name or name.lower() == 'none':
            return None
        if not self._command_exists(name):
            self.gcode.respond_info(f"KTC_PREPARE_TOOLS: {name} not defined - skipped")
            return None
        return name

    def _check_homed(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead', None)
        if toolhead is None:
            return
        homed = toolhead.get_status(self.reactor.monotonic()).get('homed_axes', '')
        if not all(axis in homed for axis in 'xyz'):
            raise gcmd.error("KTC_PREPARE_TOOLS: home all axes first (call it after G28 in PRINT_START)")

    def _lift(self):
        """Raise to safe_z (never lowers) so XY travel cannot hit the brush or bucket"""
        if self.safe_z <= 0:
            return
        gcode_move = self.printer.lookup_object('gcode_move', None)
        if gcode_move is not None:
            z = gcode_move.get_status(self.reactor.monotonic())['gcode_position'].z
            if z >= self.safe_z:
                return
        self._run("SAVE_GCODE_STATE NAME=_ktc_prepare_z\nG90\n"
                  f"G1 Z{self.safe_z:g} F{self.z_speed * 60:g}\n"
                  "RESTORE_GCODE_STATE NAME=_ktc_prepare_z")

    def _extrude(self, length: float, feedrate: float):
        """Relative E move of the active tool, extrusion mode and position kept"""
        self._run("SAVE_GCODE_STATE NAME=_ktc_prepare\nM83\n"
                  f"G1 E{length:g} F{feedrate:g}\n"
                  "RESTORE_GCODE_STATE NAME=_ktc_prepare")

    def _run(self, script: str):
        self.gcode.run_script_from_command(script)

    def _heat(self, heater: str, temp: float):
        self._run(f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={temp:g}")

    def _wait(self, heater: str, temp: float):
        self._run(f"TEMPERATURE_WAIT SENSOR={heater} MINIMUM={temp - self.temp_tolerance:g}")

    # Commands -----------------------------------------------------------------

    cmd_SET_help = "Tools of the current print for KTC_PREPARE_TOOLS (inserted by tool_prepare)"
    def cmd_SET(self, gcmd):
        def numbers(key, cast):
            return [cast(v) for v in gcmd.get(key, '').split(',') if v.strip()]
        tools = numbers('TOOLS', int)
        temps = numbers('TEMPS', float)
        retract = numbers('RETRACT', float)
        standby = numbers('STANDBY', float) or [0.] * len(tools)
        if not tools or not len(temps) == len(retract) == len(standby) == len(tools):
            raise gcmd.error(f"{SET_COMMAND}: TOOLS, TEMPS, RETRACT and STANDBY need one value per tool")
        self.retracted = {}   # a new print starts
        self.data = {'tools': tools, 'temps': dict(zip(tools, temps)),
                     'retract': dict(zip(tools, retract)), 'standby': dict(zip(tools, standby)),
                     'initial': gcmd.get_int('INITIAL', tools[0])}

    cmd_KTC_PREPARE_TOOLS_help = ("Load, purge and retract the tools of the current print "
                                  "[STAGGER=<s>] [MAX_HEATING=<n>]")
    def cmd_KTC_PREPARE_TOOLS(self, gcmd):
        data, self.data = self.data, None   # never reused for a later print
        self.retracted = {}
        if data is None:
            gcmd.respond_info("KTC_PREPARE_TOOLS: no tool data (file not processed by tool_prepare) - skipped")
            return
        self._check_homed(gcmd)
        stagger = gcmd.get_float('STAGGER', self.heat_stagger, minval=0.)
        max_heating = gcmd.get_int('MAX_HEATING', self.max_heating, minval=0)
        initial = data['initial']
        # Initial tool last: it stays picked up for the print start purge
        order = [t for t in data['tools'] if t != initial] + [initial]
        heaters = {tool: self._heater_name(tool) for tool in order}

        # 1. Check all lanes before anything moves or heats
        lanes = {tool: self._lane(tool) for tool in order}
        empty = [f"T{t} ({lane['name']})" for t, lane in lanes.items()
                 if lane is not None and not lane.get('tool_loaded') and not lane.get('load')]
        if empty:
            msg = f"KTC_PREPARE_TOOLS: no filament in lane for {', '.join(empty)}"
            if self.abort_on_empty:
                raise gcmd.error(msg + " - print aborted, load the filament and restart")
            gcmd.respond_info(msg + " - continuing")

        # 2. Heat staggered, at most max_heating tools at the same time
        lines = []
        for tool, lane in lanes.items():
            state = 'no AFC lane' if lane is None else (
                'in extruder' if lane.get('tool_loaded') else 'loaded in unit')
            lines.append(f"T{tool}: {data['temps'][tool]:g}C, standby {self._standby_for(data, tool):g}C, "
                         f"retract {data['retract'][tool]:g}mm, {state}")
        gcmd.respond_info("KTC_PREPARE_TOOLS:\n" + "\n".join(lines))
        try:
            for index, (tool, wait_for) in enumerate(heating_plan(order, max_heating)):
                if wait_for is not None:
                    self._wait(heaters[wait_for], data['temps'][wait_for])
                self._heat(heaters[tool], data['temps'][tool])
                if stagger and index < len(order) - 1:
                    self._run(f"G4 P{int(stagger * 1000)}")

            # 3. Load / purge / retract each tool
            for tool in order:
                self._prepare(tool, heaters[tool], data['temps'][tool], data['retract'][tool],
                              self._standby_for(data, tool), lanes[tool], tool == initial)
        except Exception:
            for tool in order:
                if tool != initial:
                    self._heat(heaters[tool], 0.)
            raise
        gcmd.respond_info(f"KTC_PREPARE_TOOLS: {len(order)} tool(s) prepared, T{initial} active")

    def _standby_for(self, data: Dict[str, Any], tool: int) -> float:
        if self.standby_temp is not None:
            return self.standby_temp
        return data['standby'][tool]

    def _prepare(self, tool: int, heater: str, temp: float, retract: float, standby: float,
                 lane: Optional[Dict[str, Any]], is_initial: bool):
        was_loaded = lane is None or bool(lane.get('tool_loaded'))
        self._lift()
        self._run(f"T{tool}")   # AFC loads (and purges/brushes) a lane not in the extruder
        if lane is not None and not was_loaded:
            after = self._lane(tool)
            if after is None or not after.get('tool_loaded'):
                raise self.gcode.error(f"KTC_PREPARE_TOOLS: AFC did not load T{tool} ({lane['name']})")
        self._wait(heater, temp)

        if was_loaded and is_initial and not self.purge_initial:
            # Filament position unknown, the print start purge line primes it
            return
        # Freshly loaded by AFC: filament is at the nozzle (AFC purged if enabled)
        defined = not was_loaded
        if was_loaded:
            purge, wipe = self._macro('poop'), self._macro('wipe')
            if purge or wipe:
                self._lift()
            if purge:
                self._run(f"{purge} EXTRUDER={heater}")
                defined = True
            if wipe:
                self._run(f"{wipe} EXTRUDER={heater}")
        if not defined:
            # No load and no purge: leave the filament where it is
            self.gcode.respond_info(f"KTC_PREPARE_TOOLS: T{tool} not purged (no purge macro) - not retracted")
        elif retract > 0:
            self._extrude(-retract, self.retract_speed * 60)
            self.retracted[tool] = retract
        if not is_initial:
            self._heat(heater, standby)

    cmd_PUSH_help = "Push back the KTC_PREPARE_TOOLS retraction at the first use of a tool (inserted by tool_prepare)"
    def cmd_PUSH(self, gcmd):
        tool = gcmd.get_int('T', minval=0)
        retracted = self.retracted.pop(tool, None)
        if retracted is None:
            # Not prepared in this print: the filament stays where the slicer expects it
            return
        length = min(gcmd.get_float('E', retracted, minval=0.), retracted)
        if length > 0:
            self._extrude(length, gcmd.get_float('F', self.retract_speed * 60, above=0.))

    def get_status(self, eventtime):
        return {'options': dict(self.values), 'pending': self.data is not None,
                'retracted': {f'T{t}': length for t, length in self.retracted.items()}}
