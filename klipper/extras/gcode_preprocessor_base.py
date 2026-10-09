# G-code Preprocessor Base Classes

import os
import re
from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional, Any, Tuple


FINGERPRINT = 'processed by ktc-gcode preprocessor'
# Fingerprints of earlier versions, still recognized so that files processed
# before the rename are not processed (and get cooldowns inserted) twice
LEGACY_FINGERPRINTS = ('processed by klipper-gcode-preprocessor',)

# surrogateescape keeps bytes that are not valid UTF-8 (e.g. object names
# written in a legacy codepage) and writes them back unchanged
FILE_ENCODING = 'utf-8'
FILE_ERRORS = 'surrogateescape'


class PreprocessorContext:
    """Context object passed to preprocessors containing file and printer state"""

    def __init__(self):
        self.file_path: str = ""
        self.filename: str = ""
        self.metadata: Dict[str, Any] = {}
        self.toolchanger_config: Dict[str, Any] = {}
        self.tools: List[int] = []
        self.current_line: int = 0
        self.total_lines: int = 0

    def set_metadata(self, key: str, value: Any):
        """Store metadata for other processors to use"""
        self.metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Retrieve metadata stored by other processors"""
        return self.metadata.get(key, default)


class ProcessorConfig:
    """
    Config helper for processors, backed by a dict of raw string values.

    Used by both the Klipper module and the Moonraker script so that a
    processor sees identical values regardless of how it was invoked.
    """

    TRUE_VALUES = ('1', 'yes', 'true', 'on')
    FALSE_VALUES = ('0', 'no', 'false', 'off')

    def __init__(self, section_name: str, values: Optional[Dict[str, str]] = None,
                 logger=None):
        self.section_name = section_name
        self.values = {k.lower(): v for k, v in (values or {}).items()}
        self.logger = logger

    def _warn(self, key, value, kind):
        if self.logger is not None:
            self.logger.warning(f"gcode_preprocessor: [{self.section_name}] "
                                f"invalid {kind} for '{key}': {value!r} - using default")

    def get(self, key, default=None):
        """Get a config value as string"""
        value = self.values.get(key.lower())
        if value is None:
            return default
        return value.strip()

    def getboolean(self, key, default=False):
        """Get a config value as boolean"""
        value = self.get(key, None)
        if value is None or value == '':
            return default
        value_str = value.lower()
        if value_str in self.TRUE_VALUES:
            return True
        if value_str in self.FALSE_VALUES:
            return False
        self._warn(key, value, 'boolean')
        return default

    def getint(self, key, default=0):
        """Get a config value as int"""
        value = self.get(key, None)
        if value is None or value == '':
            return default
        try:
            return int(value)
        except ValueError:
            self._warn(key, value, 'integer')
            return default

    def getfloat(self, key, default=0.0):
        """Get a config value as float"""
        value = self.get(key, None)
        if value is None or value == '':
            return default
        try:
            return float(value)
        except ValueError:
            self._warn(key, value, 'number')
            return default


class GcodePreprocessorPlugin(ABC):
    """Abstract base class for G-code preprocessor plugins"""

    def __init__(self, config, logger):
        """
        Initialize the preprocessor plugin

        Args:
            config: ProcessorConfig for this processor
            logger: Logger instance for output
        """
        self.config = config
        self.logger = logger
        self.name = self.__class__.__name__

    @abstractmethod
    def get_name(self) -> str:
        """Return the name of this processor"""
        pass

    @abstractmethod
    def get_description(self) -> str:
        """Return a description of what this processor does"""
        pass

    def can_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Determine if this processor should run on the given file

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if this processor should run, False otherwise
        """
        return True

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Initial pass through the file before line-by-line processing
        Use this to gather metadata, build usage maps, etc.

        Processor instances are reused for every file, so all per-file
        state must be reset here.

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if successful, False on error
        """
        return True

    @abstractmethod
    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        """
        Process a single line of G-code

        Args:
            line: The G-code line to process
            context: Preprocessing context

        Returns:
            List of lines to output (can be empty, one, or multiple lines)
            Return [line] to keep the line unchanged
        """
        pass

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        Final pass after all line processing is complete
        Use this for finalization, summary generation, etc.

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if successful, False on error
        """
        return True


class GcodePatterns:
    """Common regex patterns for G-code parsing"""

    # Tool change patterns
    T_COMMAND = re.compile(r'^T(\d+)\s*(?:;.*)?$', re.IGNORECASE)
    SELECT_TOOL = re.compile(r'^SELECT_TOOL\s+(?:.*\s)?(?:TOOL|T)=(\S+)', re.IGNORECASE)
    MMU_CHANGE_TOOL = re.compile(r'^MMU_CHANGE_TOOL(?:_STANDALONE)?\s+TOOL=(\d+)', re.IGNORECASE)
    TOOL_NAME_NUMBER = re.compile(r'^T?(\d+)$', re.IGNORECASE)

    # Temperature commands
    M104 = re.compile(r'^M104\s+(?:T(\d+)\s+)?S([\d.]+)', re.IGNORECASE)
    M109 = re.compile(r'^M109\s+(?:T(\d+)\s+)?S([\d.]+)', re.IGNORECASE)
    SET_TOOL_TEMP = re.compile(r'^SET_TOOL_TEMPERATURE\s+(?:TOOL=(\w+)|T=(\d+))\s+TARGET=([\d.]+)', re.IGNORECASE)

    # Movement commands
    G0_G1 = re.compile(r'^G[01]\s+', re.IGNORECASE)
    G0_G1_XY = re.compile(r'^G[01](?=.*\sX([-\d.]+))(?=.*\sY([-\d.]+)).*$', re.IGNORECASE)

    # Slicer metadata comments
    SLICER_NAME = re.compile(r'^;.*generated by ([a-z]*) .*$|^; (BambuStudio) .*$', re.IGNORECASE)
    EXTRUDER_COLOR = re.compile(r'^;\s*(?:extruder|filament)_colour\s*=\s*(#.*;*.*)$', re.IGNORECASE)
    FILAMENT_TYPE = re.compile(r'^;\s*filament_type\s*=\s*(.*)$', re.IGNORECASE)
    TEMPERATURE = re.compile(r'^;\s*(?:nozzle_)?temperature\s*=\s*(.*)$', re.IGNORECASE)
    PURGE_VOLUMES = re.compile(r'^;\s*(?:flush_volumes_matrix|wiping_volumes_matrix)\s*=\s*(.*)$', re.IGNORECASE)
    FILAMENT_NAMES = re.compile(r'^;\s*filament_settings_id\s*=\s*(.*)$', re.IGNORECASE)

    # Generic slicer config pattern: ; key = value
    SLICER_CONFIG = re.compile(r'^;\s*(\w+)\s*=\s*(.*)$')

    # Placeholder patterns
    PLACEHOLDER = re.compile(r'(?<!!)!(\w+)!(?!!)')
    GENERIC_PLACEHOLDER = re.compile(r'!!(\w+)!!')

    @staticmethod
    def is_comment(line: str) -> bool:
        """Check if a line is a comment"""
        return line.strip().startswith(';')

    @staticmethod
    def is_empty(line: str) -> bool:
        """Check if a line is empty or whitespace only"""
        return len(line.strip()) == 0

    @staticmethod
    def strip_comment(line: str) -> Tuple[str, str]:
        """
        Split a line into command and comment parts

        Returns:
            Tuple of (command, comment) where comment includes the ';'
        """
        if ';' in line:
            parts = line.split(';', 1)
            return parts[0].rstrip(), ';' + parts[1]
        return line.rstrip(), ''

    @staticmethod
    def extract_tool_number(line: str) -> Optional[int]:
        """
        Extract tool number from any tool change command

        Returns:
            Tool number or None if not a tool change command
        """
        # Try T command
        match = GcodePatterns.T_COMMAND.match(line)
        if match:
            return int(match.group(1))

        # Try SELECT_TOOL (TOOL=<n>, TOOL=T<n> or T=<n>)
        command, _ = GcodePatterns.strip_comment(line)
        match = GcodePatterns.SELECT_TOOL.match(command)
        if match:
            number = GcodePatterns.TOOL_NAME_NUMBER.match(match.group(1))
            if number:
                return int(number.group(1))
            return None

        # Try MMU_CHANGE_TOOL
        match = GcodePatterns.MMU_CHANGE_TOOL.match(line)
        if match:
            return int(match.group(1))

        return None


class PreprocessorUtilities:
    """Utility functions for preprocessors"""

    @staticmethod
    def iter_file_lines(file_path: str) -> Iterator[str]:
        """Iterate over the lines of a file without loading it into memory"""
        with open(file_path, 'r', encoding=FILE_ENCODING, errors=FILE_ERRORS) as f:
            for line in f:
                yield line

    @staticmethod
    def read_file_lines(file_path: str) -> List[str]:
        """Read all lines from a file, preserving line endings"""
        with open(file_path, 'r', encoding=FILE_ENCODING, errors=FILE_ERRORS) as f:
            return f.readlines()

    @staticmethod
    def write_file_lines(file_path: str, lines: List[str]):
        """Write lines to a file"""
        with open(file_path, 'w', encoding=FILE_ENCODING, errors=FILE_ERRORS) as f:
            f.writelines(lines)

    @staticmethod
    def format_tool_temp_command(tool_number: int, temperature: float) -> str:
        """Format a tool temperature command"""
        return f"M104 T{tool_number} S{temperature}\n"

    @staticmethod
    def parse_csv_list(csv_string: str, strip_chars: str = '#') -> List[str]:
        """Parse a semicolon or comma separated list"""
        items = []
        for separator in [';', ',']:
            if separator in csv_string:
                items = [item.strip().lstrip(strip_chars) for item in csv_string.split(separator)]
                break
        return items if items else [csv_string.strip().lstrip(strip_chars)]

    @staticmethod
    def add_fingerprint(slicer: Optional[str] = None) -> str:
        """Generate a fingerprint comment for preprocessed files"""
        if slicer:
            return f"; {FINGERPRINT} (slicer: {slicer})\n"
        return f"; {FINGERPRINT}\n"

    @staticmethod
    def is_already_processed(file_path: str) -> bool:
        """Check if file was already processed by looking for fingerprint"""
        try:
            with open(file_path, 'r', encoding=FILE_ENCODING, errors=FILE_ERRORS) as f:
                first_line = f.readline()
            return any(fp in first_line for fp in (FINGERPRINT,) + LEGACY_FINGERPRINTS)
        except OSError:
            return False


def run_pipeline(file_path: str, processors: List[GcodePreprocessorPlugin],
                 context: PreprocessorContext, logger) -> Dict[str, Any]:
    """
    Run the three-pass pipeline on a file and replace it atomically.

    Shared by the Klipper module and the Moonraker script. The output is
    streamed to a temp file, so memory use does not grow with file size.
    """
    if PreprocessorUtilities.is_already_processed(file_path):
        return {'success': True, 'processed': False, 'message': 'File already preprocessed'}

    active_processors = [p for p in processors if p.can_process(file_path, context)]
    if not active_processors:
        return {'success': True, 'processed': False, 'message': 'No processors applicable'}

    logger.info(f"gcode_preprocessor: Processing '{context.filename}' with "
                f"{len(active_processors)} processors")

    # Pass 1: Pre-processing (metadata gathering)
    for processor in active_processors:
        if not processor.pre_process(file_path, context):
            return {'success': False, 'processed': False,
                    'message': f'Pre-processing failed in {processor.get_name()}'}

    # Pass 2: Line-by-line processing, streamed to a temp file
    temp_path = file_path + '.preprocessing'
    try:
        with open(temp_path, 'w', encoding=FILE_ENCODING, errors=FILE_ERRORS) as out:
            out.write(PreprocessorUtilities.add_fingerprint(context.get_metadata('slicer')))
            line_count = 0
            for line_num, line in enumerate(PreprocessorUtilities.iter_file_lines(file_path)):
                context.current_line = line_num
                processed_lines = [line]
                for processor in active_processors:
                    new_lines = []
                    for proc_line in processed_lines:
                        new_lines.extend(processor.process_line(proc_line, context))
                    processed_lines = new_lines
                out.writelines(processed_lines)
                line_count += 1
            context.total_lines = line_count

        # Pass 3: Post-processing
        for processor in active_processors:
            if not processor.post_process(file_path, context):
                os.remove(temp_path)
                return {'success': False, 'processed': False,
                        'message': f'Post-processing failed in {processor.get_name()}'}

        # Atomic replacement
        os.replace(temp_path, file_path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    logger.info(f"gcode_preprocessor: Successfully processed '{context.filename}'")
    return {
        'success': True,
        'processed': True,
        'message': f'Processed by {len(active_processors)} processors',
        'processors': [p.get_name() for p in active_processors],
        'metadata': context.metadata
    }
