# Changelog

All notable changes to the Klipper G-code Preprocessor will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-09

### Fixed
- idle_tool_shutdown: re-selecting the already active tool (e.g. `T1` twice) no longer
  inserts `M104 T1 S0` for the tool that is printing
- idle_tool_shutdown: a tool shut down by the predictive timeout is re-armed when it is
  selected again or reheated with `M104/M109` without `T`; before, it was never cooled
  down again (neither predictive nor end-of-use)
- idle_tool_shutdown: time estimation handles G91 relative moves, G2/G3 arcs and G92;
  `G4 S5 ; Pause` is no longer read as milliseconds
- token_replacer: boolean options (`extract_*`, `replace_placeholders`) can be disabled
  (string "False" was treated as true)
- token_replacer: `extract_filament_names: True` crashed with IndexError
- token_replacer: `!!key!!` values containing `;` or `#` no longer truncate the command
  in Klipper (new option `sanitize_generic_values`, default True)
- token_replacer: built-in `!tools!` no longer matches inside an unresolved `!!tools!!`
- Both processors reset all per-file state; before, `PREPROCESS_GCODE_FILE` reused
  bed type, colors and tool counts from the previously processed file
- Klipper module: `[gcode_preprocessor <name>]` options were never applied (the module
  read a non-existent `configfile.fileconfig`); any option name is now accepted, so
  `extract_slicer_config` and custom processor options no longer fail the Klipper start
- Klipper module: `PREPROCESS_GCODE_FILE` runs in a background process instead of
  blocking the reactor, and is refused during a print unless `FORCE=1`
- `SELECT_TOOL TOOL=<n>` / `TOOL=T<n>` is recognized as a tool change
- Moonraker: config is parsed like Klipper does (inline `#`/`;` comments); before, an
  inline comment made idle_tool_shutdown fail to load
- Moonraker: UFP uploads are extracted before preprocessing instead of aborting
- Files are read/written with `surrogateescape`; non-UTF-8 bytes are preserved
- Output is streamed to the temp file (memory no longer grows with file size)
- Documentation: removed references to the non-existent `docs/README.md` and the
  invalid `[preprocessor unused_tool_shutdown]` section

### Changed
- First-line marker is now `; processed by ktc-gcode preprocessor (slicer: <name>)`;
  files with the old marker `; processed by klipper-gcode-preprocessor` are still
  recognized as already processed

### Added
- tool_summary processor + `_KTC_TOOL_SUMMARY` macro (`config/tool-summary.cfg`): console
  summary of used tools, temperatures and filament colors (colored) before PRINT_START
- install.sh installs from the repository it is run from (default clone
  `https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git` to `~/ktc-gcode-preprocessor`),
  links `preprocessors/` as a directory, adds `[gcode_preprocessor]` and
  `[update_manager ktc-gcode-preprocessor]` to `moonraker.conf` (with backup)
- `.gitattributes`: text files are stored with LF in the repository, `*.sh` always LF
- Moonraker: console messages "KTC preprocessing active" / "preprocessing successful"
  (or a failure message) for uploads while no print is running; options
  `console_messages`, `message_start`, `message_success`, `message_failed`
- idle_tool_shutdown: `tool_heaters` option to map tool numbers to Klipper heater names
  (`SET_HEATER_TEMPERATURE` instead of `M104 T{n}`)
- Moonraker: registration via Moonraker's GCode processor API with own `timeout`
  (fallback to METADATA_SCRIPT on older Moonraker); `config_path` option
- Shared `run_pipeline()` and `ProcessorConfig` used by Klipper and Moonraker
- Unit tests in `tests/`

## [1.0.0] - 2024-XX-XX

### Added
- Initial release of Klipper G-code Preprocessor
- Tool Thermal Manager processor for automatic tool cooldown
- Metadata Extractor processor for slicer metadata extraction
- Placeholder Replacer processor for dynamic G-code values
- Extensible plugin architecture
- Three-pass processing system (pre-process, line-by-line, post-process)
- Moonraker integration for automatic file preprocessing
- Manual preprocessing via `PREPROCESS_GCODE_FILE` command
- `LIST_GCODE_PROCESSORS` command to view loaded processors
- Comprehensive configuration system
- Support for PrusaSlicer, SuperSlicer, OrcaSlicer, BambuStudio
- Automated installation script
- Complete documentation and examples

### Features
- **Tool Thermal Manager**
  - Automatically cool down tools after last usage
  - Configurable tool exclusion list
  - Optional explanatory comments
  - Immediate or delayed cooldown modes

- **Metadata Extractor**
  - Extract tools used
  - Extract filament colors
  - Extract filament materials
  - Extract temperatures
  - Extract purge volumes (optional)
  - Extract filament names (optional)

- **Placeholder Replacer**
  - Replace !tool_count! with number of tools
  - Replace !tools! with tool list
  - Replace !colors! with color codes
  - Replace !materials! with material types
  - Replace !temperatures! with temperature values
  - Replace !total_toolchanges! with toolchange count

### Security
- Atomic file replacement to prevent corruption
- Fingerprint tracking to prevent double-processing
- Safe error handling with detailed logging

## [Unreleased]

### Planned
- Web UI for processor configuration
- Real-time preview of preprocessing changes
- Statistics and analytics processor
- Print time estimation adjustments
- Additional slicer support (Cura, Simplify3D)
- Processor marketplace/registry
