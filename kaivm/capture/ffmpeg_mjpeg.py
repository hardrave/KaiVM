from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from kaivm.util.log import get_logger

log = get_logger("kaivm.capture.ffmpeg")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class FfmpegMJPEGReader:
    """
    Spawns ffmpeg reading /dev/video0 and emitting a MJPEG stream to stdout,
    then extracts individual JPEG frames by SOI/EOI markers.
    """

    def __init__(
        self,
        device: str = "/dev/video0",
        size: str = "1280x720",
        input_fps: int = 30,
        out_fps: float = 1.5,
        ffmpeg_path: str = "ffmpeg",
        input_format: str = "mjpeg",
    ) -> None:
        self.device = device
        self.size = size
        self.input_fps = input_fps
        self.out_fps = out_fps
        self.ffmpeg_path = ffmpeg_path
        self.input_format = input_format
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "v4l2",
            "-input_format",
            self.input_format,
            "-framerate",
            str(self.input_fps),
            "-video_size",
            self.size,
            "-i",
            self.device,
            # reduce CPU + stabilize output
            "-vf",
            f"fps={self.out_fps}",
            "-f",
            "mjpeg",
            "pipe:1",
        ]
        log.info("Starting capture: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def stop(self) -> None:
        if not self._proc:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def frames(self):
        """
        Generator of JPEG bytes.
        """
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("ffmpeg not started")

        buf = bytearray()
        SOI = b"\xff\xd8"
        EOI = b"\xff\xd9"

        while True:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                rc = self._proc.poll()
                raise RuntimeError(f"ffmpeg stdout ended (rc={rc})")
            buf.extend(chunk)

            while True:
                start = buf.find(SOI)
                if start < 0:
                    # keep buffer bounded
                    if len(buf) > 2_000_000:
                        del buf[:-1024]
                    break
                end = buf.find(EOI, start + 2)
                if end < 0:
                    # need more data
                    if start > 0:
                        del buf[:start]
                    break

                frame = bytes(buf[start : end + 2])
                del buf[: end + 2]
                yield frame


def run_capture_loop(
    latest_path: Path,
    warmup_seconds: float = 12.0,
    restart_backoff: float = 1.0,
    **reader_kwargs,
) -> None:
    """
    Single-owner capture loop. Writes latest frame to latest_path, atomically.
    Warm-up: discard first warmup_seconds worth of frames (time-based).
    """
    reader = FfmpegMJPEGReader(**reader_kwargs)
    last_ok = 0.0
    while True:
        try:
            reader.start()
            t0 = time.time()
            for jpeg in reader.frames():
                now = time.time()
                if (now - t0) < warmup_seconds:
                    continue  # discard junk/lock time
                _atomic_write(latest_path, jpeg)
                last_ok = now
        except Exception as e:
            log.warning("Capture error: %s", e)

        reader.stop()
        # if we were capturing recently, small backoff; otherwise grow a bit
        time.sleep(restart_backoff if (time.time() - last_ok) < 5 else min(5.0, restart_backoff * 2))

