# Idle Tool Shutdown Preprocessor
# Automatically shuts down tools after their last usage or when idle for too long

from typing import Dict, List, Optional, Set, Tuple
import re
import math

try:
    # Loaded by Klipper as extras.preprocessors.idle_tool_shutdown
    from ..gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities
    )
except ImportError:
    # Loaded standalone (Moonraker script) with the extras dir on sys.path
    from gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities
    )


PARAM_PATTERN = re.compile(r'([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))', re.IGNORECASE)
TEMP_COMMAND = re.compile(r'^M10[49](?:\s|$)', re.IGNORECASE)
DWELL_COMMAND = re.compile(r'^G4(?:\s|$)', re.IGNORECASE)
MOVE_COMMAND = re.compile(r'^G([0-3])(?:\s|$)', re.IGNORECASE)
ABSOLUTE_COMMAND = re.compile(r'^G90(?:\s|$)', re.IGNORECASE)
RELATIVE_COMMAND = re.compile(r'^G91(?:\s|$)', re.IGNORECASE)
SET_POSITION_COMMAND = re.compile(r'^G92(?:\s|$)', re.IGNORECASE)


class IdleToolShutdown(GcodePreprocessorPlugin):
    """
    Processor that automatically inserts shutdown commands for tools either:
    1. After their last usage in the G-code file (default behavior)
    2. When they've been idle for a specified time period (optional)
    """

    def __init__(self, config, logger):
        super().__init__(config, logger)

        # Idle timeout feature (disabled by default)
        self.idle_timeout_minutes = config.getfloat('idle_timeout_minutes', 0.0)
        self.idle_timeout_seconds = self.idle_timeout_minutes * 60.0
        self.idle_timeout_enabled = self.idle_timeout_minutes > 0

        # Initial feedrate for time estimation before any F parameter is seen (mm/min)
        self.initial_feedrate = config.getfloat('initial_feedrate', 3000.0)

        # Parse exclude_tools
        self.exclude_tools: Set[int] = set()
        for tool_str in (config.get('exclude_tools', '') or '').split(','):
            tool_str = tool_str.strip()
            if not tool_str:
                continue
            try:
                self.exclude_tools.add(int(tool_str))
            except ValueError:
                self.logger.warning(f"idle_tool_shutdown: Ignoring invalid exclude_tools entry '{tool_str}'")

        # Optional mapping tool number -> Klipper heater name, e.g.
        # "0=extruder, 1=extruder1". Mapped tools are shut down with
        # SET_HEATER_TEMPERATURE instead of M104 T<n>, which Klipper always
        # resolves to the heater "extruder<n>".
        self.tool_heaters: Dict[int, str] = self._parse_tool_heaters(config.get('tool_heaters', ''))

        self._reset_state()

    def _parse_tool_heaters(self, value: str) -> Dict[int, str]:
        mapping: Dict[int, str] = {}
        for entry in (value or '').split(','):
            entry = entry.strip()
            if not entry:
                continue
            tool_str, sep, heater = entry.partition('=')
            if not sep:
                tool_str, sep, heater = entry.partition(':')
            try:
                tool_number = int(tool_str.strip().lstrip('Tt'))
            except ValueError:
                tool_number = None
            if not sep or tool_number is None or not heater.strip():
                self.logger.warning(f"idle_tool_shutdown: Ignoring invalid tool_heaters entry '{entry}'")
                continue
            mapping[tool_number] = heater.strip()
        return mapping

    def _reset_state(self):
        """Reset all per-file state (the processor instance is reused)"""
        # End-of-use tracking
        self.tool_usage_map: Dict[int, List[int]] = {}  # tool_number -> [line_numbers]
        self.tool_last_usage: Dict[int, int] = {}  # tool_number -> last_line_number
        self.tools_to_cooldown: Set[int] = set()  # Tools that need cooldown
        self.current_tool: Optional[int] = None
        self.pending_cooldown: Optional[int] = None  # Tool to cool after current line

        # Idle timeout tracking
        self.toolchange_times: Dict[int, float] = {}  # toolchange line_number -> estimated time (s)
        self.tool_usage_timeline: Dict[int, List[Tuple[int, float]]] = {}  # tool_number -> [(line_num, time)]
        self.current_time: float = 0.0  # Estimated current print time in seconds
        self.tools_shutdown_idle: Set[int] = set()  # Tools shutdown due to idle timeout

    def get_name(self) -> str:
        return "idle_tool_shutdown"

    def get_description(self) -> str:
        desc = "Automatically shuts down tools after their last usage"
        if self.idle_timeout_enabled:
            desc += f" or when idle > {self.idle_timeout_minutes} minutes"
        return desc

    def _parse_gcode_params(self, line: str) -> Dict[str, float]:
        """
        Parse G-code parameters from a line (comment stripped, command word skipped)
        Returns dict of parameter: value (e.g., {'X': 100.5, 'Y': 50.0, 'F': 3000})
        """
        params = {}
        command_part, _ = GcodePatterns.strip_comment(line)
        parts = command_part.strip().split(None, 1)
        if len(parts) < 2:
            return params
        for match in PARAM_PATTERN.finditer(parts[1]):
            try:
                params[match.group(1).upper()] = float(match.group(2))
            except ValueError:
                continue
        return params

    def _format_shutdown_command(self, tool_number: int) -> str:
        heater = self.tool_heaters.get(tool_number)
        if heater:
            return f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET=0\n"
        return PreprocessorUtilities.format_tool_temp_command(tool_number, 0)

    def _estimate_move_time(self, command: str, params: Dict[str, float],
                            position: Dict[str, float], feedrate: float,
                            relative: bool) -> Tuple[float, Dict[str, float], float]:
        """
        Estimate time for a G0-G3 movement command in seconds (no acceleration)
        Returns: (time_seconds, new_position, new_feedrate)
        """
        new_feedrate = params.get('F', feedrate)

        new_position = position.copy()
        for axis in ('X', 'Y', 'Z'):
            if axis in params:
                if relative:
                    new_position[axis] = position[axis] + params[axis]
                else:
                    new_position[axis] = params[axis]

        dx = new_position['X'] - position['X']
        dy = new_position['Y'] - position['Y']
        dz = new_position['Z'] - position['Z']

        if command in ('2', '3'):
            distance = self._arc_length(command, params, dx, dy, dz)
        else:
            distance = math.sqrt(dx * dx + dy * dy + dz * dz)

        if new_feedrate > 0 and distance > 0:
            return (distance / new_feedrate) * 60.0, new_position, new_feedrate
        return 0.0, new_position, new_feedrate

    @staticmethod
    def _arc_length(command: str, params: Dict[str, float], dx: float, dy: float, dz: float) -> float:
        """Length of a G2/G3 arc in the XY plane (I/J or R form)"""
        chord = math.hypot(dx, dy)
        if 'I' in params or 'J' in params:
            i = params.get('I', 0.0)
            j = params.get('J', 0.0)
            radius = math.hypot(i, j)
            if radius == 0:
                return math.sqrt(chord * chord + dz * dz)
            # Angles from the arc center to start and end point
            start_angle = math.atan2(-j, -i)
            end_angle = math.atan2(dy - j, dx - i)
            sweep = end_angle - start_angle
            if command == '2':  # clockwise
                if sweep >= 0:
                    sweep -= 2 * math.pi
            else:  # counter-clockwise
                if sweep <= 0:
                    sweep += 2 * math.pi
            planar = abs(sweep) * radius
        elif 'R' in params:
            radius = abs(params['R'])
            if radius == 0 or chord > 2 * radius:
                planar = chord
            else:
                angle = 2 * math.asin(chord / (2 * radius))
                if params['R'] < 0:
                    angle = 2 * math.pi - angle
                planar = angle * radius
        else:
            planar = chord
        return math.sqrt(planar * planar + dz * dz)

    @staticmethod
    def _dwell_seconds(params: Dict[str, float]) -> float:
        # G4 P is in milliseconds, G4 S is in seconds
        if 'P' in params:
            return params['P'] / 1000.0
        if 'S' in params:
            return params['S']
        return 0.0

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        First pass: Scan entire file to build tool usage map and estimate print times
        """
        self.logger.info(f"idle_tool_shutdown: Scanning file for tool usage")

        self._reset_state()

        # For idle timeout, we need to estimate print times
        position = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
        feedrate = self.initial_feedrate
        relative = False
        current_time = 0.0

        for line_num, line in enumerate(PreprocessorUtilities.iter_file_lines(file_path)):
            # Extract tool number from any tool change command
            tool_number = GcodePatterns.extract_tool_number(line)

            if tool_number is not None:
                self.tool_usage_map.setdefault(tool_number, []).append(line_num)

                # For idle timeout, build timeline with timestamps
                if self.idle_timeout_enabled:
                    self.toolchange_times[line_num] = current_time
                    self.tool_usage_timeline.setdefault(tool_number, []).append((line_num, current_time))
                continue

            if not self.idle_timeout_enabled:
                continue

            command = line.lstrip()
            move = MOVE_COMMAND.match(command)
            if move:
                params = self._parse_gcode_params(command)
                move_time, position, feedrate = self._estimate_move_time(
                    move.group(1), params, position, feedrate, relative)
                current_time += move_time
            elif DWELL_COMMAND.match(command):
                current_time += self._dwell_seconds(self._parse_gcode_params(command))
            elif ABSOLUTE_COMMAND.match(command):
                relative = False
            elif RELATIVE_COMMAND.match(command):
                relative = True
            elif SET_POSITION_COMMAND.match(command):
                params = self._parse_gcode_params(command)
                if not any(axis in params for axis in ('X', 'Y', 'Z', 'E')):
                    position = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}
                for axis in ('X', 'Y', 'Z'):
                    if axis in params:
                        position[axis] = params[axis]

        # Determine last usage for each tool
        for tool_number, usage_lines in self.tool_usage_map.items():
            if usage_lines:
                self.tool_last_usage[tool_number] = max(usage_lines)

        # Determine which tools should be cooled down (at end of use)
        for tool_number in self.tool_usage_map.keys():
            if tool_number not in self.exclude_tools:
                self.tools_to_cooldown.add(tool_number)

        self.logger.info(f"idle_tool_shutdown: Found {len(self.tool_usage_map)} tools used in file")
        self.logger.info(f"idle_tool_shutdown: Tools to manage: {sorted(self.tool_usage_map.keys())}")
        self.logger.info(f"idle_tool_shutdown: Excluded tools: {sorted(self.exclude_tools)}")
        self.logger.info(f"idle_tool_shutdown: Last usage map: {self.tool_last_usage}")

        if self.idle_timeout_enabled:
            self.logger.info(f"idle_tool_shutdown: Idle timeout enabled: {self.idle_timeout_minutes} minutes")
            self.logger.info(f"idle_tool_shutdown: Estimated total print time: {current_time / 60.0:.2f} minutes")

        # Store metadata for other processors
        context.set_metadata('tools_used', sorted(self.tool_usage_map.keys()))
        context.set_metadata('tool_last_usage', dict(self.tool_last_usage))

        return True

    def _get_next_tool_usage_time(self, tool_num: int, current_line: int) -> Optional[float]:
        """
        Get the time when the tool will be used next (after current line)
        Returns None if tool won't be used again
        """
        for line_num, usage_time in self.tool_usage_timeline.get(tool_num, []):
            if line_num > current_line:
                return usage_time
        return None  # No future usage

    def _mark_reheated(self, tool_num: int, reason: str, line_num: int):
        if tool_num in self.tools_shutdown_idle:
            self.tools_shutdown_idle.discard(tool_num)
            self.logger.info(f"idle_tool_shutdown: T{tool_num} {reason} at line {line_num}, "
                             f"allowing future cooldowns")

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        """
        Process each line and insert cooldown commands when appropriate
        """
        output_lines = []
        line_num = context.current_line

        # A heating command (M104/M109 with S > 0) re-arms the cooldown logic
        # for the tool it targets: the T parameter, or the active tool if absent
        command = line.lstrip()
        if TEMP_COMMAND.match(command):
            params = self._parse_gcode_params(command)
            if params.get('S', 0) > 0:
                if 'T' in params:
                    target_tool = int(params['T'])
                else:
                    target_tool = self.current_tool
                if target_tool is not None:
                    self._mark_reheated(target_tool, f"reheated to {params['S']}C", line_num)

        # Check if we have a pending cooldown to insert (end-of-use feature)
        if self.pending_cooldown is not None:
            cooldown_tool = self.pending_cooldown
            self.pending_cooldown = None

            # Only insert if not already shutdown by idle timeout
            if cooldown_tool not in self.tools_shutdown_idle:
                output_lines.append(f"; T{cooldown_tool} no longer needed - cooling down\n")
                output_lines.append(self._format_shutdown_command(cooldown_tool))

                self.logger.info(f"idle_tool_shutdown: Inserted end-of-use cooldown for T{cooldown_tool} at line {line_num}")

        # Now process the current line
        tool_number = GcodePatterns.extract_tool_number(line)

        if tool_number is not None:
            # This is a tool change
            previous_tool = self.current_tool
            self.current_tool = tool_number
            if self.idle_timeout_enabled:
                self.current_time = self.toolchange_times.get(line_num, self.current_time)

            # A selected tool is in use, so it has to be hot again (heated by
            # the tool change macro or the slicer)
            self._mark_reheated(tool_number, "selected", line_num)

            self.logger.debug(f"idle_tool_shutdown: Tool change to T{tool_number} at line {line_num}, "
                              f"time={self.current_time/60.0:.2f}min")

            # Re-selecting the active tool is not a tool change for cooldown purposes
            if previous_tool is not None and previous_tool != tool_number:
                # PREDICTIVE IDLE TIMEOUT: Check if the previous tool will be idle too long
                if (self.idle_timeout_enabled and
                        previous_tool not in self.exclude_tools and
                        previous_tool not in self.tools_shutdown_idle):

                    # Find when the previous tool will be used next
                    next_usage_time = self._get_next_tool_usage_time(previous_tool, line_num)

                    # None: tool won't be used again - end-of-use feature handles it
                    if next_usage_time is not None:
                        predicted_idle_time = next_usage_time - self.current_time

                        # If tool will be idle longer than threshold, shut it down NOW
                        if predicted_idle_time >= self.idle_timeout_seconds:
                            output_lines.append(f"; T{previous_tool} will be idle for {predicted_idle_time/60.0:.2f} minutes - cooling down\n")
                            output_lines.append(self._format_shutdown_command(previous_tool))

                            self.tools_shutdown_idle.add(previous_tool)
                            self.logger.info(f"idle_tool_shutdown: Inserted predictive cooldown for T{previous_tool} at line {line_num}, "
                                             f"predicted_idle={predicted_idle_time/60.0:.2f}min")

                # Check if this is the last usage of the previous tool (end-of-use feature)
                if previous_tool in self.tools_to_cooldown:
                    if line_num >= self.tool_last_usage.get(previous_tool, -1):
                        # Schedule it for cooldown after this tool change line
                        self.pending_cooldown = previous_tool

        # Output the current line unchanged
        output_lines.append(line)

        return output_lines

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Final pass: Log summary
        """
        if self.pending_cooldown is not None:
            self.logger.warning(f"idle_tool_shutdown: Tool T{self.pending_cooldown} had pending cooldown at end of file")
            self.pending_cooldown = None

        self.logger.info(f"idle_tool_shutdown: Processing complete")
        if self.idle_timeout_enabled:
            self.logger.info(f"idle_tool_shutdown: Shutdown {len(self.tools_shutdown_idle)} tools due to idle timeout")
            if self.tools_shutdown_idle:
                self.logger.info(f"idle_tool_shutdown: Idle shutdown tools: {sorted(self.tools_shutdown_idle)}")

        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return IdleToolShutdown(config, logger)
