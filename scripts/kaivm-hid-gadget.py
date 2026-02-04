#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

GADGET_DIR = Path("/sys/kernel/config/usb_gadget")
NAME = "kaivm"
G = GADGET_DIR / NAME

UDC_CLASS = Path("/sys/class/udc")

# Keyboard descriptor (63 bytes) from kernel docs
KBD_DESC = bytes.fromhex(
    "05 01 09 06 a1 01 05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02 "
    "95 01 75 08 81 03 95 05 75 01 05 08 19 01 29 05 91 02 95 01 75 03 "
    "91 03 95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00 c0"
)

# Boot mouse (3 bytes) descriptor (50 bytes)
MOUSE_DESC = bytes.fromhex(
    "05 01 09 02 a1 01 09 01 a1 00 05 09 19 01 29 03 15 00 25 01 95 03 "
    "75 01 81 02 95 01 75 05 81 03 05 01 09 30 09 31 15 81 25 7f 75 08 "
    "95 02 81 06 c0 c0"
)

# Absolute mouse (Generic Desktop / Mouse)
# Uses Usage Page: Generic Desktop (0x01), Usage: Mouse (0x02).
# Added Physical Min/Max to match Logical to ensure linear mapping on all OSs.
ABS_MOUSE_DESC = bytes.fromhex(
    "05 01 09 02 a1 01 05 01 09 01 a1 00 05 09 19 01 29 03 15 00 25 01 "
    "95 03 75 01 81 02 95 01 75 05 81 03 05 01 09 30 09 31 15 00 26 ff "
    "7f 35 00 46 ff 7f 75 10 95 02 81 02 c0 c0"
)

def udc_name() -> str:
    return next(UDC_CLASS.iterdir()).name

def write(p: Path, data: str | bytes) -> None:
    # Wait for file to appear (ConfigFS race?)
    deadline = time.time() + 2.0
    while not p.exists():
        if time.time() > deadline:
            break
        time.sleep(0.01)

    if isinstance(data, str):
        p.write_text(data)
    else:
        p.write_bytes(data)

def unbind() -> None:
    udc = G / "UDC"
    if udc.exists():
        try:
            udc.write_text("")
        except Exception:
            pass

def stop() -> None:
    if not G.exists():
        return
    unbind()
    # remove links first
    for link in ["hid.usb0", "hid.usb1", "hid.usb2"]:
        try:
            (G / "configs/c.1" / link).unlink()
        except Exception:
            pass
    # remove dirs
    for p in [
        G / "configs/c.1/strings/0x409",
        G / "configs/c.1",
        G / "functions/hid.usb0",
        G / "functions/hid.usb1",
        G / "functions/hid.usb2",
        G / "strings/0x409",
        G,
    ]:
        try:
            p.rmdir()
        except Exception:
            pass

def start() -> None:
    if not GADGET_DIR.exists():
        raise RuntimeError("configfs not mounted? /sys/kernel/config/usb_gadget missing")

    stop()
    G.mkdir(parents=True, exist_ok=True)

    # IDs
    write(G / "idVendor", "0x1d6b")   # Linux Foundation (dev)
    write(G / "idProduct", "0x0104")  # Multifunction Composite Gadget
    write(G / "bcdUSB", "0x0200")
    write(G / "bcdDevice", "0x0100")

    # composite: interface-defined
    write(G / "bDeviceClass", "0x00")
    write(G / "bDeviceSubClass", "0x00")
    write(G / "bDeviceProtocol", "0x00")

    (G / "strings/0x409").mkdir(parents=True, exist_ok=True)
    write(G / "strings/0x409/serialnumber", "0123456789")
    write(G / "strings/0x409/manufacturer", "kaiVM")
    write(G / "strings/0x409/product", "kaiVM HID")

    (G / "configs/c.1/strings/0x409").mkdir(parents=True, exist_ok=True)
    write(G / "configs/c.1/strings/0x409/configuration", "HID")
    write(G / "configs/c.1/MaxPower", "250")
    write(G / "configs/c.1/bmAttributes", "0xC0")  # self-powered

    # Keyboard function -> /dev/hidg0
    (G / "functions/hid.usb0").mkdir(parents=True, exist_ok=True)
    write(G / "functions/hid.usb0/protocol", "1")
    write(G / "functions/hid.usb0/subclass", "1")
    write(G / "functions/hid.usb0/report_length", "8")
    write(G / "functions/hid.usb0/no_out_endpoint", "1")
    write(G / "functions/hid.usb0/report_desc", KBD_DESC)

    # Mouse function -> /dev/hidg1
    (G / "functions/hid.usb1").mkdir(parents=True, exist_ok=True)
    write(G / "functions/hid.usb1/protocol", "2")
    write(G / "functions/hid.usb1/subclass", "1")
    write(G / "functions/hid.usb1/report_length", "3")
    write(G / "functions/hid.usb1/no_out_endpoint", "1")
    write(G / "functions/hid.usb1/report_desc", MOUSE_DESC)

    # Absolute Mouse function -> /dev/hidg2
    # Protocol 0 (None) or 2 (Mouse)? 0 is usually safer for non-boot custom devices.
    # Report length: 1 byte buttons + 4 bytes X/Y (2*2) = 5 bytes
    (G / "functions/hid.usb2").mkdir(parents=True, exist_ok=True)
    write(G / "functions/hid.usb2/protocol", "0") 
    write(G / "functions/hid.usb2/subclass", "0")
    write(G / "functions/hid.usb2/report_length", "5")
    write(G / "functions/hid.usb2/no_out_endpoint", "1")
    write(G / "functions/hid.usb2/report_desc", ABS_MOUSE_DESC)

    # Link into config
    os.symlink(str(G / "functions/hid.usb0"), str(G / "configs/c.1/hid.usb0"))
    os.symlink(str(G / "functions/hid.usb1"), str(G / "configs/c.1/hid.usb1"))
    os.symlink(str(G / "functions/hid.usb2"), str(G / "configs/c.1/hid.usb2"))

    # Bind
    u = udc_name()
    write(G / "UDC", u)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "stop", "restart"])
    args = ap.parse_args()

    if args.cmd == "start":
        start()
    elif args.cmd == "stop":
        stop()
    else:
        stop()
        time.sleep(0.5)
        start()

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

