# KTC G-code Preprocessor

A powerful, extensible G-code preprocessing system for Klipper that automatically optimizes and enhances G-code files for multi-tool 3D printing.

Repository: <https://github.com/ChrisFo8390/ktc-gcode-preprocessor>
(fork of [jwellman80/klipper-gcode-preprocessor](https://github.com/jwellman80/klipper-gcode-preprocessor))

## Installation

```bash
cd ~
git clone https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git
cd ktc-gcode-preprocessor
./install.sh
```

`install.sh` installs from the directory it is started in, so the clone can live
anywhere. It

- links `gcode_preprocessor.py`, `gcode_preprocessor_base.py` and the
  `preprocessors/` directory into `~/klipper/klippy/extras/`,
- copies `config/gcode-preprocessor.cfg` to
  `~/printer_data/config/gcode-preprocessor/preprocessor.cfg` (only if it does not exist yet),
- optionally links the Moonraker component and adds `[gcode_preprocessor]` and
  `[update_manager ktc-gcode-preprocessor]` to `moonraker.conf` (a backup
  `moonraker.conf.bak.<timestamp>` is written first; existing sections are not changed).

Different locations can be set with environment variables, e.g.
`KLIPPER_PATH=~/klipper CONFIG_PATH=~/printer_data/config ./install.sh`.

Add to `printer.cfg`
```ini
[include gcode-preprocessor/preprocessor.cfg]
```

### Updates

With the `[update_manager ktc-gcode-preprocessor]` section the preprocessor appears
in Mainsail/Fluidd's update manager and is updated from this repository; Klipper
and Moonraker are restarted afterwards. Section written by `install.sh`:
```ini
[update_manager ktc-gcode-preprocessor]
type: git_repo
path: ~/ktc-gcode-preprocessor
origin: https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git
primary_branch: main
managed_services: klipper moonraker
```
Without the update manager: `cd ~/ktc-gcode-preprocessor && git pull`, then
restart Klipper and Moonraker. Re-running `install.sh` is only needed if a
release note says so.

### Switching from jwellman80/klipper-gcode-preprocessor

Run `install.sh` from this repository; it replaces the existing links in
`~/klipper/klippy/extras/` and `~/moonraker/moonraker/components/` and keeps your
`preprocessor.cfg`. Then remove an `[update_manager]` section that points to the
old repository from `moonraker.conf`, restart Klipper and Moonraker and delete the
old clone (`~/klipper-gcode-preprocessor`).

### Moonraker options

`moonraker.conf`:
```ini
[gcode_preprocessor]
enable_preprocessing: True
# Optional:
# config_path: ~/printer_data/config/gcode-preprocessor/preprocessor.cfg
# timeout: 600        # seconds, used with Moonraker's GCode processor API
# console_messages: True
# message_start: KTC preprocessing active
# message_success: preprocessing successful
# message_failed: !! KTC preprocessing failed - see moonraker.log
```

**Console messages:** When a file is uploaded while no print is running or paused,
the Mainsail/Fluidd console shows `message_start` when preprocessing begins and
`message_success` (or `message_failed`) when it has finished. Nothing is shown
during a print, for files that are already preprocessed or when preprocessing is
disabled. The messages are sent by Moonraker itself (like a Klipper console
response), Klipper is not involved. This uses Moonraker's internal
`_run_extract_metadata`; if a future Moonraker version removes it, preprocessing
keeps working without the messages (a warning is logged).

On Moonraker versions that provide the GCode processor API
(`MetadataStorage.register_gcode_processor`) the preprocessor registers itself
there and runs with its own `timeout`. On older versions it falls back to
replacing Moonraker's metadata script; the preprocessing then has to finish
within Moonraker's metadata timeout (20 s by default). For large multi-tool
files raise it in `moonraker.conf`:
```ini
[file_manager]
default_metadata_parser_timeout: 300
```

The Moonraker script only reads `preprocessor.cfg` (or `config_path`), not
`printer.cfg`. Keep all `[gcode_preprocessor ...]` sections in that file so
Klipper and Moonraker use the same settings.

Restart Moonraker and Klipper:
```bash
sudo service moonraker restart
sudo service klipper restart
```

## Features

### ⏱️ **Idle Tool Shutdown** (Formerly "Unused Tool Shutdown")
Intelligently shuts down tools with two complementary strategies:

1. **End-of-Use Shutdown** (Always enabled): Automatically inserts `M104 T{n} S0` after a tool's last usage
2. **Predictive Idle Shutdown** (Optional): Predicts when a tool will be idle for longer than a threshold and shuts it down immediately after tool change

**Benefits:**
- Saves energy and reduces wear on hotends
- Prevents oozing from idle tools during long prints
- Predictive mode allows tools to cool while idle instead of staying hot unnecessarily

**Example (End-of-Use):**
```gcode
T0          ; Print with T0
T1          ; Switch to T1
T0          ; Switch back to T0
; T1 no longer needed - cooling down (INSERTED AUTOMATICALLY)
M104 T1 S0
```

**Example (Predictive Idle with 5min threshold):**
```gcode
T0          ; Use T0 at time 0:00
; ... print for 3 minutes ...
T1          ; Switch to T1 at time 3:00
; T0 will be idle for 6.2 minutes - cooling down (INSERTED IMMEDIATELY)
M104 T0 S0  ; Shutdown happens NOW, not 5 minutes later
; ... print with T1 for 6 minutes ...
T0          ; Use T0 again at time 9:00 (was idle 6 min)
```

### 🔄 **Token Replacer**
Extracts slicer metadata from G-code comments and replaces token placeholders with actual values.

**Metadata extracted:**
- Tools used
- Filament colors
- Filament materials
- Print temperatures
- Purge volumes (optional)
- Filament names (optional)

**Placeholders replaced:**
- `!tool_count!` → Number of tools
- `!tools!` → Comma-separated tool list
- `!colors!` → Hex color codes
- `!materials!` → Material types
- `!temperatures!` → Temperature values

**Generic Replacement:**

Scans for all values in the config section with: 
`; key = ` 

Looks for `!!key!!` in gcode and replaces with those values.  

Example config section at bottom of gcode: 
```
; curr_bed_type = Textured PEI Plate
; default_acceleration = 5000
; default_bed_type = 
; default_filament_colour = ;;;
```

Example print_start macro in Slicer: 
```
PRINT_START BED_TYPE="!!curr_bed_type!!"
```

Resulting print_start after gcode preprocessing has ran:
```
PRINT_START BED_TYPE="Textured PEI Plate"
```


**Supports:** 
- PrusaSlicer
- SuperSlicer
- OrcaSlicer
- BambuStudio

**Example Usage:**
```gcode
[gcode_macro START_PRINT]
gcode:
    M118 Print uses !tool_count! tools
    M118 Colors: !colors!
```

## Usage

### Automatic Processing
Files uploaded via Mainsail/Fluidd are automatically preprocessed (requires Moonraker integration).

### Manual Processing
```gcode
PREPROCESS_GCODE_FILE FILE=/path/to/file.gcode
```
Runs in a background process so Klipper keeps serving heaters and motion.
Refused while a print is running or paused unless `FORCE=1` is given.

### List Processors
```gcode
LIST_GCODE_PROCESSORS
```

## Configuration

### Default Settings
Located in `~/printer_data/config/gcode-preprocessor/preprocessor.cfg`

### Idle Tool Shutdown Settings
```ini
[gcode_preprocessor idle_tool_shutdown]
# Predictive idle timeout (set to 0 to disable, only use end-of-use shutdown)
idle_timeout_minutes: 5      # Shut down tools idle for > 5 minutes

# End-of-use feature (always enabled)
exclude_tools:               # Comma-separated list to exclude (e.g., "0,1")

# Time estimation (only used if idle_timeout_minutes > 0)
initial_feedrate: 3000       # Initial feedrate in mm/min for time calculations

# Optional tool -> heater mapping (see below)
tool_heaters:                # e.g. "0=extruder, 1=extruder1, 2=extruder2"
```

**Which heater is switched off:** Without `tool_heaters`, the shutdown command
is `M104 T{n} S0`. Klipper resolves `T{n}` to the heater `extruder` (n=0) or
`extruder{n}`, and silently ignores `S0` for a heater that does not exist. If the
tool numbers in your G-code are not identical to the extruder index (e.g. AFC
lanes or a toolchanger with custom extruder names), set `tool_heaters` so the
preprocessor emits `SET_HEATER_TEMPERATURE HEATER=<name> TARGET=0` instead.

**Reheating:** The preprocessor never inserts heat-up commands. A tool that was
shut down must be reheated (and waited for) by your tool change macro or the
slicer before it prints again.

**Time estimation:** Distance / feedrate of G0-G3 moves (absolute and relative
positioning, arcs, G92) plus G4 dwells. Acceleration is ignored, so the estimate
is lower than the real print time; predictive shutdowns are therefore
conservative.

**How it works:**
- **End-of-use** is always active - tools are shut down after their final usage
- **Predictive idle** is optional - when enabled, tools are shut down immediately if they'll be idle > threshold
- Set `idle_timeout_minutes: 0` to disable predictive mode and only use end-of-use shutdown

### Token Replacer Settings
```ini
[gcode_preprocessor token_replacer]
extract_tools: True
extract_colors: True
extract_materials: True
extract_temperatures: True
replace_placeholders: True
extract_slicer_config: True     # enables !!key!! placeholders
sanitize_generic_values: True   # ';' -> ',' and '#' removed in !!key!! values
```

Klipper cuts a command at `;` and treats `#` as a comment in extended commands.
Multi-value slicer settings such as `filament_type = PLA;PETG` or
`filament_colour = #FF0000;#00FF00` would therefore silently truncate the line.
With `sanitize_generic_values` (default) they are inserted as `PLA,PETG` and
`FF0000,00FF00`. Values with spaces need quotes in the macro call, e.g.
`BED_TYPE="!!curr_bed_type!!"`.

## Writing Custom Processors

Create a new file in `klipper/extras/preprocessors/my_processor.py` of this
repository. The `preprocessors/` directory is linked as a whole, so the new
file is available to Klipper and Moonraker after a restart:

```python
try:
    from ..gcode_preprocessor_base import (   # loaded by Klipper
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns
    )
except ImportError:
    from gcode_preprocessor_base import (     # loaded by the Moonraker script
        GcodePreprocessorPlugin,
        PreprocessorContext,
        GcodePatterns
    )

class MyProcessor(GcodePreprocessorPlugin):
    def __init__(self, config, logger):
        super().__init__(config, logger)

    def get_name(self):
        return "my_processor"

    def get_description(self):
        return "My custom processor"

    def pre_process(self, file_path, context):
        # First pass - gather metadata
        # The instance is reused for every file: reset per-file state here
        return True

    def process_line(self, line, context):
        # Process each line
        return [line]

    def post_process(self, file_path, context):
        # Final pass
        return True

def create_processor(config, logger):
    return MyProcessor(config, logger)
```

Then add to your config:
```ini
[gcode_preprocessor]
processors: token_replacer, idle_tool_shutdown, my_processor

[gcode_preprocessor my_processor]
# Custom settings (any option name is accepted)
my_option: 42
```

Read options with `config.get()`, `config.getboolean()`, `config.getint()` or
`config.getfloat()`.

## Architecture

### Processing Pipeline
```
File Upload → Moonraker → Klipper Preprocessor
                              ↓
                 ┌────────────┴────────────┐
                 │  Pass 1: Pre-process    │
                 │  (gather metadata)      │
                 └────────────┬────────────┘
                              ↓
                 ┌────────────┴────────────┐
                 │  Pass 2: Line-by-line   │
                 │  (transform commands)   │
                 └────────────┬────────────┘
                              ↓
                 ┌────────────┴────────────┐
                 │  Pass 3: Post-process   │
                 │  (finalization)         │
                 └────────────┬────────────┘
                              ↓
                     Processed G-code
```

### Directory Structure
```
ktc-gcode-preprocessor/
├── install.sh                          # Installation script
├── README.md                           # This file
├── klipper/extras/
│   ├── gcode_preprocessor_base.py     # Base classes
│   ├── gcode_preprocessor.py          # Main registry
│   └── preprocessors/
│       ├── __init__.py
│       ├── token_replacer.py          # Extract metadata & replace tokens
│       ├── idle_tool_shutdown.py      # Intelligent tool shutdown (predictive + end-of-use)
│       └── example_template.py        # Example processor template
├── moonraker/
│   └── gcode_preprocessor.py          # Moonraker component
├── config/
│   └── gcode-preprocessor.cfg         # Default configuration
├── examples/
│   └── test_sample.gcode               # Test file
└── tests/
    └── test_preprocessor.py            # Unit tests (python3 -m unittest discover -s tests)
```

## Troubleshooting

### Preprocessor Not Running
- Check that `enabled: True` in `[gcode_preprocessor]`
- Verify Klipper logs: `tail -f ~/printer_data/logs/klippy.log`
- Ensure config is included in `printer.cfg`

### Files Already Preprocessed
Files are only preprocessed once. First line will contain:
```gcode
; processed by ktc-gcode preprocessor (slicer: OrcaSlicer)
```

The `(slicer: ...)` suffix is only added when `token_replacer` detected the
slicer. Files marked by older versions (`; processed by klipper-gcode-preprocessor`)
are recognized as well and not processed again.

To reprocess, delete this line or re-upload the file.

### Tool Not Shutting Down
- Check `exclude_tools` setting (empty by default - all tools shut down)
- Check that `M104 T{n}` reaches the right heater, otherwise set `tool_heaters`
- For end-of-use: the tool must be switched away from after its last usage
- For predictive idle: Ensure `idle_timeout_minutes` is set and tool is idle long enough
- Check logs for `idle_tool_shutdown` messages

### Placeholders Not Replaced
- Ensure `token_replacer` is in the processors list
- Verify slicer is supported (PrusaSlicer, OrcaSlicer, BambuStudio, SuperSlicer)

## Examples

See `examples/test_sample.gcode` and the tests in `tests/`.

## Contributing

Contributions welcome! Please submit pull requests or open issues at
<https://github.com/ChrisFo8390/ktc-gcode-preprocessor>.

## Credits

Based on [klipper-gcode-preprocessor](https://github.com/jwellman80/klipper-gcode-preprocessor)
by Jared Wellman. Inspired by the Happy Hare MMU preprocessor by moggieuk.

## License

GNU GPLv3

## Support

- Issues: GitHub Issues
- Discussions: GitHub Discussions
