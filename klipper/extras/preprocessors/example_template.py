# Example Preprocessor Template
# Use this as a starting point for creating your own custom G-code preprocessor

from typing import List

# Import the base classes. Klipper loads processors as
# extras.preprocessors.<name>, the Moonraker script imports them with the
# extras directory on sys.path, so support both.
try:
    from ..gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities
    )
except ImportError:
    from gcode_preprocessor_base import (
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns,
        PreprocessorUtilities
    )


class ExampleProcessor(GcodePreprocessorPlugin):
    """
    Example preprocessor that demonstrates the basic structure.

    This example counts tool changes and adds a summary comment at the end.
    """

    def __init__(self, config, logger):
        """
        Initialize the processor.

        Args:
            config: Config object with .get(), .getboolean(), .getint() methods
            logger: Logger instance for output
        """
        # REQUIRED: Call parent constructor
        super().__init__(config, logger)

        # Load your custom configuration options here
        # Use config.get(key, default) for strings
        # Use config.getboolean(key, default) for booleans
        # Use config.getint(key, default) for integers
        # Use config.getfloat(key, default) for floats

        self.add_summary = config.getboolean('add_summary', True)
        self.summary_prefix = config.get('summary_prefix', 'PROCESSOR NOTE')

        # Initialize your processor's internal state here
        self.tool_changes = 0
        self.tools_seen = set()

    # ========================================================================
    # REQUIRED METHODS - You must implement these
    # ========================================================================

    def get_name(self) -> str:
        """
        REQUIRED: Return the unique name of this processor.
        This name is used for logging and identification.
        """
        return "example_processor"

    def get_description(self) -> str:
        """
        REQUIRED: Return a human-readable description.
        This is shown when users run LIST_GCODE_PROCESSORS command.
        """
        return "Example processor that counts tool changes"

    def process_line(self, line: str, context: PreprocessorContext) -> List[str]:
        """
        REQUIRED: Process a single line of G-code.

        This method is called for EVERY line in the G-code file.
        It runs AFTER pre_process() and BEFORE post_process().

        Args:
            line: The G-code line to process (includes newline character)
            context: Context object with metadata and state

        Returns:
            List of lines to output:
            - Return [line] to keep the line unchanged
            - Return [] to skip/remove the line
            - Return [line1, line2, ...] to insert multiple lines
            - Lines should include \n if you want newlines
        """
        # Example: Detect tool changes
        tool_number = GcodePatterns.extract_tool_number(line)
        if tool_number is not None:
            self.tool_changes += 1
            self.tools_seen.add(tool_number)
            self.logger.info(f"{self.get_name()}: Detected tool change to T{tool_number}")

        # Return the line unchanged
        # You could also modify it or insert additional lines before/after
        return [line]

    # ========================================================================
    # OPTIONAL METHODS - Override these if you need them
    # ========================================================================

    def can_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        OPTIONAL: Determine if this processor should run on the given file.

        This is called BEFORE pre_process(). Use it to skip files based on
        filename, extension, or other criteria.

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if this processor should run, False to skip
        """
        # Default implementation returns True
        # You can add conditions to skip certain files

        # Example: Only process files with certain names
        # if 'multi_tool' not in context.filename.lower():
        #     return False

        return True

    def pre_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        OPTIONAL: Initial pass through the file before line-by-line processing.

        This is called ONCE before process_line() is called for any lines.
        Use this to:
        - Scan the entire file to gather information
        - Build lookup tables or maps
        - Extract metadata
        - Share data with other processors via context.set_metadata()

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if successful, False to abort processing
        """
        self.logger.info(f"{self.get_name()}: Pre-processing file")

        # Reset state for this file
        self.tool_changes = 0
        self.tools_seen.clear()

        # Example: Read entire file to gather statistics
        # for line in PreprocessorUtilities.iter_file_lines(file_path):
        #     # Analyze lines here
        #     pass

        # Example: Share data with other processors
        # context.set_metadata('my_data', some_value)

        # Example: Use data from other processors (like metadata_extractor)
        # slicer = context.get_metadata('slicer', 'unknown')
        # self.logger.info(f"{self.get_name()}: Detected slicer: {slicer}")

        return True

    def post_process(self, file_path: str, context: PreprocessorContext) -> bool:
        """
        OPTIONAL: Final pass after all line processing is complete.

        This is called ONCE after process_line() has been called for all lines.
        Use this to:
        - Add summary information
        - Perform validation
        - Log statistics
        - Clean up state

        Note: You cannot modify the file here - the output has already been
        written. This is only for logging and validation.

        Args:
            file_path: Path to the G-code file
            context: Preprocessing context

        Returns:
            True if successful, False to mark processing as failed
        """
        self.logger.info(f"{self.get_name()}: Post-processing complete")
        self.logger.info(f"{self.get_name()}: Detected {self.tool_changes} tool changes")
        self.logger.info(f"{self.get_name()}: Tools used: {sorted(self.tools_seen)}")

        # Example: Store results in context for other processors
        context.set_metadata('example_tool_changes', self.tool_changes)

        return True


# ============================================================================
# REQUIRED: Factory function to create processor instance
# ============================================================================

def create_processor(config, logger):
    """
    REQUIRED: Factory function called by the preprocessor system.

    This function MUST be present and MUST be named exactly 'create_processor'.

    Args:
        config: Config object for this processor
        logger: Logger instance

    Returns:
        Instance of your processor class
    """
    return ExampleProcessor(config, logger)


# ============================================================================
# HELPFUL UTILITIES AND PATTERNS
# ============================================================================

"""
COMMON PATTERNS:

1. Skip comment lines:
   if GcodePatterns.is_comment(line):
       return [line]

2. Skip empty lines:
   if GcodePatterns.is_empty(line):
       return [line]

3. Split command and comment:
   command, comment = GcodePatterns.strip_comment(line)
   # Process command, then recombine
   return [command + comment]

4. Detect tool changes:
   tool_number = GcodePatterns.extract_tool_number(line)
   if tool_number is not None:
       # This is a tool change to tool_number

5. Insert a line before current line:
   return ["M104 S200\n", line]

6. Insert a line after current line:
   return [line, "; Comment added\n"]

7. Replace a line:
   return ["G28 ; Modified command\n"]

8. Remove a line:
   return []

9. Read configuration:
   self.enabled = config.getboolean('enabled', True)
   self.threshold = config.getint('threshold', 100)
   self.text = config.get('custom_text', 'default')

10. Access context data:
    tools_used = context.get_metadata('tools_used', [])
    slicer = context.get_metadata('slicer', 'unknown')

11. Share data with other processors:
    context.set_metadata('my_key', my_value)

12. Check current line number:
    if context.current_line == 0:
        # First line of file

13. Format tool temperature command:
    cmd = PreprocessorUtilities.format_tool_temp_command(tool=1, temp=200)
    # Returns: "M104 T1 S200\n"
"""
