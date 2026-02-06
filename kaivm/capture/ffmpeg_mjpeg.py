from __future__ import annotations

import errno
import hashlib
import os
import queue
import stat
import subprocess
import threading
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


def _is_fifo(path: Path) -> bool:
    try:
        return stat.S_ISFIFO(path.stat().st_mode)
    except FileNotFoundError:
        return False


class LiveMJPEGStreamer:
    """
    Robust MJPEG stream writer to a FIFO.

    Key property: it never leaves a partial JPEG in the stream.
    If the viewer is slow, it drops whole frames (queue overwrite behavior).
    """

    def __init__(self, fifo_path: Path, queue_size: int = 2) -> None:
        self.fifo_path = fifo_path
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="kaivm-live-mjpg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Unblock the queue wait
        try:
            self.q.put_nowait(b"")
        except Exception:
            pass

    def push(self, jpeg: bytes) -> None:
        if self._stop.is_set():
            return

        # Drop-oldest policy to keep latency low
        try:
            self.q.put_nowait(jpeg)
            return
        except queue.Full:
            try:
                _ = self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(jpeg)
            except queue.Full:
                pass

    def _ensure_fifo_exists(self) -> bool:
        try:
            self.fifo_path.parent.mkdir(parents=True, exist_ok=True)
            if self.fifo_path.exists():
                if not _is_fifo(self.fifo_path):
                    log.warning("Live path exists but is not FIFO: %s (disable live)", self.fifo_path)
                    return False
            else:
                os.mkfifo(self.fifo_path, 0o666)
            return True
        except Exception as e:
            log.warning("Failed to create/validate FIFO %s: %s", self.fifo_path, e)
            return False

    def _open_writer(self) -> Optional[int]:
        """
        Open FIFO for writing without blocking capture.
        - If no reader yet: ENXIO -> return None (try later)
        - Once opened, switch to blocking mode for safe full-frame writes
        """
        if not self._ensure_fifo_exists():
            return None

        try:
            fd = os.open(str(self.fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno.ENXIO, errno.ENOENT):
                return None
            log.debug("FIFO open writer error: %s", e)
            return None

        try:
            os.set_blocking(fd, True)  # crucial: block inside writer thread, not capture loop
        except Exception:
            pass

        return fd

    def _write_full(self, fd: int, data: bytes) -> bool:
        """
        Write a whole JPEG frame (blocking). Returns False if pipe broke.
        """
        mv = memoryview(data)
        off = 0
        try:
            while off < len(mv):
                n = os.write(fd, mv[off:])
                if n <= 0:
                    return False
                off += n
            return True
        except BrokenPipeError:
            return False
        except OSError as e:
            if e.errno in (errno.EPIPE, errno.EBADF):
                return False
            # Any other error: drop this frame and reopen
            return False

    def _run(self) -> None:
        fd: Optional[int] = None
        while not self._stop.is_set():
            try:
                jpeg = self.q.get(timeout=0.5)
            except queue.Empty:
                continue

            if self._stop.is_set():
                break
            if not jpeg:
                continue

            if fd is None:
                fd = self._open_writer()
                if fd is None:
                    # No viewer yet; drop silently
                    continue

            ok = self._write_full(fd, jpeg)
            if not ok:
                try:
                    os.close(fd)
                except Exception:
                    pass
                fd = None

        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


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
        ffmpeg_path: str = "ffmpeg",
        input_format: str = "mjpeg",
    ) -> None:
        self.device = device
        self.size = size
        self.input_fps = input_fps
        self.ffmpeg_path = ffmpeg_path
        self.input_format = input_format
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return

        # If the camera delivers MJPEG, copy avoids decode/encode and keeps 30fps cheap.
        # If not MJPEG, we encode to MJPEG (more CPU).
        if self.input_format.lower() == "mjpeg":
            codec_args = ["-c:v", "copy"]
        else:
            codec_args = ["-c:v", "mjpeg", "-q:v", "5"]

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
            *codec_args,
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
                    if len(buf) > 3_000_000:
                        del buf[:-2048]
                    break

                end = buf.find(EOI, start + 2)
                if end < 0:
                    if start > 0:
                        del buf[:start]
                    break

                frame = bytes(buf[start : end + 2])
                del buf[: end + 2]
                yield frame


def run_capture_loop(
    latest_path: Path,
    warmup_seconds: float = 2.0,
    restart_backoff: float = 1.0,
    out_fps: float = 8.0,
    live_path: Optional[Path] = None,
    live_fps: Optional[float] = None,  # None/0 => unlimited (camera fps)
    live_queue_size: int = 2,
    **reader_kwargs,
) -> None:
    """
    Single-owner capture loop.

    - Writes latest_path at out_fps (agent-friendly)
    - Streams MJPEG to live_path FIFO at live_fps (or unlimited) without corrupt frames.

    Warm-up: discard first warmup_seconds (time-based).
    """
    reader = FfmpegMJPEGReader(**reader_kwargs)
    streamer = LiveMJPEGStreamer(live_path, queue_size=live_queue_size) if live_path else None

    last_ok = 0.0
    next_latest = 0.0
    next_live = 0.0

    latest_period = (1.0 / out_fps) if out_fps and out_fps > 0 else 0.0
    live_period = (1.0 / live_fps) if (live_fps and live_fps > 0) else 0.0

    while True:
        if not Path(reader.device).exists():
            log.warning("Device %s not found. Waiting...", reader.device)
            # Force directory listing to refresh cache
            try:
                available = sorted([str(p) for p in Path(reader.device).parent.glob("video*")])
                log.info("Available video devices: %s", available)
                os.listdir(Path(reader.device).parent)
            except Exception:
                pass
            time.sleep(1.0)
            continue

        try:
            reader.start()
            t0 = time.time()
            next_latest = 0.0
            next_live = 0.0

            for jpeg in reader.frames():
                now = time.time()
                if (now - t0) < warmup_seconds:
                    continue

                # live feed
                if streamer is not None:
                    if live_period <= 0.0 or now >= next_live:
                        streamer.push(jpeg)
                        if live_period > 0.0:
                            if next_live <= 0.0:
                                next_live = now + live_period
                            else:
                                while next_live <= now:
                                    next_live += live_period

                # latest.jpg for agent
                if latest_period <= 0.0 or now >= next_latest:
                    _atomic_write(latest_path, jpeg)
                    last_ok = now
                    if latest_period > 0.0:
                        if next_latest <= 0.0:
                            next_latest = now + latest_period
                        else:
                            while next_latest <= now:
                                next_latest += latest_period

        except Exception as e:
            log.warning("Capture error: %s", e)

        reader.stop()
        time.sleep(restart_backoff if (time.time() - last_ok) < 5 else min(5.0, restart_backoff * 2))

