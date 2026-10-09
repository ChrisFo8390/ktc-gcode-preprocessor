# Token Replacer Preprocessor
# Extracts slicer metadata from G-code comments and replaces token placeholders

from typing import Dict, List, Optional
import re

try:
    # Loaded by Klipper as extras.preprocessors.token_replacer
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


class TokenReplacer(GcodePreprocessorPlugin):
    """
    Processor that extracts metadata from slicer-generated comments
    including colors, materials, temperatures, and replaces token placeholders
    """

    SUPPORTED_SLICERS = ['PrusaSlicer', 'SuperSlicer', 'OrcaSlicer', 'BambuStudio']

    def __init__(self, config, logger):
        super().__init__(config, logger)

        # Configuration options - Metadata extraction
        self.extract_tools = config.getboolean('extract_tools', True)
        self.extract_colors = config.getboolean('extract_colors', True)
        self.extract_materials = config.getboolean('extract_materials', True)
        self.extract_temperatures = config.getboolean('extract_temperatures', True)
        self.extract_purge_volumes = config.getboolean('extract_purge_volumes', False)
        self.extract_filament_names = config.getboolean('extract_filament_names', False)
        self.extract_slicer_config = config.getboolean('extract_slicer_config', True)

        # Configuration options - Placeholder replacement
        self.replace_placeholders = config.getboolean('replace_placeholders', True)
        # Klipper truncates a command at ';' and treats '#' as a comment in
        # extended commands, so multi-value slicer settings like
        # "PLA;PETG" or "#FF0000;#00FF00" would silently cut the line.
        self.sanitize_generic_values = config.getboolean('sanitize_generic_values', True)

        self._reset_state()

    def _reset_state(self):
        """Reset all per-file state (the processor instance is reused)"""
        self.slicer: Optional[str] = None
        self.colors: List[str] = []
        self.materials: List[str] = []
        self.temperatures: List[str] = []
        self.purge_volumes: List[str] = []
        self.filament_names: List[str] = []
        self.slicer_config: Dict[str, str] = {}
        self.tools_used: set = set()
        self.total_toolchanges: int = 0

        # Flags to track if we've found metadata
        self.found_colors = False
        self.found_materials = False
        self.found_temperatures = False
        self.found_purge_volumes = False
        self.found_filament_names = False

        # Placeholder replacement map
        self.replacement_map: Dict[str, str] = {}

    def get_name(self) -> str:
        return "token_replacer"

    def get_description(self) -> str:
        return "Extracts slicer metadata and replaces token placeholders (!tool_count!, !colors!, !!any_slicer_key!!, etc.)"

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Scan file to extract all metadata from comments
        """
        self.logger.info(f"token_replacer: Scanning file for metadata")

        self._reset_state()

        for line in PreprocessorUtilities.iter_file_lines(file_path):
            # Extract all slicer config key=value pairs for generic replacement
            if self.extract_slicer_config and GcodePatterns.is_comment(line):
                match = GcodePatterns.SLICER_CONFIG.match(line)
                if match:
                    key = match.group(1).strip()
                    value = match.group(2).strip()
                    # Only store first occurrence of each key
                    if key not in self.slicer_config:
                        self.slicer_config[key] = value

            # Detect slicer
            if not self.slicer and GcodePatterns.is_comment(line):
                match = GcodePatterns.SLICER_NAME.match(line)
                if match:
                    self.slicer = match.group(1) or match.group(2)
                    if self.slicer in self.SUPPORTED_SLICERS:
                        self.logger.info(f"token_replacer: Detected slicer: {self.slicer}")

            # Extract tool changes if enabled
            if self.extract_tools:
                tool_number = GcodePatterns.extract_tool_number(line)
                if tool_number is not None:
                    self.tools_used.add(tool_number)
                    self.total_toolchanges += 1

            # Extract colors if enabled and not found yet
            if self.extract_colors and not self.found_colors and GcodePatterns.is_comment(line):
                match = GcodePatterns.EXTRUDER_COLOR.match(line)
                if match:
                    colors_csv = PreprocessorUtilities.parse_csv_list(match.group(1), '#')
                    if not self.colors:
                        self.colors.extend(colors_csv)
                    else:
                        # Merge, preferring non-empty values
                        self.colors = [n if o == '' else o for o, n in zip(self.colors, colors_csv)]
                    self.found_colors = all(len(c) > 0 for c in self.colors)

            # Extract materials if enabled and not found yet
            if self.extract_materials and not self.found_materials and GcodePatterns.is_comment(line):
                match = GcodePatterns.FILAMENT_TYPE.match(line)
                if match:
                    materials_csv = match.group(1).strip().split(';')
                    self.materials.extend([m.strip() for m in materials_csv])
                    self.found_materials = True

            # Extract temperatures if enabled and not found yet
            if self.extract_temperatures and not self.found_temperatures and GcodePatterns.is_comment(line):
                match = GcodePatterns.TEMPERATURE.match(line)
                if match:
                    temps_csv = re.split('[;,]', match.group(1).strip())
                    self.temperatures.extend([t.strip() for t in temps_csv])
                    self.found_temperatures = True

            # Extract purge volumes if enabled and not found yet
            if self.extract_purge_volumes and not self.found_purge_volumes and GcodePatterns.is_comment(line):
                match = GcodePatterns.PURGE_VOLUMES.match(line)
                if match:
                    purge_csv = match.group(1).strip().split(',')
                    self.purge_volumes.extend([p.strip() for p in purge_csv])
                    self.found_purge_volumes = True

            # Extract filament names if enabled and not found yet
            if self.extract_filament_names and not self.found_filament_names and GcodePatterns.is_comment(line):
                match = GcodePatterns.FILAMENT_NAMES.match(line)
                if match:
                    names_csv = re.split('[;,]', match.group(1).strip())
                    self.filament_names.extend([n.strip().strip('"') for n in names_csv])
                    self.found_filament_names = True

        # Log what we found
        self.logger.info(f"token_replacer: Slicer: {self.slicer}")
        if self.extract_tools:
            self.logger.info(f"token_replacer: Tools used: {sorted(self.tools_used)}")
            self.logger.info(f"token_replacer: Total tool changes: {self.total_toolchanges}")
        if self.extract_colors:
            self.logger.info(f"token_replacer: Colors: {self.colors}")
        if self.extract_materials:
            self.logger.info(f"token_replacer: Materials: {self.materials}")
        if self.extract_temperatures:
            self.logger.info(f"token_replacer: Temperatures: {self.temperatures}")
        if self.extract_slicer_config:
            self.logger.info(f"token_replacer: Slicer config entries: {len(self.slicer_config)}")

        # Store metadata in context for other processors
        context.set_metadata('slicer', self.slicer)
        context.set_metadata('tools_used', sorted(self.tools_used))
        context.set_metadata('total_toolchanges', self.total_toolchanges)
        context.set_metadata('colors', self.colors)
        context.set_metadata('materials', self.materials)
        context.set_metadata('temperatures', self.temperatures)
        context.set_metadata('purge_volumes', self.purge_volumes)
        context.set_metadata('filament_names', self.filament_names)
        context.set_metadata('slicer_config', self.slicer_config)

        # Build replacement map for placeholder substitution
        if self.replace_placeholders:
            tools_list = sorted(self.tools_used)
            self.replacement_map = {
                '!tool_count!': str(len(tools_list)) if tools_list else '0',
                '!tools!': ','.join(map(str, tools_list)) if tools_list else '0',
                '!referenced_tools!': ','.join(map(str, tools_list)) if tools_list else '0',
                '!total_toolchanges!': str(self.total_toolchanges),
                '!colors!': ','.join(self.colors) if self.colors else '',
                '!materials!': ','.join(self.materials) if self.materials else '',
                '!temperatures!': ','.join(self.temperatures) if self.temperatures else '',
                '!filament_names!': ','.join(self.filament_names) if self.filament_names else '',
            }
            self.logger.info(f"token_replacer: Built replacement map with {len(self.replacement_map)} placeholders")

        return True

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        """
        Replace placeholders in non-comment lines
        """
        # Don't process comment lines (preserve slicer metadata)
        if not self.replace_placeholders or GcodePatterns.is_comment(line):
            return [line]

        modified_line = line

        # Generic slicer config placeholders (!!key!!) first, so that a
        # built-in token like !tools! cannot match inside !!tools!!
        if self.extract_slicer_config and '!!' in modified_line:
            def replace_generic(match):
                key = match.group(1)
                if key in self.slicer_config:
                    value = self._generic_value(key)
                    self.logger.info(f"token_replacer: Replaced !!{key}!! with {value} at line {context.current_line}")
                    return value
                return match.group(0)  # Keep original if key not found
            modified_line = GcodePatterns.GENERIC_PLACEHOLDER.sub(replace_generic, modified_line)

        # Built-in placeholders (!key!), not touching unresolved !!key!!
        if '!' in modified_line:
            def replace_builtin(match):
                placeholder = match.group(0)
                replacement = self.replacement_map.get(placeholder)
                if replacement is None:
                    return placeholder
                self.logger.info(f"token_replacer: Replaced {placeholder} with {replacement} at line {context.current_line}")
                return replacement
            modified_line = GcodePatterns.PLACEHOLDER.sub(replace_builtin, modified_line)

        return [modified_line]

    def _generic_value(self, key: str) -> str:
        value = self.slicer_config[key]
        if not self.sanitize_generic_values:
            return value
        sanitized = value.replace(';', ',').replace('#', '')
        if sanitized != value:
            self.logger.info(f"token_replacer: Sanitized value of !!{key}!! ('{value}' -> '{sanitized}')")
        return sanitized

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Post-processing: Could add summary metadata as comments
        """
        return True


def create_processor(config, logger):
    """Factory function to create processor instance"""
    return TokenReplacer(config, logger)
