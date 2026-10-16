#!/usr/bin/env python3
"""Local RTSP channel counter and snapshot collage using GStreamer and Pillow."""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


HERE = Path(__file__).resolve().parent
STREAM_RE = re.compile(r"^\s+(?:video|audio) #\d+:", re.MULTILINE | re.IGNORECASE)
CHANNEL_RE = re.compile(r"(\d+)(?!.*\d)")
MAX_CHANNELS = 256


@dataclass(frozen=True)
class ScanConfig:
    url: str
    start: int
    end: int
    streams: tuple[int, ...]
    timeout: int
    workers: int


def normalize_url(url: str) -> str:
    """Percent encode credentials so spaces, Unicode, and @ work in RTSP URIs."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "rtsp" or not parts.hostname:
        raise ValueError("Geçerli bir rtsp:// adresi girin.")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Port geçersiz.") from exc
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = host + (f":{port}" if port is not None else "")
    if parts.username is not None:
        user = quote(unquote(parts.username), safe="")
        password = quote(unquote(parts.password or ""), safe="")
        authority = f"{user}:{password}@{authority}"
    return urlunsplit(("rtsp", authority, parts.path, parts.query, parts.fragment))


def prepare_config(data: dict) -> ScanConfig:
    url = normalize_url(str(data.get("url", "")))
    path = urlsplit(url).path
    if "{channel}" not in path and not CHANNEL_RE.search(path):
        raise ValueError("Adres yolunda bir kanal numarası veya {channel} olmalı.")
    if "{stream}" in path and "{channel}" not in path:
        raise ValueError("{stream} kullanırken {channel} da ekleyin.")
    try:
        start = int(data.get("start", 1))
        end = int(data.get("end", 32))
        timeout = int(data.get("timeout", 6))
        workers = int(data.get("workers", 3))
        streams = tuple(dict.fromkeys(int(x) for x in data.get("streams", [1])))
    except (TypeError, ValueError) as exc:
        raise ValueError("Aralık, süre, eşzamanlılık ve akışlar sayı olmalı.") from exc
    if not 1 <= start <= end <= MAX_CHANNELS:
        raise ValueError(f"Kanal aralığı 1–{MAX_CHANNELS} içinde olmalı.")
    if not 2 <= timeout <= 30:
        raise ValueError("Zaman aşımı 2–30 saniye olmalı.")
    if not 1 <= workers <= 6:
        raise ValueError("Eşzamanlı deneme sayısı 1–6 olmalı.")
    if not streams or any(s not in (1, 2) for s in streams):
        raise ValueError("Akış 1 (ana) veya 2 (alt) olmalı.")
    if "{channel}" in path and "{stream}" not in path and len(streams) > 1:
        raise ValueError("Özel şablonda iki akış denemek için {stream} ekleyin.")
    if "{channel}" not in path and len(CHANNEL_RE.search(path).group(1)) < 3 and 2 in streams:
        raise ValueError("Alt akış için 101 biçiminde adres veya {stream} içeren şablon kullanın.")
    return ScanConfig(url, start, end, streams, timeout, workers)


def channel_url(config: ScanConfig, channel: int, stream: int) -> str:
    parts = urlsplit(config.url)
    path = parts.path
    if "{channel}" in path:
        path = path.replace("{channel}", str(channel)).replace("{stream}", str(stream))
    else:
        match = CHANNEL_RE.search(path)
        assert match is not None
        original = match.group(1)
        # Hikvision /Streaming/Channels/101: camera 1, stream 01.
        if len(original) >= 3:
            replacement = f"{channel}{stream:02d}"
        else:
            replacement = str(channel).zfill(len(original))
        path = path[: match.start()] + replacement + path[match.end() :]
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def safe_display_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    authority = host + (f":{parts.port}" if parts.port else "")
    if parts.username is not None:
        authority = f"***@{authority}"
    return urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))


def probe(url: str, timeout: int) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["gst-discoverer-1.0", "-t", str(timeout), url],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout + 3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout", "Zaman aşımı"
    except OSError:
        return "error", "GStreamer çalıştırılamadı"
    output = result.stdout + "\n" + result.stderr
    if result.returncode == 0 and STREAM_RE.search(output):
        return "found", "Ses veya görüntü akışı bulundu"
    lower = output.lower()
    if "401" in lower or "unauthorized" in lower or "authentication" in lower:
        return "auth", "Kimlik doğrulama başarısız"
    if "404" in lower or "not found" in lower:
        return "missing", "Kanal bulunamadı"
    if "timeout" in lower or "timed out" in lower:
        return "timeout", "Zaman aşımı"
    if "could not connect" in lower or "connection refused" in lower or "no route" in lower:
        return "connection", "Cihaza bağlanılamadı"
    return "error", "Akış doğrulanamadı"


def capture_frame(url: str, output: Path, timeout: int) -> tuple[bool, str]:
    """Use the same GStreamer RTSP transport as discovery to save one video frame."""
    try:
        result = subprocess.run(
            [
                "gst-launch-1.0", "-q", "uridecodebin", f"uri={url}",
                "!", "videoconvert", "!", "jpegenc", "snapshot=true",
                "!", "filesink", f"location={output}",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout + 8,
            check=False,
        )
        if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            from PIL import Image

            try:
                with Image.open(output) as image:
                    image.verify()
                return True, "Görüntü alındı"
            except (OSError, ValueError):
                return False, "Oluşan JPEG geçersiz"
        message = (result.stdout + "\n" + result.stderr).lower()
        if "401" in message or "unauthorized" in message:
            return False, "Yetki hatası"
        if "not-negotiated" in message or "no decoder" in message or "missing plugin" in message:
            return False, "Video çözücü veya GStreamer eklentisi eksik"
        if "could not link" in message or "delayed linking failed" in message:
            return False, "Video akışı bağlanamadı"
        if "timed out" in message or "timeout" in message:
            return False, "Kare beklerken zaman aşımı"
        return False, "Video karesi alınamadı"
    except subprocess.TimeoutExpired:
        return False, "Kare beklerken zaman aşımı"
    except OSError:
        return False, "gst-launch-1.0 çalıştırılamadı"


def make_collage(frames: list[tuple[int, Path | None]]) -> bytes:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    columns = min(4, max(1, math.ceil(math.sqrt(len(frames)))))
    rows = math.ceil(len(frames) / columns)
    tile_w, tile_h, gap, heading = 360, 250, 14, 54
    canvas = Image.new("RGB", (columns * tile_w + (columns + 1) * gap,
                               heading + rows * tile_h + (rows + 1) * gap), "#101722")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
        small = ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
        small = font
    draw.text((gap, 16), "RTSP Kanal Kolajı", fill="#e8eef8", font=font)
    for index, (channel, path) in enumerate(frames):
        x = gap + (index % columns) * (tile_w + gap)
        y = heading + gap + (index // columns) * (tile_h + gap)
        draw.rounded_rectangle((x, y, x + tile_w, y + tile_h), radius=9, fill="#192333")
        if path:
            try:
                with Image.open(path) as source:
                    frame = ImageOps.contain(source.convert("RGB"), (tile_w, 202))
                    canvas.paste(frame, (x + (tile_w - frame.width) // 2, y + (202 - frame.height) // 2))
            except (OSError, ValueError):
                path = None
        if not path:
            draw.text((x + 18, y + 92), "Görüntü alınamadı", fill="#a9b9cb", font=small)
        draw.text((x + 14, y + 211), f"Kanal {channel}", fill="#e8eef8", font=font)
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


class ScanManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.collage: bytes | None = None
        self.state: dict = {"running": False, "results": [], "completed": 0, "total": 0,
                            "collage_running": False, "collage_ready": False,
                            "collage_completed": 0, "collage_total": 0,
                            "collage_captured": 0, "collage_failures": []}

    def snapshot(self) -> dict:
        with self.lock:
            state = dict(self.state)
            state["results"] = list(self.state["results"])
        state["found_channels"] = sorted({r["channel"] for r in state["results"] if r["status"] == "found"})
        state["found_count"] = len(state["found_channels"])
        return state

    def start(self, config: ScanConfig) -> None:
        with self.lock:
            if self.state["running"] or self.state["collage_running"]:
                raise ValueError("Önceki tarama veya kolaj hazırlığı sürüyor.")
            self.cancel = threading.Event()
            self.collage = None
            self.state = {
                "running": True,
                "results": [],
                "completed": 0,
                "total": (config.end - config.start + 1) * len(config.streams),
                "started_at": time.time(),
                "error": None,
                "collage_running": False,
                "collage_ready": False,
                "collage_completed": 0,
                "collage_total": 0,
                "collage_captured": 0,
                "collage_error": None,
                "collage_failures": [],
            }
        threading.Thread(target=self._run, args=(config, self.cancel), daemon=True).start()

    def stop(self) -> None:
        self.cancel.set()

    def _run(self, config: ScanConfig, cancel: threading.Event) -> None:
        try:
            with ThreadPoolExecutor(max_workers=config.workers) as pool:
                futures = {
                    pool.submit(probe, channel_url(config, c, s), config.timeout): (c, s)
                    for c in range(config.start, config.end + 1)
                    for s in config.streams
                }
                for future in as_completed(futures):
                    if cancel.is_set():
                        for pending in futures:
                            pending.cancel()
                        break
                    channel, stream = futures[future]
                    try:
                        status, message = future.result()
                    except Exception:
                        status, message = "error", "Beklenmeyen deneme hatası"
                    row = {
                        "channel": channel,
                        "stream": stream,
                        "url": safe_display_url(channel_url(config, channel, stream)),
                        "status": status,
                        "message": message,
                    }
                    with self.lock:
                        self.state["results"].append(row)
                        self.state["completed"] += 1
        except Exception:
            with self.lock:
                self.state["error"] = "Tarama başlatılamadı. GStreamer kurulumunu kontrol edin."
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["cancelled"] = cancel.is_set()
                found = sorted({r["channel"] for r in self.state["results"] if r["status"] == "found"})
                preferred = {channel: 2 for channel in found}
                for row in self.state["results"]:
                    if row["status"] == "found" and row["stream"] == 1:
                        preferred[row["channel"]] = 1
                if found and not cancel.is_set():
                    self.state["collage_running"] = True
                    self.state["collage_total"] = len(found)
            if found and not cancel.is_set():
                self._make_collage(config, preferred, cancel)

    def _make_collage(self, config: ScanConfig, preferred: dict[int, int], cancel: threading.Event) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="rtsp-kanal-") as directory:
                paths = {channel: Path(directory) / f"kanal-{channel}.jpg" for channel in preferred}
                with ThreadPoolExecutor(max_workers=min(config.workers, 4)) as pool:
                    futures = {
                        pool.submit(capture_frame, channel_url(config, channel, stream), paths[channel], config.timeout): channel
                        for channel, stream in preferred.items()
                    }
                    captured = set()
                    failures = []
                    for future in as_completed(futures):
                        if cancel.is_set():
                            for pending in futures:
                                pending.cancel()
                            break
                        channel = futures[future]
                        try:
                            success, reason = future.result()
                            if success:
                                captured.add(channel)
                            else:
                                failures.append({"channel": channel, "reason": reason})
                        except Exception:
                            failures.append({"channel": channel, "reason": "Beklenmeyen yakalama hatası"})
                        with self.lock:
                            self.state["collage_completed"] += 1
                            self.state["collage_captured"] = len(captured)
                            self.state["collage_failures"] = sorted(failures, key=lambda item: item["channel"])
                if not cancel.is_set():
                    if captured:
                        frames = [(channel, paths[channel] if channel in captured else None) for channel in sorted(preferred)]
                        collage = make_collage(frames)
                        with self.lock:
                            self.collage = collage
                            self.state["collage_ready"] = True
                    else:
                        with self.lock:
                            self.state["collage_error"] = "Hiçbir kanaldan video karesi alınamadı. Ayrıntılara bakın."
        except Exception:
            with self.lock:
                self.state["collage_error"] = "Kolaj oluşturulamadı. GStreamer ve Pillow kurulumunu kontrol edin."
        finally:
            with self.lock:
                self.state["collage_running"] = False
                self.state["cancelled"] = cancel.is_set()


MANAGER = ScanManager()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.path == "/":
            content = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/status":
            self._json(200, MANAGER.snapshot())
        elif self.path in ("/api/collage", "/api/collage-preview"):
            with MANAGER.lock:
                content = MANAGER.collage
            if content is None:
                return self._json(404, {"error": "Kolaj henüz hazır değil"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            disposition = "attachment" if self.path == "/api/collage" else "inline"
            self.send_header("Content-Disposition", f'{disposition}; filename="rtsp-kanal-kolaji.jpg"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
        else:
            self._json(404, {"error": "Bulunamadı"})

    def do_POST(self) -> None:
        if self.path not in ("/api/start", "/api/stop"):
            return self._json(404, {"error": "Bulunamadı"})
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            return self._json(403, {"error": "İstek reddedildi"})
        if self.path == "/api/stop":
            MANAGER.stop()
            return self._json(200, {"ok": True})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 4096:
                raise ValueError("İstek boyutu geçersiz.")
            data = json.loads(self.rfile.read(length))
            config = prepare_config(data)
            MANAGER.start(config)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, {"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="Yerel RTSP kanal sayacı")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not shutil.which("gst-discoverer-1.0"):
        parser.error("gst-discoverer-1.0 bulunamadı")
    if not shutil.which("gst-launch-1.0"):
        parser.error("gst-launch-1.0 bulunamadı")
    try:
        import PIL  # noqa: F401
    except ImportError:
        parser.error("Pillow bulunamadı (pip install Pillow)")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"RTSP kanal sayacı: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
