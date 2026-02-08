import time
import subprocess
import socket
import threading
import logging
from pathlib import Path
from PIL import Image, ImageFont, ImageDraw

# Hardware Imports
try:
    from luma.core.interface.serial import spi
    from luma.lcd.device import st7789
    import RPi.GPIO as GPIO
    HAS_HARDWARE = True
except ImportError:
    HAS_HARDWARE = False
    st7789 = object
    spi = object
    GPIO = None

log = logging.getLogger("kaivm.display")

# Path to logo
LOGO_PATH = Path(__file__).parent / "static" / "kaivm-small.png"

def get_ip_address():
    try:
        # Connect to a public DNS server to get the local interface IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(('8.8.8.8', 1)) 
        IP = s.getsockname()[0]
        s.close()
        return IP
    except Exception:
        try:
            cmd = "hostname -I | cut -d' ' -f1"
            IP = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()
            return IP if IP else "No IP"
        except Exception:
            return "No IP"

def get_mdns_hostname():
    try:
        return socket.gethostname() + ".local"
    except:
        return "kaivm.local"

class DisplayManager:
    def __init__(self):
        self.device = None
        self.running = False
        self.thread = None
        self.state = None
        self.lock = threading.Lock()
        
        # Assets
        self.font = None
        self.font_small = None
        self.font_large = None
        self.font_bold = None
        self.logo_img = None
        
        # Hardware Config (Waveshare 2inch LCD)
        self.bl_pin = 18
        self.dc_pin = 25
        self.rst_pin = 27
        self.spi_port = 0
        self.spi_device = 0
        self.spi_speed = 24000000

    def start(self, app_state):
        self.state = app_state
        if not HAS_HARDWARE:
            log.warning("Display hardware dependencies not met. Display service disabled.")
            return

        try:
            self._init_device()
            self._load_assets()
            
            # Show Boot Screen
            self._show_boot()
            
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            log.info("Display service started.")
        except Exception as e:
            log.error(f"Failed to initialize display: {e}")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.device:
            try:
                with self.lock:
                    self.device.cleanup()
            except:
                pass
        if GPIO:
             try:
                 GPIO.cleanup()
             except: pass

    def _init_device(self):
        try:
            # Backlight control
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.bl_pin, GPIO.OUT)
            GPIO.output(self.bl_pin, GPIO.HIGH)

            # Initialize SPI interface
            serial = spi(port=self.spi_port, device=self.spi_device, 
                         gpio_DC=self.dc_pin, gpio_RST=self.rst_pin, 
                         bus_speed_hz=self.spi_speed)
            
            # Initialize ST7789
            # User specified rotate=2 (180 deg) and 320x240 dimensions.
            # Maintained as requested for correct orientation.
            self.device = st7789(serial, width=320, height=240, rotate=2)
            
            # Clear screen
            self.device.clear()
            self.device.show()
        except Exception as e:
            log.warning(f"Display hardware init failed: {e}")
            raise

    def _load_assets(self):
        # Fonts
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        try:
            self.font = ImageFont.truetype(font_path, 18)
            self.font_small = ImageFont.truetype(font_path, 14)
            self.font_large = ImageFont.truetype(font_bold_path, 24)
            self.font_bold = ImageFont.truetype(font_bold_path, 18)
        except IOError:
            # Fallback
            self.font = ImageFont.load_default()
            self.font_small = ImageFont.load_default()
            self.font_large = ImageFont.load_default()
            self.font_bold = ImageFont.load_default()

        # Logo
        if LOGO_PATH.exists():
            try:
                raw_logo = Image.open(LOGO_PATH).convert("RGBA")
                # Resize logo to fit header (height ~30-40px)
                h = 40
                aspect = raw_logo.width / raw_logo.height
                w = int(h * aspect)
                self.logo_img = raw_logo.resize((w, h), Image.Resampling.LANCZOS)
            except Exception as e:
                log.error(f"Failed to load logo: {e}")

    def _show_boot(self):
        if not self.device: return
        with self.lock:
            # Landscape Dimensions
            w, h = 320, 240
            img = Image.new("RGB", (w, h), "black")
            draw = ImageDraw.Draw(img)
            
            # Draw Logo Centered
            if self.logo_img:
                x = (w - self.logo_img.width) // 2
                y = (h - self.logo_img.height) // 2 - 20
                img.paste(self.logo_img, (x, y), self.logo_img)
            
            draw.text((20, h - 40), "Booting KaiVM...", font=self.font, fill="white")
            
            # Direct display (no software rotation)
            self.device.display(img)

    def _loop(self):
        while self.running:
            try:
                self._update_display()
            except Exception as e:
                log.error(f"Error in display loop: {e}")
            time.sleep(1)

    def _update_display(self):
        if not self.device:
            return

        # 1. Gather Data
        ip = get_ip_address()
        mdns = get_mdns_hostname()
        
        mode = "Manual"
        status = "Idle"
        services_ok = True
        
        # Check Services
        if self.state:
            # Simple check: if state exists, services are largely "loaded" 
            # as Display is started at end of lifespan startup.
            if self.state.agent_running:
                mode = "Agent"
                status = self.state.last_status or "Working"
            elif self.state.events.running:
                mode = "Events"
                status = "Monitoring"
            elif self.state.scheduler.running:
                mode = "Scheduler"
                status = "Active"
            else:
                mode = "Manual"
                status = "Idle"
        else:
            services_ok = False

        # Truncate status if too long
        if len(status) > 20:
            status = status[:17] + "..."

        # 2. Render Image
        # Landscape Dimensions (320x240)
        width = 320
        height = 240
        
        img = Image.new("RGB", (width, height), "black")
        draw = ImageDraw.Draw(img)
        
        # --- ERROR STATE ---
        if not services_ok:
            draw.rectangle((0, 0, width, height), fill="red")
            draw.text((20, 80), "ERROR", font=self.font_large, fill="white")
            draw.text((20, 120), "Services Failed", font=self.font, fill="white")
            with self.lock:
                self.device.display(img)
            return

        # --- NORMAL STATE ---
        
        # Header (Top 50px)
        header_h = 50
        # Logo Centered
        if self.logo_img:
             # Center logo horizontally
             logo_x = (width - self.logo_img.width) // 2
             img.paste(self.logo_img, (logo_x, 5), self.logo_img)
        
        # Separator
        draw.line((0, header_h, width, header_h), fill="grey")
        
        # Body (Middle)
        body_y = header_h + 15
        
        # System
        sys_val = "Online" if services_ok else "Error"
        sys_col = "green" if services_ok else "red"
        
        draw.text((10, body_y), "System:", font=self.font_bold, fill="white")
        draw.text((110, body_y), sys_val, font=self.font, fill=sys_col)
        
        body_y += 30

        # Mode
        draw.text((10, body_y), "Mode:", font=self.font_bold, fill="yellow")
        draw.text((110, body_y), mode, font=self.font, fill="white")
        
        # Status (Same Line)
        body_y += 30
        draw.text((10, body_y), "Status:", font=self.font_bold, fill="cyan")
        draw.text((110, body_y), status, font=self.font, fill="white")
        
        # Footer (Bottom 60px)
        footer_y = height - 60
        draw.line((0, footer_y, width, footer_y), fill="grey")
        
        footer_y += 10
        # Increased font size for IP/mDNS
        draw.text((10, footer_y), f"IP: {ip}", font=self.font, fill="lightgrey")
        draw.text((10, footer_y + 20), f"mDNS: {mdns}", font=self.font, fill="lightgrey")

        # 3. Push to Display
        # Direct display (no software rotation)
        
        with self.lock:
            self.device.display(img)
