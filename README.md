# Klipper G-code Preprocessor

A powerful, extensible G-code preprocessing system for Klipper that automatically optimizes and enhances G-code files for multi-tool 3D printing.

## Installation

```bash
cd ~
git clone https://github.com/jwellman80/klipper-gcode-preprocessor.git
cd klipper-gcode-preprocessor
./install.sh
```

Add to `printer.cfg`
```ini
[include gcode-preprocessor/preprocessor.cfg]
```

Add to `moonraker.conf`:
```ini
[gcode_preprocessor]
enable_preprocessing: True
```

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
```

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
```

## Writing Custom Processors

Create a new file in `~/klipper/klippy/extras/preprocessors/my_processor.py`:

```python
from gcode_preprocessor_base import (
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
# Custom settings
```

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
klipper-gcode-preprocessor/
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
├── docs/
│   └── README.md                       # Detailed documentation
└── examples/
    └── test_sample.gcode               # Test file
```

## Troubleshooting

### Preprocessor Not Running
- Check that `enabled: True` in `[gcode_preprocessor]`
- Verify Klipper logs: `tail -f ~/printer_data/logs/klippy.log`
- Ensure config is included in `printer.cfg`

### Files Already Preprocessed
Files are only preprocessed once. First line will contain:
```gcode
; processed by klipper-gcode-preprocessor
```

To reprocess, delete this line or re-upload the file.

### Tool Not Shutting Down
- Check `exclude_tools` setting (empty by default - all tools shut down)
- For end-of-use: Verify tool is used multiple times (needs "last usage")
- For predictive idle: Ensure `idle_timeout_minutes` is set and tool is idle long enough
- Check logs for `idle_tool_shutdown` messages

### Placeholders Not Replaced
- Ensure `token_replacer` is in the processors list
- Verify slicer is supported (PrusaSlicer, OrcaSlicer, BambuStudio, SuperSlicer)

## Examples

See `docs/README.md` for comprehensive examples and use cases.

## Contributing

Contributions welcome! Please submit pull requests or open issues on GitHub.

## Credits

Inspired by the Happy Hare MMU preprocessor by moggieuk.

## License

GNU GPLv3

## Support

- Documentation: `docs/README.md`
- Issues: GitHub Issues
- Discussions: GitHub Discussions
