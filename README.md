# KTC G-code Preprocessor

G-code preprocessor for Klipper multi-tool printers (toolchanger, AFC/MMU). Uploaded
files are preprocessed once by Moonraker: slicer data is extracted, placeholders are
replaced, idle tools are switched off and a tool summary is printed at print start.

Repository: <https://github.com/ChrisFo8390/ktc-gcode-preprocessor>
(fork of [jwellman80/klipper-gcode-preprocessor](https://github.com/jwellman80/klipper-gcode-preprocessor)).
Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Installation

```bash
git clone https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git ~/ktc-gcode-preprocessor
~/ktc-gcode-preprocessor/install.sh
```

`install.sh` (run as normal user, uses `sudo` for `systemctl`):

- links `gcode_preprocessor.py`, `gcode_preprocessor_base.py` and the `preprocessors/`
  directory into `~/klipper/klippy/extras/`
- copies `config/gcode-preprocessor.cfg` to
  `~/printer_data/config/gcode-preprocessor/preprocessor.cfg` (only if missing) and links
  `tool-summary.cfg` next to it
- optionally (option 1) links the Moonraker component and adds `[gcode_preprocessor]` and
  `[update_manager ktc-gcode-preprocessor]` to `moonraker.conf` (backup
  `moonraker.conf.bak.<timestamp>` first, existing sections stay unchanged)
- optionally restarts Klipper and Moonraker

Other paths: `KLIPPER_PATH`, `MOONRAKER_PATH`, `CONFIG_PATH` environment variables.

Add to `printer.cfg`:
```ini
[include gcode-preprocessor/preprocessor.cfg]
```

**Updates** then come through the Mainsail/Fluidd update manager (Klipper and Moonraker
are restarted). Manually: `git -C ~/ktc-gcode-preprocessor pull` and restart both.

### Switching from jwellman80/klipper-gcode-preprocessor

Your `preprocessor.cfg` and the `printer.cfg` include are kept.

```bash
rm -f ~/klipper/klippy/extras/gcode_preprocessor*.py ~/moonraker/moonraker/components/gcode_preprocessor.py
rm -rf ~/klipper/klippy/extras/preprocessors ~/klipper-gcode-preprocessor
git clone https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git ~/ktc-gcode-preprocessor
~/ktc-gcode-preprocessor/install.sh
```

Then remove an `[update_manager]` section for the old repository from `moonraker.conf`
(if present). For the tool summary add `[include tool-summary.cfg]` at the top of
`preprocessor.cfg` and `tool_summary` to `processors`.

## Configuration

**All `[gcode_preprocessor ...]` sections belong in `preprocessor.cfg`.** Moonraker
preprocesses uploads with a standalone script that reads only this file, not
`printer.cfg`; sections in `printer.cfg` would apply to `PREPROCESS_GCODE_FILE` but not
to uploads.

```ini
[include tool-summary.cfg]

[gcode_preprocessor]
enabled: True
processors: token_replacer, idle_tool_shutdown, tool_summary   # run in this order

[gcode_preprocessor idle_tool_shutdown]
idle_timeout_minutes: 2
```

`moonraker.conf`:
```ini
[gcode_preprocessor]
enable_preprocessing: True
# config_path: ~/printer_data/config/gcode-preprocessor/preprocessor.cfg
# timeout: 600              # seconds
# console_messages: True
# message_start: KTC preprocessing active
# message_success: preprocessing successful
# message_failed: !! KTC preprocessing failed - see moonraker.log
```

- Console messages are shown for uploads while no print is running or paused.
- Newer Moonraker: registered as GCode processor with its own `timeout`. Older
  Moonraker: runs as metadata script within `[file_manager] default_metadata_parser_timeout`
  (default 20 s, raise to e.g. 300 for large files).

## Processors

### token_replacer
Reads slicer metadata (PrusaSlicer, SuperSlicer, OrcaSlicer, BambuStudio) and replaces
placeholders in non-comment lines:

- `!tool_count!`, `!tools!`, `!colors!`, `!materials!`, `!temperatures!`
- `!!key!!` → any `; key = value` from the slicer config block, e.g.
  `PRINT_START BED_TYPE="!!curr_bed_type!!"`. `;` becomes `,` and `#` is removed
  (`sanitize_generic_values`), so Klipper does not cut the command.

```ini
[gcode_preprocessor token_replacer]
extract_tools: True
extract_colors: True
extract_materials: True
extract_temperatures: True
extract_purge_volumes: False
extract_filament_names: False
extract_slicer_config: True
replace_placeholders: True
sanitize_generic_values: True
```

### idle_tool_shutdown
- **End of use** (always): `M104 T{n} S0` after the last use of a tool.
- **Predictive** (`idle_timeout_minutes` > 0): on a tool change, a tool that will be idle
  longer than the timeout is switched off immediately. The time is estimated from
  G0-G3 moves and G4 dwells without acceleration, so it errs on the safe side.
- Re-selecting the active tool is ignored. Reheating is up to your tool change macro.

```ini
[gcode_preprocessor idle_tool_shutdown]
idle_timeout_minutes: 0      # 0 = only end-of-use shutdown
exclude_tools:               # e.g. 0,1
initial_feedrate: 3000       # mm/min until the first F
tool_heaters:                # e.g. 0=extruder, 1=extruder1
```

With `tool_heaters` it emits `SET_HEATER_TEMPERATURE HEATER=<name> TARGET=0`; use it when
tool numbers do not match `extruder`/`extruder{n}`.

### tool_summary
Inserts `_KTC_TOOL_SUMMARY TOOLS=.. TEMPS=.. FIRST_TEMPS=.. COLORS=..` before the first
`PRINT_START`/`START_PRINT`. The macro (`tool-summary.cfg`) prints each tool with its
temperature (and first layer temperature if different) and a colored swatch in the
Mainsail/Fluidd console:

```
KTC tool summary: 2 tool(s)
T0: 220 °C (first layer 225 °C)   ■ #FF0000
T2: 250 °C   ■ #00AEEF
```

Needs `token_replacer` before it. Options:
```ini
[gcode_preprocessor tool_summary]
print_start_macros: PRINT_START, START_PRINT
summary_macro: _KTC_TOOL_SUMMARY
```

## Commands

| Command | |
|---|---|
| `LIST_GCODE_PROCESSORS` | loaded processors with their descriptions |
| `PREPROCESS_GCODE_FILE FILE=<path>` | preprocess a file manually (background process; refused during a print unless `FORCE=1`) |

Processed files start with `; processed by ktc-gcode preprocessor (slicer: <name>)` and
are not processed again (the old `klipper-gcode-preprocessor` marker is recognized too).
To reprocess, re-upload the file from the slicer.

## Troubleshooting

- Logs: `~/printer_data/logs/klippy.log` and `moonraker.log` (search `gcode_preprocessor`).
- Setting works in `LIST_GCODE_PROCESSORS` but not for uploads → section is in
  `printer.cfg` instead of `preprocessor.cfg`.
- Tool not switched off → check `exclude_tools`, `tool_heaters`, and that the tool is
  changed away from after its last use.
- Placeholders not replaced → `token_replacer` missing in `processors` or slicer not
  supported.

## Custom processors

Add `klipper/extras/preprocessors/my_processor.py` (see `example_template.py`), list it
in `processors` and configure it in `[gcode_preprocessor my_processor]`. A processor
derives from `GcodePreprocessorPlugin` and implements `pre_process()` (scan the file,
reset per-file state), `process_line()` (return a list of output lines) and
`post_process()`, plus a `create_processor(config, logger)` factory. Import the base
module with `from ..gcode_preprocessor_base import ...` and fall back to
`from gcode_preprocessor_base import ...` on `ImportError` (Moonraker script).

Tests: `python3 -m unittest discover -s tests -v`

## Credits & license

Based on [klipper-gcode-preprocessor](https://github.com/jwellman80/klipper-gcode-preprocessor)
by Jared Wellman, inspired by the Happy Hare MMU preprocessor by moggieuk. GNU GPLv3.
