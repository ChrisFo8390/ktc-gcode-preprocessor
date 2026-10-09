# Klipper G-code Preprocessor - Moonraker Component
# Hooks into Moonraker's file upload/metadata system to preprocess G-code files
#
# Two integration modes:
#   1. GCode processor API (Moonraker versions that provide
#      MetadataStorage.register_gcode_processor): Moonraker runs this script
#      with --gcode-file before extracting metadata, with its own timeout.
#   2. Fallback for older Moonraker versions: METADATA_SCRIPT is replaced by
#      this script, which preprocesses the file and then runs Moonraker's
#      metadata extraction. Here the preprocessing has to finish within
#      [file_manager] default_metadata_parser_timeout (20 s by default).
#
# While no print is running, the console shows when preprocessing of an
# uploaded file starts and whether it succeeded (console_messages option).

import os
import re
import sys
import logging

DEFAULT_CONFIG_PATH = '~/printer_data/config/gcode-preprocessor/preprocessor.cfg'
DEFAULT_TIMEOUT = 600.
# Current and legacy fingerprint (see gcode_preprocessor_base.FINGERPRINT)
FINGERPRINT_REGEX = r"^; processed by (?:ktc-gcode preprocessor|klipper-gcode-preprocessor)"
GCODE_EXTENSIONS = ('.gcode', '.g', '.gco')
DEFAULT_MESSAGE_START = "KTC preprocessing active"
DEFAULT_MESSAGE_SUCCESS = "preprocessing successful"
DEFAULT_MESSAGE_FAILED = "!! KTC preprocessing failed - see moonraker.log"


class GcodePreprocessor:
    """
    Moonraker component that integrates G-code preprocessing into file upload
    """

    def __init__(self, config):
        self.config = config
        self.server = config.get_server()

        # Configuration
        self.enabled = config.getboolean('enable_preprocessing', True)
        self.config_path = os.path.expanduser(config.get('config_path', DEFAULT_CONFIG_PATH))
        self.timeout = config.getfloat('timeout', DEFAULT_TIMEOUT, above=0.)

        # Console feedback while the printer is idle
        self.console_messages = config.getboolean('console_messages', True)
        self.message_start = config.get('message_start', DEFAULT_MESSAGE_START)
        self.message_success = config.get('message_success', DEFAULT_MESSAGE_SUCCESS)
        self.message_failed = config.get('message_failed', DEFAULT_MESSAGE_FAILED)

        # Setup the preprocessor hook
        if self.enabled:
            self.setup_preprocessor_hook()
            if self.console_messages:
                self._setup_console_messages()

        logging.info("gcode_preprocessor: Component initialized")

    def _script_command(self):
        return [sys.executable, os.path.abspath(__file__), "-C", self.config_path]

    def setup_preprocessor_hook(self):
        """
        Setup the preprocessor to run on file uploads
        """
        if self._register_gcode_processor():
            return
        self._patch_metadata_script()

    def _register_gcode_processor(self) -> bool:
        try:
            file_manager = self.server.lookup_component('file_manager')
            mdst = file_manager.get_metadata_storage()
            register = mdst.register_gcode_processor
        except Exception:
            return False
        proc_config = {
            "name": "klipper_gcode_preprocessor",
            "command": self._script_command() + ["--gcode-file", "{gcode_file_path}"],
            "timeout": self.timeout,
            "ident": {
                "regex": FINGERPRINT_REGEX,
                "location": "header"
            },
            "enabled": True
        }
        try:
            register("klipper_gcode_preprocessor", proc_config)
        except Exception:
            logging.exception("gcode_preprocessor: Failed to register GCode processor")
            return False
        logging.info("gcode_preprocessor: Registered as Moonraker GCode processor "
                     f"(timeout {self.timeout:.0f} s)")
        return True

    def _patch_metadata_script(self):
        # Set the metadata script to this file
        # When Moonraker processes uploaded files, it will call this script
        try:
            from .file_manager import file_manager
        except ImportError:
            logging.warning("gcode_preprocessor: Could not import file_manager - preprocessing may not work")
            return
        file_manager.METADATA_SCRIPT = " ".join(
            [os.path.abspath(__file__), "-x", "-C", self.config_path])
        logging.info(f"gcode_preprocessor: Set METADATA_SCRIPT to {file_manager.METADATA_SCRIPT}")
        logging.warning("gcode_preprocessor: Using METADATA_SCRIPT fallback; preprocessing must finish "
                        "within [file_manager] default_metadata_parser_timeout")

    def _setup_console_messages(self):
        """
        Wrap Moonraker's metadata extraction, which runs the preprocessor in
        both integration modes, to report start and result on the console.
        _run_extract_metadata is internal to Moonraker (present since 2021);
        if it is missing, preprocessing still works, only without messages.
        """
        try:
            file_manager = self.server.lookup_component('file_manager')
            mdst = file_manager.get_metadata_storage()
            original = mdst._run_extract_metadata
        except Exception:
            logging.warning("gcode_preprocessor: Metadata hook not found - console messages disabled")
            return

        async def run_extract_metadata(filename, *args, **kwargs):
            ufp_path = args[0] if args else kwargs.get('ufp_path')
            file_path = os.path.join(mdst.gc_path, filename)
            notify = await self._should_notify(file_path, ufp_path)
            if notify:
                self._console(self.message_start)
            try:
                await original(filename, *args, **kwargs)
            except BaseException:
                if notify:
                    self._console(self.message_failed)
                raise
            if notify:
                eventloop = self.server.get_event_loop()
                done = await eventloop.run_in_thread(file_has_fingerprint, file_path)
                self._console(self.message_success if done else self.message_failed)

        mdst._run_extract_metadata = run_extract_metadata

    async def _should_notify(self, file_path, ufp_path) -> bool:
        """True if the file will be preprocessed and no print is running"""
        try:
            eventloop = self.server.get_event_loop()
            pending = await eventloop.run_in_thread(
                will_preprocess, file_path, ufp_path, self.config_path)
            if not pending:
                return False
            return await self._printer_idle()
        except Exception:
            logging.exception("gcode_preprocessor: Could not determine console message state")
            return False

    async def _printer_idle(self) -> bool:
        """No print job running or paused (Klipper offline counts as idle)"""
        kapis = self.server.lookup_component('klippy_apis')
        status = await kapis.query_objects({'print_stats': ['state']}, default=None)
        state = ((status or {}).get('print_stats') or {}).get('state')
        return state not in ('printing', 'paused')

    def _console(self, message):
        # Delivered to Mainsail/Fluidd like a Klipper console response and
        # stored in Moonraker's gcode history; Klipper is not involved
        self.server.send_event("server:gcode_response", message)
        logging.info(f"gcode_preprocessor: {message}")

    async def component_init(self):
        """Initialize component after server is ready"""
        logging.info("gcode_preprocessor: Component initialization complete")


def load_component(config):
    return GcodePreprocessor(config)


# ==============================================================================
# Command-line interface, used when this script is invoked by Moonraker
# ==============================================================================

def _find_extras_path():
    """Locate the directory with gcode_preprocessor_base.py and preprocessors/"""
    candidates = [
        # Repository layout (this file is usually a symlink into the repo)
        os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'klipper', 'extras'),
        os.path.expanduser('~/klipper/klippy/extras'),
    ]
    for path in candidates:
        if os.path.isfile(os.path.join(path, 'gcode_preprocessor_base.py')):
            return path
    return None


def file_has_fingerprint(file_path) -> bool:
    try:
        with open(file_path, 'r', encoding='utf-8', errors='surrogateescape') as f:
            return re.match(FINGERPRINT_REGEX, f.readline()) is not None
    except OSError:
        return False


def will_preprocess(file_path, ufp_path, config_file) -> bool:
    """Predict whether preprocess_file() is going to change this file"""
    if not file_path.lower().endswith(GCODE_EXTENSIONS):
        return False
    if ufp_path is None:
        if not os.path.isfile(file_path) or file_has_fingerprint(file_path):
            return False
    if not os.path.isfile(config_file):
        return False
    parser = _read_klipper_style_config(config_file)
    section = 'gcode_preprocessor'
    if not parser.has_section(section):
        return False
    enabled = parser.get(section, 'enabled', fallback='true').strip().lower()
    if enabled in ('0', 'no', 'false', 'off'):
        return False
    processors = parser.get(section, 'processors', fallback='')
    return any(p.strip() for p in processors.split(','))


def _read_klipper_style_config(config_file):
    """Parse the config like Klipper does: '#' starts a comment anywhere"""
    import configparser
    parser = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    with open(config_file, 'r', encoding='utf-8') as f:
        lines = [line.split('#', 1)[0] for line in f.read().replace('\r\n', '\n').split('\n')]
    parser.read_string('\n'.join(lines), config_file)
    return parser


def preprocess_file(file_path, config_file):
    """Preprocess one file. Returns True on success (or nothing to do)."""
    if not file_path.lower().endswith(GCODE_EXTENSIONS):
        logging.info(f"gcode_preprocessor: Skipping non-G-code file: {file_path}")
        return True

    extras_path = _find_extras_path()
    if extras_path is None:
        logging.error("gcode_preprocessor: gcode_preprocessor_base.py not found")
        return False
    if extras_path not in sys.path:
        sys.path.append(extras_path)

    import importlib
    from gcode_preprocessor_base import (PreprocessorContext, PreprocessorUtilities,
                                         ProcessorConfig, run_pipeline)

    if PreprocessorUtilities.is_already_processed(file_path):
        logging.info(f"gcode_preprocessor: File already preprocessed: {file_path}")
        return True

    if not os.path.isfile(config_file):
        logging.error(f"gcode_preprocessor: Config file not found: {config_file}")
        return False
    parser = _read_klipper_style_config(config_file)

    main_section = 'gcode_preprocessor'
    main_config = ProcessorConfig(main_section,
                                  dict(parser.items(main_section)) if parser.has_section(main_section) else {},
                                  logging)
    if not main_config.getboolean('enabled', True):
        logging.info("gcode_preprocessor: Disabled in config")
        return True
    processors_list = [p.strip() for p in main_config.get('processors', '').split(',') if p.strip()]

    loaded_processors = []
    for proc_name in processors_list:
        try:
            module = importlib.import_module(f'preprocessors.{proc_name}')
            section_name = f'gcode_preprocessor {proc_name}'
            values = dict(parser.items(section_name)) if parser.has_section(section_name) else {}
            processor = module.create_processor(ProcessorConfig(section_name, values, logging), logging)
            loaded_processors.append(processor)
            logging.info(f"gcode_preprocessor: Loaded processor '{proc_name}'")
        except Exception as e:
            logging.warning(f"gcode_preprocessor: Failed to load processor '{proc_name}': {e}")

    if not loaded_processors:
        logging.warning("gcode_preprocessor: No processors loaded, skipping")
        return True

    context = PreprocessorContext()
    context.file_path = file_path
    context.filename = os.path.basename(file_path)

    result = run_pipeline(file_path, loaded_processors, context, logging)
    logging.info(f"gcode_preprocessor: {result['message']}")
    return result['success']


if __name__ == "__main__":
    import argparse
    import json
    import traceback

    # Log to stderr; stdout is reserved for Moonraker's metadata JSON
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(description="G-code Preprocessing Script")
    parser.add_argument("-c", "--config", metavar='<config_file>',
                        default=None,
                        help="Optional JSON configuration file (from Moonraker)")
    parser.add_argument("-f", "--filename", metavar='<filename>',
                        help="Name of G-code file to process")
    parser.add_argument("-p", "--path", metavar='<path>',
                        default=os.path.abspath(os.path.dirname(__file__)),
                        help="Optional absolute path for file")
    parser.add_argument("-u", "--ufp", metavar="<ufp file>",
                        default=None,
                        help="Optional path of UFP file to extract")
    parser.add_argument("-o", "--check-objects", dest='check_objects',
                        action='store_true',
                        help="Process G-code file for exclude object functionality")
    parser.add_argument("-x", "--preprocess", dest='preprocess',
                        action='store_true',
                        help="Enable preprocessing")
    parser.add_argument("-C", "--preprocessor-config", dest='preprocessor_config',
                        default=os.path.expanduser(DEFAULT_CONFIG_PATH),
                        help="Path of the preprocessor .cfg file")
    parser.add_argument("--gcode-file", dest='gcode_file', default=None,
                        help="Only preprocess this file (Moonraker GCode processor mode)")

    args = parser.parse_args()

    # GCode processor mode: preprocess only, Moonraker extracts metadata itself
    if args.gcode_file is not None:
        try:
            ok = preprocess_file(args.gcode_file, args.preprocessor_config)
        except Exception:
            logging.error(traceback.format_exc())
            ok = False
        sys.exit(0 if ok else 1)

    # METADATA_SCRIPT fallback mode
    config = {}
    if args.config is None:
        if args.filename is None:
            logging.error("The '--filename' (-f) option must be specified when --config is not set")
            sys.exit(-1)
        config["filename"] = args.filename
        config["gcode_dir"] = args.path
        config["ufp_path"] = args.ufp
        config["check_objects"] = args.check_objects
        config["preprocess"] = args.preprocess
    else:
        try:
            with open(args.config, "r") as f:
                config = json.load(f)
        except Exception:
            logging.error(traceback.format_exc())
            sys.exit(-1)

        if config.get("filename") is None:
            logging.error("The 'filename' field must be present in the configuration")
            sys.exit(-1)

        # Merge command-line args into config (command-line takes precedence)
        if args.preprocess:
            config["preprocess"] = True
        if args.check_objects:
            config["check_objects"] = True

    if config.get("gcode_dir") is None:
        config["gcode_dir"] = os.path.abspath(os.path.dirname(__file__))

    file_path = os.path.join(config["gcode_dir"], config["filename"])

    # Moonraker's metadata.py extracts UFP archives itself; the G-code file
    # does not exist before that, so it is preprocessed afterwards
    ufp_pending = config.get("ufp_path") is not None and not os.path.isfile(file_path)

    directory = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(directory, "file_manager")
    metadata = None
    if os.path.exists(target_dir):
        sys.path.insert(0, target_dir)
        import metadata

    if ufp_pending and metadata is not None:
        try:
            metadata.extract_ufp(config["ufp_path"], file_path)
            config["ufp_path"] = None
        except Exception:
            logging.error(traceback.format_exc())

    if not os.path.isfile(file_path):
        logging.error(f"File not found: {file_path}")
        sys.exit(-1)

    if config.get("preprocess", False):
        try:
            preprocess_file(file_path, args.preprocessor_config)
        except Exception as e:
            # Don't exit with error - allow metadata extraction to continue
            logging.error(f"gcode_preprocessor: Error during preprocessing: {e}")
            logging.error(traceback.format_exc())

    # Now call the original metadata extraction
    # This maintains compatibility with Moonraker's file processing
    if metadata is None:
        logging.error(f"gcode_preprocessor: file_manager directory not found at {target_dir}")
        sys.exit(-1)
    try:
        metadata.main(config)
    except Exception as e:
        logging.error(f"gcode_preprocessor: Error during metadata extraction: {e}")
        logging.error(traceback.format_exc())
        sys.exit(-1)
