# KaiVM

**The Agentic Hardware KVM**

KaiVM turns a Raspberry Pi into an autonomous AI agent that can physically control any computer. By acting as a USB Keyboard/Mouse and capturing video output via HDMI, KaiVM can see, reason, and interact with target machines regardless of their operating system or state—even in BIOS or during a crash.

Powered by **Google Gemini 3**, KaiVM bridges the gap between multimodal AI and physical hardware.

---

## Key Features

* **Universal Control:** Works on Windows, Linux, macOS, and bare-metal servers. No software installation is required on the target machine.
* **Visual Reasoning:** Uses Gemini 3's multimodal capabilities to analyze the screen in real-time.
* **Absolute Mouse Precision:** Implements a custom USB HID descriptor (Digitizer) to map screen coordinates 1:1, eliminating AI mouse drift and "hallucinated" clicks.
* **Autonomous Recovery:** Can navigate BIOS menus, bootloaders (GRUB), and recovery screens where software agents fail.
* **Event Watchdog:** Define visual triggers (e.g., "If error popup appears") to execute actions automatically.


---

## Architecture

KaiVM acts as a "Man-in-the-Middle" device. It captures video output and injects USB input.

1.  **Video Flow:** The target computer sends HDMI video to the Capture Card. The Server processes this raw video into screenshots with a grid overlay and sends them to the Gemini 3 API.
2.  **Logic Flow:** Gemini 3 analyzes the screenshot and returns a JSON Action Plan.
3.  **Control Flow:** The Server translates the plan into HID commands (Keystrokes/Clicks) which are sent via the USB Gadget interface back to the target computer.

---

## Hardware Requirements

To build a KaiVM unit, you need:

1.  **Raspberry Pi 4:** The Pi 4 is required for its specific USB-C OTG capabilities (`dwc2`).
2.  **HDMI-to-USB Capture Card:** Any generic dongle based on the **MacroSilicon MS2109** chip (available cheaply online).
3.  **USB-C Splitter (Power + Data):** Essential for the Pi 4. You need a cable that splits the USB-C port into a Power input and a Data (USB-A) output to connect to the target PC.
4.  **Target Computer:** The machine you wish to control.
5.  **(Optional) SPI Display:** Supports ST7789-based 2-inch IPS displays for local status monitoring.

---

## Installation

### 1. Prepare the Raspberry Pi
Flash a fresh image of **Raspberry Pi OS Lite (64-bit)**.

### 2. Install KaiVM
Clone the repository and run the installer script as root. This script handles dependencies (`ffmpeg`, `python-venv`), configures the Linux Kernel USB Gadget modules, and sets up systemd services.

```bash
git clone https://github.com/yourusername/kaivm.git
cd kaivm
sudo ./install.sh
```

Note: The installer will modify /boot/firmware/config.txt to enable USB OTG (dtoverlay=dwc2) and SPI. A reboot is required after installation.

### 3. Configuration
The installer will prompt you for your Gemini API Key. If you skip this, you can manually edit the environment file later:

```bash
sudo nano /etc/default/kaivm
# Add: GEMINI_API_KEY=your_key_here
```

---

## Usage

### Web Interface
Once installed and rebooted, access the dashboard at:

```
http://<raspberry-pi-ip>:8000
```

* **View Stream:** Low-latency MJPEG stream of the target PC.
* **Chat/Instruct:** Type natural language commands (e.g., "Open the browser and find the weather in Tokyo").
* **Scheduler:** Set up recurring tasks (e.g., "Check disk space every morning").

---

## Advanced Configuration

### Mouse Calibration
KaiVM uses an absolute mouse mode, but different operating systems scale coordinates differently. If clicks are inaccurate:

1. Run `kaivm calibrate` (or use the Calibrate button in the Web UI).
2. The system will move the mouse to corners, detect the cursor position using visual diffing, and calculate the scaling factor.
3. Results are saved to `~/.config/kaivm/calibration.txt`.


---

## Project Structure

* `install.sh`: Main setup script. Configures Kernel, udev rules, and Python env.
* `kaivm/agent/`: Core logic for the AI loop (Reasoning -> Action).
* `kaivm/hid/`: Low-level USB gadget drivers (Keyboard, Relative Mouse, Absolute Mouse).
* `kaivm/gemini/`: Client for Google Gemini 3 API, including prompt engineering and image preprocessing (Grid Overlay).
* `kaivm/server.py`: FastAPI backend and WebSocket handler.
* `scripts/`: Systemd unit files and hardware initialization scripts.

---

## Troubleshooting

* **"USB Device Not Recognized" on Target:** Ensure you are using a data-capable USB-C splitter. The Pi must be powered and have data lines connected to the target.
* **Video Feed is Black:** Check if the target PC is sleeping. Ensure the HDMI capture card is recognized (`ls /dev/video*`).
* **Mouse Clicks are Off:** Run the calibration tool. Ensure the target OS resolution is standard (1920x1080 is recommended for best OCR results).
