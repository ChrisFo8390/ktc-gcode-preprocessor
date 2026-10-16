# KTC G-code Preprocessor

G-code preprocessor for Klipper multi-tool printers (toolchanger, AFC/MMU).

## Summary

Every uploaded G-code file is processed once by Moonraker, before it is printed.
Processors can be combined freely:

| Processor | What it does |
|---|---|
| [token_replacer](#token_replacer) | reads the slicer settings and replaces placeholders such as `!tool_count!` or `!!curr_bed_type!!` |
| [idle_tool_shutdown](#idle_tool_shutdown) | switches tools off after their last use or when they will be idle for a long time |
| [tool_summary](#tool_summary) | prints used tools, temperatures and filament colors to the console at print start |
| [heat_stats](#heat_stats) | measures how long tool changes wait for the nozzle temperature and suggests preheat time / standby temperature |
| [tool_prepare](#tool_prepare) | at print start: loads (AFC), purges, brushes and retracts every tool of the print, so the first use of a tool does not miss filament |

Processed files start with `; processed by ktc-gcode preprocessor (slicer: <name>)` and
are never processed twice. Supported slicers: PrusaSlicer (incl. BOSS fork), SuperSlicer,
OrcaSlicer, BambuStudio.

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

**Updates** come through the Mainsail/Fluidd update manager (Klipper and Moonraker are
restarted). Manually: `git -C ~/ktc-gcode-preprocessor pull` and restart both.
Processors added by an update are available immediately (the `preprocessors/` directory
is linked as a whole); enable them in `processors`.

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
# run in this order; token_replacer first (the others use its slicer data)
processors: token_replacer, idle_tool_shutdown, tool_summary, heat_stats, tool_prepare

[gcode_preprocessor idle_tool_shutdown]
idle_timeout_minutes: 2
```

All processor sections are optional; the options and defaults are listed with each
processor below and in `config/gcode-preprocessor.cfg`.

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
Reads the slicer metadata and replaces placeholders in non-comment lines. The other
processors use the data it collects, so list it first.

- `!tool_count!`, `!tools!`, `!colors!`, `!materials!`, `!temperatures!`
- `!!key!!` → any `; key = value` from the slicer config block, e.g.
  `PRINT_START BED_TYPE="!!curr_bed_type!!"`. `;` becomes `,` and `#` is removed
  (`sanitize_generic_values`), so Klipper does not cut the command.
- Colors come from `filament_colour` (`color_source: filament`) or `extruder_colour`
  (`color_source: extruder`); empty entries are filled from the other list.

```ini
[gcode_preprocessor token_replacer]
extract_tools: True
extract_colors: True
color_source: filament
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
- Re-selecting the active tool is ignored. Reheating is up to your tool change macro or
  the slicer.

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

Single-tool prints show the tool from the print start line (`TOOL=<n>`). Needs
`token_replacer` before it.

```ini
[gcode_preprocessor tool_summary]
print_start_macros: PRINT_START, START_PRINT
summary_macro: _KTC_TOOL_SUMMARY
```

### heat_stats
Measures per tool how long the printer stands still at tool changes because the nozzle
is not at temperature yet, and suggests slicer values at the end of the print.

- The processor marks the file with `_KTC_HEAT_MARK` lines: `PREHEAT` before the slicer
  preheat (`M104 S.. Tn ; preheat Tn time: 15s` from PrusaSlicer BOSS ooze prevention),
  `BEGIN` before each tool change, `END` after the heat wait that follows it
  (`M109`/`M116`/`TEMPERATURE_WAIT`), `RESET` at the start and `REPORT` at the end.
- Klipper samples the tool's heater and the toolhead every `sample_interval`. Heat wait
  is the time the toolhead stands still while the nozzle is more than `temp_tolerance`
  off target or the heater is still settling (the condition `M109` waits on). Waits in
  your tool change macro (e.g. the pickup `M109` at the dock) are included.
- A preheat that the slicer undoes with a later cooldown `M104` of the same tool is
  counted (`preheats undone by cooldown`) and not used for the suggestions.

Report at the end of the print, or any time with `KTC_HEAT_STATS` (example output):
```
KTC heat stats: 639 tool changes, heat wait 1h 23m (avg 7.8 s)
T1: 207 changes | wait avg 8.1 s, p90 12.8 s, max 14.0 s, total 27m 57s | heat rate 3.0 °C/s | preheats undone by cooldown: 48
Suggestion (slicer preheat time now 15 s, real/slicer time 1.00):
  A) preheat_time: 31 s (keep standby temperature)
  B) standby_temperature_delta: -50 °C (keep preheat_time 15 s); per filament idle_temperature: T1 175 °C
```
- **A)** preheat time that covers the 90th percentile wait plus `margin` (max. 120 s). If
  tools were hot long before their change, a shorter time is suggested.
- **B)** how far a parked tool may cool down and still reach temperature in the time it
  had (median): measured heat rate × `safety_factor` × time, rounded down to 5 °C.
- With several tools the longest preheat and the smallest standby drop are suggested.
  Use A or B, not both. Each report is appended to `history_file` (JSON lines).

```ini
[gcode_preprocessor heat_stats]
insert_report: True          # REPORT marker at the end of the file
sample_interval: 0.2         # s
temp_tolerance: 1            # °C
margin: 3                    # s added to the suggested preheat time
safety_factor: 0.9           # applied to the measured heat rate
track_timeout: 900           # s until a preheated tool that is never selected is dropped
history_file: ~/printer_data/logs/ktc_heat_stats.jsonl
tool_heaters:                # e.g. 0=extruder, 1=extruder1
```

Heater per tool: `tool_heaters`, else the `extruder` of klipper-toolchanger's
`[tool Tn]`, else `extruder`/`extruder{n}`. `_KTC_HEAT_MARK` and `KTC_HEAT_STATS` are
always registered, so files processed with heat_stats also print after it was removed
from `processors`. Marker times are taken when Klipper reads the line, so the measured
preheat lead can be off by the move buffer (up to about 2 s).

### tool_prepare
Brings every tool of the print into a defined state at print start. The slicer retracts
a tool by `retract_length_toolchange` when it leaves it, but at the first use of a tool
it only pushes the travel unretract (e.g. `G1 E1`, with a wipe tower nothing). A tool
left retracted by the previous print therefore starts about 10 mm short.

**Setup**
1. Add `tool_prepare` to `processors` (after `token_replacer`).
2. Call `KTC_PREPARE_TOOLS` in your `PRINT_START` after homing/probing, before the
   initial tool is selected and the purge line is printed:
   ```
   CARTOGRAPHER_TOUCH_HOME    # or G28 / your probing
   KTC_PREPARE_TOOLS
   ```
3. Re-upload the file from the slicer.

**At upload** the processor inserts
- `_KTC_PREPARE_TOOLS_SET TOOLS=4,3,0 TEMPS=225,225,225 RETRACT=10,10,10
  STANDBY=125,125,125 INITIAL=4` before `PRINT_START`: tools, print temperatures,
  retraction (`filament_retract_length_toolchange` overrides `retract_length_toolchange`)
  and standby (`idle_temperature`, else temperature + `standby_temperature_delta`) from
  the slicer,
- `_KTC_PREPARE_PUSH T=<n> E=<retraction> F=<deretract_speed>` right before the first
  extrusion after the first tool change of every tool except the initial one (before
  the travel unretract, or before the first wipe tower extrusion).

**`KTC_PREPARE_TOOLS`** at print start
1. checks the AFC lane of every tool and aborts before anything heats if one has no
   filament (`abort_on_empty`),
2. heats the tools one after another, `heat_stagger` seconds apart and not more than
   `max_heating` at the same time,
3. raises to `safe_z` and picks up each tool (initial tool last):
   - lane not loaded into the extruder: AFC loads it and runs its poop/wipe macros,
   - tool already loaded: purged and brushed with the AFC macros (`poop_cmd`/`wipe_cmd`
     from `[AFC]`) or `purge_cmd`/`wipe_cmd`,
   - then retracted by the slicer value and set to its standby temperature,
4. leaves the initial tool picked up and hot. If it was already loaded it is neither
   purged nor retracted (its filament position is unknown, the purge line primes it).

**`_KTC_PREPARE_PUSH`** pushes only if `KTC_PREPARE_TOOLS` retracted that tool in this
print, at most once and never more than was retracted, in its own relative-extrusion
state (also works with `M82`). A tool that was not retracted is not pushed, so the
print behaves as without tool_prepare.

**Without AFC, macros or processor:** all three commands are always registered.
Without the `_KTC_PREPARE_TOOLS_SET` data (file not processed) `KTC_PREPARE_TOOLS` only
reports "skipped". Without AFC there is no lane check and no loading; purge/brush macros
that are not defined are skipped with a message; a tool that was neither loaded nor
purged is not retracted. Not homed: aborts with a message.

```ini
[gcode_preprocessor tool_prepare]
heat_stagger: 10        # s between starting the tool heaters
max_heating: 3          # tools heating up at the same time (0 = no limit)
standby_temp:           # until first use; default: slicer standby (e.g. 225 - 100 = 125)
abort_on_empty: True    # abort the print if a needed lane has no filament
purge_initial: False    # also purge an already loaded initial tool
retract_speed: 35       # mm/s
safe_z: 5               # raise to this Z before each pickup and purge (never lowers, 0 = off)
z_speed: 10             # mm/s for the safe_z move
purge_cmd:              # purge macro, default AFC poop_cmd (if poop enabled), none = off
wipe_cmd:               # brush macro, default AFC wipe_cmd (if wipe enabled), none = off
retract_length:         # fixed retraction instead of the slicer value
tool_heaters:           # e.g. 0=extruder, 1=extruder1
```

## Commands

| Command | |
|---|---|
| `LIST_GCODE_PROCESSORS` | loaded processors with their descriptions |
| `PREPROCESS_GCODE_FILE FILE=<path>` | preprocess a file manually (background process; refused during a print unless `FORCE=1`) |
| `KTC_PREPARE_TOOLS [STAGGER=<s>] [MAX_HEATING=<n>]` | load, purge and retract the tools of the current print (tool_prepare, call from `PRINT_START`) |
| `KTC_HEAT_STATS [RESET=1]` | heat wait per tool and suggested preheat/standby for the current or last print (heat_stats) |

Processed files are not processed again (the old `klipper-gcode-preprocessor` marker is
recognized too). To reprocess, re-upload the file from the slicer.

## Troubleshooting

- Logs: `~/printer_data/logs/klippy.log` and `moonraker.log` (search `gcode_preprocessor`).
- Setting works in `LIST_GCODE_PROCESSORS` but not for uploads → section is in
  `printer.cfg` instead of `preprocessor.cfg`.
- Tool not switched off → check `exclude_tools`, `tool_heaters`, and that the tool is
  changed away from after its last use.
- Placeholders not replaced → `token_replacer` missing in `processors` or slicer not
  supported.
- `KTC_PREPARE_TOOLS` reports "skipped" → file uploaded before `tool_prepare` was
  enabled, or `token_replacer` missing; re-upload.
- AFC loads but does not purge/brush → `poop_cmd`/`wipe_cmd` missing in `[AFC]`
  (they have no default in AFC).

## Custom processors

Add `klipper/extras/preprocessors/my_processor.py` (see `example_template.py`), list it
in `processors` and configure it in `[gcode_preprocessor my_processor]`. A processor
derives from `GcodePreprocessorPlugin` and implements `pre_process()` (scan the file,
reset per-file state), `process_line()` (return a list of output lines) and
`post_process()`, plus a `create_processor(config, logger)` factory. Import the base
module with `from ..gcode_preprocessor_base import ...` and fall back to
`from gcode_preprocessor_base import ...` on `ImportError` (Moonraker script).
Processors that also need commands during the print add a runtime class to
`RUNTIME_PROCESSORS` in `gcode_preprocessor.py` (see heat_stats, tool_prepare).

Tests: `python3 -m unittest discover -s tests -v`

## Credits & license

Based on [klipper-gcode-preprocessor](https://github.com/jwellman80/klipper-gcode-preprocessor)
by Jared Wellman, inspired by the Happy Hare MMU preprocessor by moggieuk. GNU GPLv3.
