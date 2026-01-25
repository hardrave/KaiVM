from __future__ import annotations

import argparse

from kaivm.capture.ffmpeg_mjpeg import run_capture_loop
from kaivm.util.log import setup_logging
from kaivm.util.paths import LATEST_JPG


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="kaivm-capture")
    p.add_argument("--device", default="/dev/video0")
    p.add_argument("--size", default="1280x720")
    p.add_argument("--in-fps", type=int, default=30)
    p.add_argument("--out-fps", type=float, default=1.5)
    p.add_argument("--warmup", type=float, default=12.0)
    p.add_argument("--latest", default=str(LATEST_JPG))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    run_capture_loop(
        latest_path=LATEST_JPG.__class__(args.latest),
        warmup_seconds=args.warmup,
        device=args.device,
        size=args.size,
        input_fps=args.in_fps,
        out_fps=args.out_fps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

