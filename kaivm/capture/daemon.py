from __future__ import annotations

import argparse
from pathlib import Path

from kaivm.capture.ffmpeg_mjpeg import run_capture_loop
from kaivm.util.log import setup_logging
from kaivm.util.paths import LATEST_JPG, LIVE_MJPG


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="kaivm-capture")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--size", default="1280x720")
    p.add_argument("--in-fps", type=int, default=30)

    # Agent cadence
    p.add_argument("--out-fps", type=float, default=8.0)

    # Live stream cadence (0 = unlimited)
    p.add_argument("--live-fps", type=float, default=0.0)

    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--latest", default=str(LATEST_JPG))
    p.add_argument("--live", default=str(LIVE_MJPG))
    p.add_argument("--live-queue", type=int, default=2, help="Small queue to keep latency low (drops frames under load)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    live_fps = None if args.live_fps <= 0 else float(args.live_fps)

    run_capture_loop(
        latest_path=Path(args.latest),
        warmup_seconds=args.warmup,
        device=args.device,
        size=args.size,
        input_fps=args.in_fps,
        out_fps=args.out_fps,
        live_path=Path(args.live),
        live_fps=live_fps,
        live_queue_size=args.live_queue,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

