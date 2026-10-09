# Tool Summary Preprocessor
# Inserts a call to the _KTC_TOOL_SUMMARY macro before PRINT_START, which prints
# the tools used by the print with their temperatures and filament colors to
# the console (see config/tool-summary.cfg)

from typing import Dict, List, Optional
import re

try:
    # Loaded by Klipper as extras.preprocessors.tool_summary
    from ..gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
    )
except ImportError:
    # Loaded standalone (Moonraker script) with the extras dir on sys.path
    from gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
    )


HEX_COLOR = re.compile(r'^[0-9A-Fa-f]{6}$')
# Slicer settings holding the first layer nozzle temperature per filament
FIRST_LAYER_KEYS = ('first_layer_temperature', 'nozzle_temperature_initial_layer')


class ToolSummary(GcodePreprocessorPlugin):
    """
    Inserts '_KTC_TOOL_SUMMARY TOOLS=.. TEMPS=.. FIRST_TEMPS=.. COLORS=..'
    before the print start macro. Uses the data gathered by token_replacer,
    which therefore has to be listed before this processor.
    """

    def __init__(self, config, logger):
        super().__init__(config, logger)
        macros = config.get('print_start_macros', 'PRINT_START, START_PRINT') or ''
        self.print_start_macros = {m.strip().upper() for m in macros.split(',') if m.strip()}
        self.summary_macro = (config.get('summary_macro', '_KTC_TOOL_SUMMARY') or '').strip()
        self._reset_state()

    def _reset_state(self):
        self.summary_line: Optional[str] = None
        self.inserted = False

    def get_name(self) -> str:
        return "tool_summary"

    def get_description(self) -> str:
        return (f"Prints used tools, temperatures and filament colors via {self.summary_macro} "
                f"before {'/'.join(sorted(self.print_start_macros))}")

    @staticmethod
    def _split(value: Optional[str]) -> List[str]:
        if not value:
            return []
        return [v.strip().strip('"') for v in re.split('[;,]', value)]

    @staticmethod
    def _at(values: List[str], index: int) -> str:
        return values[index] if 0 <= index < len(values) else ''

    @staticmethod
    def _clean_temp(value: str) -> str:
        try:
            temp = float(value)
        except ValueError:
            return ''
        return f"{temp:g}"

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        self._reset_state()

        slicer_config: Optional[Dict[str, str]] = context.get_metadata('slicer_config')
        if slicer_config is None:
            self.logger.warning("tool_summary: No slicer data - list token_replacer before tool_summary "
                                "in [gcode_preprocessor] processors")
            return True

        tools = sorted(context.get_metadata('tools_used', []) or [])
        if not tools or not self.summary_macro:
            self.logger.info("tool_summary: No tool changes found - no summary inserted")
            return True

        temperatures = context.get_metadata('temperatures', []) or []
        colors = context.get_metadata('colors', []) or []
        first_layer = []
        for key in FIRST_LAYER_KEYS:
            if key in slicer_config:
                first_layer = self._split(slicer_config[key])
                break

        temps, first_temps, hex_colors = [], [], []
        for tool in tools:
            temps.append(self._clean_temp(self._at(temperatures, tool)))
            first_temps.append(self._clean_temp(self._at(first_layer, tool)))
            color = self._at(colors, tool).lstrip('#')
            hex_colors.append(color.upper() if HEX_COLOR.match(color) else '')

        # Values contain no spaces, ';' or '#', so Klipper passes them unchanged
        self.summary_line = (f"{self.summary_macro} TOOLS={','.join(map(str, tools))} "
                             f"TEMPS={','.join(temps)} FIRST_TEMPS={','.join(first_temps)} "
                             f"COLORS={','.join(hex_colors)}\n")
        return True

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        if self.inserted or self.summary_line is None or GcodePatterns.is_comment(line):
            return [line]
        command, _ = GcodePatterns.strip_comment(line)
        words = command.split(None, 1)
        if words and words[0].upper() in self.print_start_macros:
            self.inserted = True
            self.logger.info(f"tool_summary: Inserted tool summary before line {context.current_line}")
            return [self.summary_line, line]
        return [line]

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        if self.summary_line is not None and not self.inserted:
            self.logger.warning(f"tool_summary: None of {sorted(self.print_start_macros)} found - "
                                f"no summary inserted (see print_start_macros)")
        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return ToolSummary(config, logger)
