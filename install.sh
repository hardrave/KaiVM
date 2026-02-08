#!/usr/bin/env bash
#
# kaiVM Installer for Raspberry Pi
# ------------------------------
# Installs dependencies, sets up the Python environment, configures
# hardware (USB gadget, camera), and enables systemd services.
#
# Run as root: sudo ./install.sh
#

set -euo pipefail

# --- Colors & Formatting ---
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}${BOLD}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}${BOLD}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}${BOLD}[WARN]${NC} $1"; }
log_err() { echo -e "${RED}${BOLD}[ERR]${NC} $1"; }
step() { echo -e "\n${CYAN}${BOLD}==>${NC} ${BOLD}$1${NC}"; }

# --- header ---
clear
echo -e "${CYAN}${BOLD}"
echo "  _         ___   ____  __  "
echo " | |__ __ _(_) \ / /  \/  | "
echo ' | / // _` | |\ V /| |\/| | '
echo " |_\_\\\\__,_|_| \_/ |_|  |_| "
echo "                            "
echo "  KaiVM Installer for RPi   "
echo -e "${NC}"
echo "---------------------------------------------------"

# --- Checks ---
if [[ $EUID -ne 0 ]]; then
   log_err "This script must be run as root."
   echo "Try: sudo ./install.sh"
   exit 1
fi

REAL_USER="${SUDO_USER:-$(whoami)}"
if [[ "$REAL_USER" == "root" ]]; then
    log_warn "Running as root user directly? Ideally run as a normal user with sudo."
    read -p "Continue as root? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then exit 1; fi
fi

INSTALL_DIR="$(pwd)"
SERVICE_DIR="/etc/systemd/system"
CONFIG_FILE="/boot/firmware/config.txt"
# Fallback for older Pi OS versions
if [[ ! -f "$CONFIG_FILE" ]]; then
    CONFIG_FILE="/boot/config.txt"
fi

log_info "Installing for user: ${BOLD}$REAL_USER${NC}"
log_info "Install directory:   ${BOLD}$INSTALL_DIR${NC}"
log_info "Config file:         ${BOLD}$CONFIG_FILE${NC}"

# --- 1. System Dependencies ---
step "Installing System Dependencies"
apt-get update
# python3-venv: for creating virtual environment
# ffmpeg: for video capture
# python3-dev, build-essential: for compiling some python libs if needed
apt-get install -y python3 python3-venv python3-pip python3-dev ffmpeg build-essential git

log_success "Dependencies installed."

# --- 2. Python Environment ---
step "Setting up Python Environment"

if [[ ! -d ".venv" ]]; then
    log_info "Creating virtual environment..."
    python3 -m venv .venv
else
    log_info "Virtual environment already exists."
fi

log_info "Installing python packages..."
# Install in editable mode so changes to source are reflected immediately
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e .

log_success "Python environment ready."

# --- 3. Hardware Configuration (USB Gadget & SPI) ---
step "Configuring Hardware"

echo -e "${YELLOW}Hardware configuration involves editing $CONFIG_FILE, enabling kernel modules, and setting udev rules.${NC}"
echo -e "${YELLOW}This is required for KaiVM to function correctly (USB HID, SPI display).${NC}"
echo -e "${YELLOW}It is PREFERABLE to configure this manually if you have a custom setup.${NC}"
read -p "Do you want to proceed with AUTOMATIC hardware configuration? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then

    # 3.1 Kernel Modules
    if ! grep -q "libcomposite" /etc/modules-load.d/modules.conf 2>/dev/null && \
       ! grep -q "libcomposite" /etc/modules 2>/dev/null && \
       [ ! -f "/etc/modules-load.d/libcomposite.conf" ]; then
        log_info "Enabling libcomposite module..."
        echo "libcomposite" > /etc/modules-load.d/libcomposite.conf
    else
        log_success "libcomposite module already configured."
    fi

    # 3.2 Boot Config (OTG + SPI)
    log_info "Updating $CONFIG_FILE..."

    # OTG
    if ! grep -qE '^\s*dtoverlay=dwc2' "$CONFIG_FILE"; then
        log_info "Adding dtoverlay=dwc2..."
        echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$CONFIG_FILE"
    else
        log_success "dtoverlay=dwc2 already present."
    fi

    # SPI
    if ! grep -qE '^\s*dtparam=spi=on' "$CONFIG_FILE"; then
        log_info "Adding dtparam=spi=on..."
        echo "dtparam=spi=on" >> "$CONFIG_FILE"
    else
        log_success "SPI (dtparam=spi=on) already enabled."
    fi

    # 3.3 Install Gadget Script
    log_info "Installing gadget setup script..."
    install -m 0755 scripts/kaivm-hid-gadget.py /usr/local/sbin/kaivm-hid-gadget.py

    # 3.4 Udev Rules
    log_info "Setting up udev rules..."
    cat >/etc/udev/rules.d/99-hidg.rules <<'RULE'
KERNEL=="hidg*", MODE="0660", GROUP="plugdev"
RULE
    udevadm control --reload-rules
    udevadm trigger

    # 3.5 User Groups
    log_info "Adding $REAL_USER to hardware groups..."
    for grp in plugdev video gpio spi i2c; do
        usermod -aG "$grp" "$REAL_USER" || log_warn "Group $grp might not exist, skipping."
    done

    log_success "Hardware configuration complete."

else
    log_warn "Skipping hardware configuration."
    echo -e "${RED}IMPORTANT: You must manually configure USB OTG, SPI, and permissions for KaiVM to work.${NC}"
fi

# --- 4. API Key Configuration ---
step "Configuration"

ENV_FILE="/etc/default/kaivm"
API_KEY=""

if [[ -f "$ENV_FILE" ]]; then
    log_info "Existing configuration found at $ENV_FILE"
    # Optional: Source it to check if key exists, but simplest is just to ask if they want to change it.
else
    touch "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE" # Secrets should be root-owned generally, but service runs as user.
    # Actually, systemd EnvironmentFile needs to be readable by the process or systemd reads it as root before exec?
    # Systemd reads it. So root:root 600 is fine.
fi

echo -e "${YELLOW}Enter your Gemini API Key (leave empty to skip/configure later):${NC}"
read -r -s API_KEY_INPUT
echo

if [[ -n "$API_KEY_INPUT" ]]; then
    echo "GEMINI_API_KEY=$API_KEY_INPUT" > "$ENV_FILE"
    log_success "API key saved to $ENV_FILE"
else
    if ! grep -q "GEMINI_API_KEY" "$ENV_FILE"; then
        log_warn "No API Key provided. You will need to edit $ENV_FILE later."
        echo "# GEMINI_API_KEY=your_key_here" >> "$ENV_FILE"
    fi
fi

# --- 5. Systemd Services ---
step "Installing Services"

# Helper to process and install a service file
install_service() {
    local src="$1"
    local destname="$2"
    local dest="$SERVICE_DIR/$destname"
    
    log_info "Installing $destname..."
    
    # Read file, replace variables, write to dest
    # We replace:
    # User=kai -> User=$REAL_USER
    # Group=kai -> Group=$REAL_USER (or video for capture if needed, let's look at source)
    # /home/kai/kaivm -> $INSTALL_DIR
    
    # We need to respect the specific Group requirements of the original files:
    # kaivm-capture.service: Group=video
    # kaivm-agent.service: Group=kai (user's primary group)
    
    # Get primary group of real user
    local REAL_GROUP
    REAL_GROUP=$(id -gn "$REAL_USER")
    
    cp "$src" "$dest"
    
    # Replace User=kai with User=$REAL_USER
    sed -i "s/^User=kai/User=$REAL_USER/g" "$dest"
    
    # Replace Group=kai with Group=$REAL_GROUP (Preserve Group=video in capture service)
    sed -i "s/^Group=kai/Group=$REAL_GROUP/g" "$dest"
    
    # Replace paths
    # We escape slashes in INSTALL_DIR for sed
    local ESCAPED_DIR
    ESCAPED_DIR=$(echo "$INSTALL_DIR" | sed 's/\//\\\//g')
    
    sed -i "s|/home/kai/kaivm|$ESCAPED_DIR|g" "$dest"
    
    # Inject EnvironmentFile into kaivm-agent.service if not present
    if [[ "$destname" == "kaivm-agent.service" ]]; then
        if ! grep -q "EnvironmentFile" "$dest"; then
            sed -i "/^\[Service\]/a EnvironmentFile=-/etc/default/kaivm" "$dest"
        fi
    fi
}

install_service "scripts/kaivm-hid.service" "kaivm-hid.service"
install_service "scripts/kaivm-capture.service" "kaivm-capture.service"
install_service "scripts/kaivm-agent.service" "kaivm-agent.service"

log_info "Reloading systemd..."
systemctl daemon-reload

log_info "Enabling services..."
systemctl enable kaivm-hid.service
systemctl enable kaivm-capture.service
systemctl enable kaivm-agent.service

log_info "Starting services..."
# HID service first
systemctl restart kaivm-hid.service || log_warn "Failed to start HID service (might need reboot first)"
# Capture and Agent might fail if reboot needed for permissions/modules
systemctl restart kaivm-capture.service || log_warn "Failed to start Capture service"
systemctl restart kaivm-agent.service || log_warn "Failed to start Agent service"

log_success "Services installed."

# --- Finalize ---
step "Installation Complete!"
echo -e "
${GREEN}Success!${NC} KaiVM has been installed.

${BOLD}Next Steps:${NC}
1. If this is the first install, ${BOLD}REBOOT your Pi${NC} to enable the USB gadget and permissions.
   ${CYAN}sudo reboot${NC}

2. After reboot, check status:
   ${CYAN}systemctl status kaivm-agent${NC}

3. View logs:
   ${CYAN}journalctl -u kaivm-agent -f${NC}

4. Access the UI:
   http://<raspberry-pi-ip>:8000

"

exit 0
