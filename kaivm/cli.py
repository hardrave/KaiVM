from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from kaivm.agent.runner import AgentConfig, KaiVMAgent
from kaivm.capture.daemon import main as capture_main
from kaivm.gemini.client import DEFAULT_MODEL, GeminiPlanner
from kaivm.hid.keyboard import KeyboardHID
from kaivm.hid.mouse import MouseHID
from kaivm.hid.udc import GADGET_UDC_PATH, udc_name, udc_state, usb_replug
from kaivm.util.log import get_logger, setup_logging
from kaivm.util.paths import LATEST_JPG, LIVE_MJPG, RUN_DIR

log = get_logger("kaivm.cli")


def cmd_status(_args) -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    print("UDC:", udc_name())
    print("UDC state:", udc_state())
    print("Gadget UDC bound:", GADGET_UDC_PATH.read_text().strip() if GADGET_UDC_PATH.exists() else "(missing)")
    print("hidg0 exists:", Path("/dev/hidg0").exists())
    print("hidg1 exists:", Path("/dev/hidg1").exists())

    if LATEST_JPG.exists():
        age = time.time() - LATEST_JPG.stat().st_mtime
        print("latest.jpg:", str(LATEST_JPG), f"(age {age:.2f}s)")
    else:
        print("latest.jpg: (missing)")

    if LIVE_MJPG.exists():
        print("live.mjpg:", str(LIVE_MJPG))
    else:
        print("live.mjpg: (missing)")

    return 0


def cmd_capture(args) -> int:
    argv = [
        "--device", args.device,
        "--size", args.size,
        "--in-fps", str(args.in_fps),
        "--out-fps", str(args.out_fps),
        "--live-fps", str(args.live_fps),
        "--live-queue", str(args.live_queue),
        "--warmup", str(args.warmup),
    ]
    if args.verbose:
        argv.append("--verbose")
    return capture_main(argv)


def _ensure_fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        import stat as _stat
        mode = path.stat().st_mode
        if not _stat.S_ISFIFO(mode):
            raise RuntimeError(f"{path} exists but is not a FIFO. Delete it or choose another path.")
        return
    os.mkfifo(path, 0o666)


def cmd_view(args) -> int:
    setup_logging(args.verbose)

    try:
        _ensure_fifo(LIVE_MJPG)
    except Exception as e:
        print(f"ERROR: cannot create/open FIFO {LIVE_MJPG}: {e}", file=sys.stderr)
        return 2

    import subprocess

    # Important: tell ffplay the intended framerate for a raw MJPEG pipe
    cmd = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-f",
        "mjpeg",
        "-framerate",
        str(args.fps),
        str(LIVE_MJPG),
    ]
    return subprocess.call(cmd)


def cmd_type(args) -> int:
    setup_logging(args.verbose)
    KeyboardHID().send_text(args.text)
    return 0


def cmd_mouse(args) -> int:
    setup_logging(args.verbose)
    m = MouseHID()
    if args.move:
        dx, dy = args.move
        m.move(dx, dy)
    if args.click:
        m.click(args.click)
    return 0


def cmd_usb_replug(args) -> int:
    setup_logging(args.verbose)
    usb_replug(settle=args.settle)
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    # Import the app to ensure it loads correctly
    from kaivm.server import app
    print(f"Starting server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_run(args) -> int:
    setup_logging(args.verbose)

    viewer_proc = None
    if args.view:
        import subprocess
        viewer_proc = subprocess.Popen(
            ["kaivm", "view", "--fps", str(args.view_fps)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    try:
        planner = GeminiPlanner(
            model=args.model,
            thinking_level=args.thinking_level,
            timeout_steps=2,
        )
        agent = KaiVMAgent(
            planner=planner,
            kbd=KeyboardHID(),
            mouse=MouseHID(),
            cfg=AgentConfig(
                max_steps=args.max_steps,
                overall_timeout_s=args.timeout,
                step_sleep=args.step_sleep,
                dry_run=args.dry_run,
                confirm=args.confirm,
                allow_danger=args.allow_danger,
                do_replug=not args.no_replug,
            ),
        )
        result = agent.run(args.instruction)
        print(result)
        return 0
    finally:
        if viewer_proc:
            try:
                viewer_proc.terminate()
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kaivm")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("capture", help="Run capture loop in foreground (latest.jpg + live FIFO)")
    sp.add_argument("--device", default="/dev/video0")
    sp.add_argument("--size", default="1280x720")
    sp.add_argument("--in-fps", type=int, default=30)
    sp.add_argument("--out-fps", type=float, default=8.0)
    sp.add_argument("--live-fps", type=float, default=0.0, help="0 = unlimited")
    sp.add_argument("--live-queue", type=int, default=2)
    sp.add_argument("--warmup", type=float, default=2.0)
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("view", help="Live view (MJPEG FIFO)")
    sp.add_argument("--fps", type=int, default=30, help="Input framerate hint for ffplay")
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("run", help='Run agent loop: kaivm run "..."')
    sp.add_argument("instruction")
    sp.add_argument("--model", default=DEFAULT_MODEL)
    sp.add_argument("--thinking-level", default="low", help="Flash supports minimal/low/medium/high")
    sp.add_argument("--max-steps", type=int, default=30)
    sp.add_argument("--timeout", type=float, default=120.0)
    sp.add_argument("--step-sleep", type=float, default=0.15)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--confirm", action="store_true")
    sp.add_argument("--allow-danger", action="store_true")
    sp.add_argument("--no-replug", action="store_true")
    sp.add_argument("--view", action="store_true")
    sp.add_argument("--view-fps", type=int, default=30)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("type", help='Type text via HID: kaivm type "hello\\n"')
    sp.add_argument("text")
    sp.set_defaults(func=cmd_type)

    sp = sub.add_parser("mouse", help="Mouse actions")
    sp.add_argument("--move", nargs=2, type=int, metavar=("DX", "DY"))
    sp.add_argument("--click", choices=["left", "right", "middle"])
    sp.set_defaults(func=cmd_mouse)

    sp = sub.add_parser("usb", help="USB helper commands")
    usbsub = sp.add_subparsers(dest="usb_cmd", required=True)
    sp2 = usbsub.add_parser("replug", help="Soft re-enumerate gadget (unbind/rebind UDC)")
    sp2.add_argument("--settle", type=float, default=1.0)
    sp2.set_defaults(func=cmd_usb_replug)

    sp = sub.add_parser("serve", help="Run the kaivm web server")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(func=cmd_serve)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "verbose"):
        args.verbose = False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

