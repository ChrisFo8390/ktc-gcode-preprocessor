# G-code Preprocessor System

import os
import logging
import importlib
import multiprocessing
import traceback
from typing import Dict, List, Optional, Any
from . import gcode_preprocessor_base


class PreprocessorConfigSection:
    """Holds the options of a [gcode_preprocessor <name>] section"""
    def __init__(self, config):
        self.name = config.get_name()
        # Read every option so Klipper accepts processor specific settings
        # (including those of custom processors) without a config error
        self.values = {option: config.get(option)
                       for option in config.get_prefix_options('')}

    def get_status(self, eventtime):
        return {'options': dict(self.values)}


class GcodePreprocessor:
    """Main G-code preprocessor that manages and orchestrates plugins"""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.config = config
        self.name = config.get_name()

        # Load configuration
        self.enabled = config.getboolean('enabled', True)
        self.processors_list = config.get('processors', '').split(',')
        self.processors_list = [p.strip() for p in self.processors_list if p.strip()]

        # Registry of available processors
        self.processors: List[gcode_preprocessor_base.GcodePreprocessorPlugin] = []

        # Register for klippy events
        self.printer.register_event_handler("klippy:connect", self._handle_connect)

        self.gcode.register_command("PREPROCESS_GCODE_FILE",
                                    self.cmd_PREPROCESS_GCODE_FILE,
                                    desc=self.cmd_PREPROCESS_GCODE_FILE_help)
        self.gcode.register_command("LIST_GCODE_PROCESSORS",
                                    self.cmd_LIST_GCODE_PROCESSORS,
                                    desc=self.cmd_LIST_GCODE_PROCESSORS_help)

    def _handle_connect(self):
        """Handle Klipper connect event"""
        # Load processors
        if self.enabled:
            self._load_processors()

    def _processor_config(self, processor_name: str):
        """Build the config helper from the [gcode_preprocessor <name>] section"""
        section_name = f"gcode_preprocessor {processor_name}"
        section = self.printer.lookup_object(section_name, None)
        values = section.values if section is not None else {}
        return gcode_preprocessor_base.ProcessorConfig(section_name, values, logging)

    def _load_processors(self):
        """Load and initialize all configured processors"""
        for index, processor_name in enumerate(self.processors_list):
            try:
                try:
                    module = importlib.import_module(f".preprocessors.{processor_name}", package="extras")
                except ImportError as e:
                    logging.warning(f"gcode_preprocessor: Could not load processor '{processor_name}': {e}")
                    continue

                factory = getattr(module, 'create_processor', None)
                if factory:
                    processor = factory(self._processor_config(processor_name), logging)
                    self.processors.append(processor)
                    logging.info(f"gcode_preprocessor: Loaded processor '{processor.get_name()}' (order: {index + 1})")
                else:
                    logging.warning(f"gcode_preprocessor: Processor module '{processor_name}' missing create_processor() function")

            except Exception as e:
                logging.error(f"gcode_preprocessor: Error loading processor '{processor_name}': {e}")
                logging.error(traceback.format_exc())

        # Processors are already in the correct order based on list position
        logging.info(f"gcode_preprocessor: Loaded {len(self.processors)} processors")

    def _build_context(self, file_path: str):
        context = gcode_preprocessor_base.PreprocessorContext()
        context.file_path = file_path
        context.filename = os.path.basename(file_path)

        # Get toolchanger if available
        toolchanger = self.printer.lookup_object('toolchanger', None)
        if toolchanger is not None and hasattr(toolchanger, 'get_status'):
            try:
                status = toolchanger.get_status(self.printer.get_reactor().monotonic())
                context.toolchanger_config = dict(status)
                context.tools = list(status.get('tool_numbers', []))
            except Exception:
                logging.exception("gcode_preprocessor: Could not read toolchanger status")
        return context

    def _run_pipeline(self, file_path: str, context) -> Dict[str, Any]:
        try:
            return gcode_preprocessor_base.run_pipeline(file_path, self.processors, context, logging)
        except Exception as e:
            logging.error(f"gcode_preprocessor: Error processing file: {e}")
            logging.error(traceback.format_exc())
            return {'success': False, 'processed': False, 'message': str(e)}

    def _run_in_background(self, file_path: str, context) -> Dict[str, Any]:
        """
        Run the pipeline in a forked child process while the reactor keeps
        running, so heaters and motion are not starved (same approach as
        Klipper's shaper_calibrate).
        """
        mp = multiprocessing.get_context('fork')
        parent_conn, child_conn = mp.Pipe()

        def wrapper():
            try:
                import queuelogger
                queuelogger.clear_bg_logging()
            except Exception:
                pass
            try:
                child_conn.send(self._run_pipeline(file_path, context))
            except Exception:
                child_conn.send({'success': False, 'processed': False,
                                 'message': traceback.format_exc()})
            child_conn.close()

        proc = mp.Process(target=wrapper)
        proc.daemon = True
        proc.start()

        reactor = self.printer.get_reactor()
        eventtime = last_report_time = reactor.monotonic()
        while proc.is_alive() and not parent_conn.poll():
            if eventtime > last_report_time + 5.:
                last_report_time = eventtime
                self.gcode.respond_info("Preprocessing G-code file...", log=False)
            eventtime = reactor.pause(eventtime + .1)

        if parent_conn.poll():
            result = parent_conn.recv()
        else:
            result = {'success': False, 'processed': False,
                      'message': f'Preprocessing process exited with code {proc.exitcode}'}
        proc.join(1.)
        return result

    def process_file(self, file_path: str) -> Dict[str, Any]:
        """
        Process a G-code file through all enabled processors

        Args:
            file_path: Path to the G-code file to process

        Returns:
            Dictionary with processing results and metadata
        """
        if not self.enabled:
            return {'success': True, 'processed': False, 'message': 'Preprocessor disabled'}

        if not os.path.exists(file_path):
            return {'success': False, 'processed': False, 'message': f'File not found: {file_path}'}

        if gcode_preprocessor_base.PreprocessorUtilities.is_already_processed(file_path):
            return {'success': True, 'processed': False, 'message': 'File already preprocessed'}

        return self._run_in_background(file_path, self._build_context(file_path))

    def _is_printing(self) -> bool:
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats is None:
            return False
        state = print_stats.get_status(self.printer.get_reactor().monotonic()).get('state')
        return state in ('printing', 'paused')

    cmd_PREPROCESS_GCODE_FILE_help = "Manually preprocess a G-code file"
    def cmd_PREPROCESS_GCODE_FILE(self, gcmd):
        """Command to manually trigger file preprocessing"""
        file_path = gcmd.get('FILE', None)

        if not file_path:
            gcmd.respond_info("Usage: PREPROCESS_GCODE_FILE FILE=<path> [FORCE=1]")
            return

        if self._is_printing() and not gcmd.get_int('FORCE', 0):
            raise gcmd.error("PREPROCESS_GCODE_FILE is not allowed while a print "
                             "is running or paused (use FORCE=1 to override)")

        result = self.process_file(os.path.expanduser(file_path))

        if result['success']:
            if result['processed']:
                gcmd.respond_info(f"File preprocessed successfully: {result['message']}")
                if 'processors' in result:
                    gcmd.respond_info(f"Processors used: {', '.join(result['processors'])}")
            else:
                gcmd.respond_info(f"File not processed: {result['message']}")
        else:
            raise gcmd.error(f"Error preprocessing file: {result['message']}")

    cmd_LIST_GCODE_PROCESSORS_help = "List available G-code processors"
    def cmd_LIST_GCODE_PROCESSORS(self, gcmd):
        """Command to list all loaded processors"""
        if not self.processors:
            gcmd.respond_info("No processors loaded")
            return

        lines = [f"Loaded {len(self.processors)} processors:"]
        for i, proc in enumerate(self.processors, 1):
            lines.append(f"  {i}. {proc.get_name()}")
            lines.append(f"     {proc.get_description()}")
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        """Return status for queries"""
        return {
            'enabled': self.enabled,
            'processors': [
                {
                    'name': p.get_name(),
                    'description': p.get_description()
                }
                for p in self.processors
            ]
        }


def load_config(config):
    return GcodePreprocessor(config)

def load_config_prefix(config):
    """Allow [gcode_preprocessor ...] config sections to be defined"""
    return PreprocessorConfigSection(config)
