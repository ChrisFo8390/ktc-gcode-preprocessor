#!/bin/bash
# KTC G-code Preprocessor Installation Script
#
# This script installs the G-code preprocessor system for Klipper
# It can be used standalone or integrated with klipper-toolchanger
#
# Usage (from a clone of the repository):
#   git clone https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git ~/ktc-gcode-preprocessor
#   ~/ktc-gcode-preprocessor/install.sh

REPO_URL="https://github.com/ChrisFo8390/ktc-gcode-preprocessor.git"
REPO_NAME="ktc-gcode-preprocessor"
KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
MOONRAKER_PATH="${MOONRAKER_PATH:-${HOME}/moonraker}"
CONFIG_PATH="${CONFIG_PATH:-${HOME}/printer_data/config}"

set -eu
export LC_ALL=C

# Install from the directory this script lives in when it is a clone of the
# repository, otherwise clone it to ~/ktc-gcode-preprocessor
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "${SCRIPT_DIR}/klipper/extras/gcode_preprocessor.py" ]; then
    INSTALL_PATH="${SCRIPT_DIR}"
else
    INSTALL_PATH="${INSTALL_PATH:-${HOME}/${REPO_NAME}}"
fi

function preflight_checks {
    if [ "$EUID" -eq 0 ]; then
        echo "[PRE-CHECK] This script must not be run as root!"
        exit 1
    fi

    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F 'klipper.service')" ]; then
        printf "[PRE-CHECK] Klipper service found! Continuing...\n\n"
    else
        echo "[ERROR] Klipper service not found, please install Klipper first!"
        exit 1
    fi

    if [ ! -d "${KLIPPER_PATH}/klippy/extras" ]; then
        echo "[ERROR] Klipper not found at ${KLIPPER_PATH} (set KLIPPER_PATH=...)"
        exit 1
    fi
}

function check_download {
    local installdirname installbasename
    installdirname="$(dirname "${INSTALL_PATH}")"
    installbasename="$(basename "${INSTALL_PATH}")"

    if [ ! -d "${INSTALL_PATH}" ]; then
        echo "[DOWNLOAD] Downloading repository from ${REPO_URL}..."
        if git -C "${installdirname}" clone "${REPO_URL}" "${installbasename}"; then
            chmod +x "${INSTALL_PATH}/install.sh"
            printf "[DOWNLOAD] Download complete!\n\n"
        else
            echo "[ERROR] Download of git repository failed!"
            exit 1
        fi
    else
        printf "[DOWNLOAD] Using repository at %s\n\n" "${INSTALL_PATH}"
    fi
}

function link_klipper_modules {
    echo "[INSTALL] Linking G-code preprocessor modules to Klipper..."

    local extras="${KLIPPER_PATH}/klippy/extras"

    # Link main modules
    ln -sfn "${INSTALL_PATH}"/klipper/extras/gcode_preprocessor_base.py "${extras}/"
    ln -sfn "${INSTALL_PATH}"/klipper/extras/gcode_preprocessor.py "${extras}/"

    # Link the plugin directory as a whole, so processors added by a later
    # update are available without re-running this script. Older versions
    # created a real directory with one link per file; replace it.
    if [ -d "${extras}/preprocessors" ] && [ ! -L "${extras}/preprocessors" ]; then
        find "${extras}/preprocessors" -maxdepth 1 -type l -delete
        rm -rf "${extras}/preprocessors/__pycache__"
        if ! rmdir "${extras}/preprocessors" 2>/dev/null; then
            local backup="${extras}/preprocessors.bak.$(date +%Y%m%d%H%M%S)"
            mv "${extras}/preprocessors" "${backup}"
            echo "[WARNING] Existing files in ${extras}/preprocessors moved to ${backup}"
        fi
    fi
    ln -sfn "${INSTALL_PATH}"/klipper/extras/preprocessors "${extras}/preprocessors"

    echo "[INSTALL] Klipper modules linked successfully!"
}

function install_config {
    echo "[INSTALL] Installing configuration files..."

    mkdir -p "${CONFIG_PATH}"/gcode-preprocessor

    # Copy config file if it doesn't exist
    if [ ! -f "${CONFIG_PATH}/gcode-preprocessor/preprocessor.cfg" ]; then
        cp "${INSTALL_PATH}"/config/gcode-preprocessor.cfg "${CONFIG_PATH}"/gcode-preprocessor/preprocessor.cfg
        echo "[INSTALL] Configuration file installed to ${CONFIG_PATH}/gcode-preprocessor/preprocessor.cfg"
    else
        echo "[INFO] Configuration file already exists at ${CONFIG_PATH}/gcode-preprocessor/preprocessor.cfg"
        echo "[INFO] Review ${INSTALL_PATH}/config/gcode-preprocessor.cfg for any new settings"
    fi

    echo ""
    echo "[INFO] Add the following line to your printer.cfg to enable the preprocessor:"
    echo ""
    echo "    [include gcode-preprocessor/preprocessor.cfg]"
    echo ""
}

# Append a section to moonraker.conf unless a section with that header exists
function add_moonraker_section {
    local conf="$1" header="$2" body="$3"
    if grep -qF "[${header}]" "${conf}"; then
        echo "[INFO] [${header}] already present in ${conf} - not changed"
    else
        printf "\n[%s]\n%s\n" "${header}" "${body}" >> "${conf}"
        echo "[INSTALL] Added [${header}] to ${conf}"
    fi
}

function install_moonraker_component {
    echo -e "\n\nInstall Moonraker component for automatic G-code preprocessing?"
    echo "This enables automatic preprocessing when files are uploaded via Mainsail/Fluidd"
    echo "and adds update manager support for ${REPO_NAME} to moonraker.conf."
    echo "You can skip this and use manual preprocessing with PREPROCESS_GCODE_FILE instead."
    echo ""
    echo "1. Yes, install Moonraker component (recommended)"
    echo "2. No, skip Moonraker integration"
    read -rp "Select an option [1-2]: " moonraker_choice

    case $moonraker_choice in
        1)
            if [ ! -d "${MOONRAKER_PATH}/moonraker/components" ]; then
                echo "[WARNING] Moonraker components directory not found at ${MOONRAKER_PATH}/moonraker/components"
                echo "[WARNING] Moonraker component installation skipped."
                echo "[INFO] You can still use manual preprocessing with PREPROCESS_GCODE_FILE"
                return
            fi
            echo "[INSTALL] Installing Moonraker component..."
            ln -sfn "${INSTALL_PATH}"/moonraker/gcode_preprocessor.py "${MOONRAKER_PATH}"/moonraker/components/
            echo "[INSTALL] Moonraker component installed!"

            local conf="${CONFIG_PATH}/moonraker.conf"
            if [ -f "${conf}" ]; then
                cp "${conf}" "${conf}.bak.$(date +%Y%m%d%H%M%S)"
                add_moonraker_section "${conf}" "gcode_preprocessor" "enable_preprocessing: True"
                add_moonraker_section "${conf}" "update_manager ${REPO_NAME}" \
"type: git_repo
path: ${INSTALL_PATH}
origin: ${REPO_URL}
primary_branch: main
managed_services: klipper moonraker"
            else
                echo "[WARNING] ${conf} not found. Add the following to your moonraker.conf:"
                echo ""
                echo "[gcode_preprocessor]"
                echo "enable_preprocessing: True"
                echo ""
                echo "[update_manager ${REPO_NAME}]"
                echo "type: git_repo"
                echo "path: ${INSTALL_PATH}"
                echo "origin: ${REPO_URL}"
                echo "primary_branch: main"
                echo "managed_services: klipper moonraker"
            fi
            ;;
        2)
            echo "[INSTALL] Skipping Moonraker component installation."
            echo "[INFO] You can use manual preprocessing with PREPROCESS_GCODE_FILE"
            ;;
        *)
            echo "[ERROR] Invalid option selected!"
            exit 1
            ;;
    esac
}

function restart_services {
    echo -e "\n[POST-INSTALL] Restart Klipper and Moonraker now?"
    echo "1. Yes, restart both services"
    echo "2. No, I'll restart manually later"
    read -rp "Select an option [1-2]: " restart_choice

    case $restart_choice in
        1)
            echo "[POST-INSTALL] Restarting Klipper..."
            sudo systemctl restart klipper
            echo "[POST-INSTALL] Klipper restarted!"

            if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F 'moonraker.service')" ]; then
                echo "[POST-INSTALL] Restarting Moonraker..."
                sudo systemctl restart moonraker
                echo "[POST-INSTALL] Moonraker restarted!"
            fi
            ;;
        2)
            echo "[POST-INSTALL] Please restart services manually when ready:"
            echo "    sudo systemctl restart klipper"
            echo "    sudo systemctl restart moonraker"
            ;;
        *)
            echo "[WARNING] Invalid option, skipping restart"
            ;;
    esac
}

function show_completion_message {
    echo ""
    echo "========================================"
    echo "Installation Complete!"
    echo "========================================"
    echo ""
    echo "Next steps:"
    echo "1. Add to your printer.cfg:"
    echo "   [include gcode-preprocessor/preprocessor.cfg]"
    echo ""
    echo "2. Restart services if you haven't already:"
    echo "   sudo systemctl restart klipper"
    echo "   sudo systemctl restart moonraker"
    echo ""
    echo "3. Test the preprocessor with:"
    echo "   PREPROCESS_GCODE_FILE FILE=/path/to/file.gcode"
    echo ""
    echo "4. List available processors:"
    echo "   LIST_GCODE_PROCESSORS"
    echo ""
    echo "Documentation: ${INSTALL_PATH}/README.md"
    echo "Example test file: ${INSTALL_PATH}/examples/test_sample.gcode"
    echo ""
}

# Main installation flow
printf "\n============================================\n"
echo "  KTC G-code Preprocessor Installer"
printf "============================================\n\n"

# Run steps
preflight_checks
check_download
link_klipper_modules
install_config
install_moonraker_component
restart_services
show_completion_message
