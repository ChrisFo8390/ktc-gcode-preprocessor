# Quick Start Guide

Get the G-code preprocessor running in 5 minutes!

## Installation (2 minutes)

```bash
cd ~
git clone https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git
cd ktc-gcode-preprocessor
./install.sh
```

Follow the prompts:
1. Answer "1" for Moonraker integration (recommended). This also adds
   `[gcode_preprocessor]` and the update manager entry to `moonraker.conf`.
2. Answer "1" to restart Klipper and Moonraker

## Configuration (1 minute)

Add to your `printer.cfg`:

```ini
[include gcode-preprocessor/preprocessor.cfg]
```

Save and restart Klipper:
```bash
sudo systemctl restart klipper
```

## Test It (2 minutes)

### Option 1: Upload a Test File
1. Upload the included test file to Mainsail/Fluidd:
   - File: `~/ktc-gcode-preprocessor/examples/test_sample.gcode`
2. Check the first line - it should now say:
   ```gcode
   ; processed by ktc-gcode preprocessor
   ```
3. Look for inserted cooldown commands:
   ```gcode
   ; T1 no longer needed - cooling down
   M104 T1 S0
   ```

### Option 2: Manual Processing
```gcode
PREPROCESS_GCODE_FILE FILE=/path/to/your/file.gcode
```

### Option 3: List Processors
```gcode
LIST_GCODE_PROCESSORS
```

You should see:
- token_replacer
- idle_tool_shutdown

## What It Does

✅ **Automatically cools down tools** after their last use
- T1 used, then T2 → `M104 T1 S0` inserted
- Saves energy and reduces oozing

✅ **Extracts metadata** from slicer comments
- Tool count, colors, materials, temperatures

✅ **Replaces placeholders** in your macros
- `!tool_count!` → actual number
- `!colors!` → hex color codes

## Customize (optional)

Edit `~/printer_data/config/gcode-preprocessor/preprocessor.cfg`:

```ini
# Customize idle tool shutdown
[gcode_preprocessor idle_tool_shutdown]
exclude_tools: 0  # Optional: exclude T0 from shutdown
```

## Next Steps

- Read full docs: `README.md`
- Try placeholders in your macros
- Write custom processors

## Troubleshooting

**Files not being preprocessed?**
- Check: `tail -f ~/printer_data/logs/klippy.log`
- Verify config is included in printer.cfg

**Already preprocessed warning?**
- Files are only processed once
- Re-upload to process again

**Need help?**
- Full documentation: `README.md`
- GitHub Issues for bugs

## Example: Using Placeholders

Add to your `START_PRINT` macro:

```gcode
[gcode_macro START_PRINT]
gcode:
    M118 Starting print with !tool_count! tools
    M118 Colors: !colors!
    M118 Materials: !materials!
```

After preprocessing, this becomes:
```gcode
M118 Starting print with 3 tools
M118 Colors: ff0000,00ff00,0000ff
M118 Materials: PLA,PETG,ABS
```

---

That's it! Your G-code preprocessor is now running and automatically optimizing your prints.
