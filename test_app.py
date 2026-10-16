import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import ScanManager, capture_frame, channel_url, normalize_url, prepare_config, probe, safe_display_url


class ChannelCounterTests(unittest.TestCase):
    def test_hikvision_channel_and_stream_generation(self):
        config = prepare_config({
            "url": "rtsp://user:pass@camera:554/Streaming/Channels/101",
            "start": 1, "end": 12, "streams": [1, 2],
        })
        self.assertEqual(channel_url(config, 12, 2), "rtsp://user:pass@camera:554/Streaming/Channels/1202")

    def test_custom_template(self):
        config = prepare_config({
            "url": "rtsp://camera/live/{channel}/stream/{stream}",
            "start": 1, "end": 3, "streams": [2],
        })
        self.assertEqual(channel_url(config, 3, 2), "rtsp://camera/live/3/stream/2")

    def test_unicode_credentials_are_encoded_and_hidden(self):
        url = normalize_url("rtsp://kullanıcıadı:şifre@camera:554/Streaming/Channels/101")
        self.assertIn("kullan%C4%B1c%C4%B1ad%C4%B1:%C5%9Fifre@", url)
        self.assertNotIn("şifre", safe_display_url(url))
        self.assertIn("***@camera:554", safe_display_url(url))

    def test_probe_requires_a_real_stream(self):
        good = subprocess.CompletedProcess([], 0, "Properties:\n  video #0: H.264\n", "")
        bad = subprocess.CompletedProcess([], 0, "An error was encountered: 404 Not Found", "")
        with patch("app.subprocess.run", side_effect=[good, bad]):
            self.assertEqual(probe("rtsp://camera/101", 5)[0], "found")
            self.assertEqual(probe("rtsp://camera/102", 5)[0], "missing")

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            prepare_config({"url": "http://camera/101"})
        with self.assertRaises(ValueError):
            prepare_config({"url": "rtsp://camera/101", "start": 10, "end": 1})
        with self.assertRaises(ValueError):
            prepare_config({"url": "rtsp://camera/live/{channel}", "streams": [1, 2]})

    def test_count_is_unique_and_does_not_stop_at_a_gap(self):
        manager = ScanManager()
        config = prepare_config({
            "url": "rtsp://user:secret@camera/Streaming/Channels/101",
            "start": 1, "end": 3, "streams": [1, 2],
        })

        def fake_probe(url, _timeout):
            return ("found", "Akış bulundu") if url.endswith(("101", "102", "301")) else ("missing", "Yok")

        def fake_capture(_url, output, _timeout):
            Image.new("RGB", (80, 45), "blue").save(output)
            return True, "Görüntü alındı"

        with patch("app.probe", side_effect=fake_probe), patch("app.capture_frame", side_effect=fake_capture):
            manager.start(config)
            deadline = time.monotonic() + 2
            while (manager.snapshot()["running"] or manager.snapshot()["collage_running"]) and time.monotonic() < deadline:
                time.sleep(0.01)
        state = manager.snapshot()
        self.assertFalse(state["running"])
        self.assertEqual(state["found_channels"], [1, 3])
        self.assertEqual(state["found_count"], 2)
        self.assertEqual(state["completed"], 6)
        self.assertTrue(state["collage_ready"])
        self.assertEqual(state["collage_total"], 2)
        self.assertEqual(state["collage_captured"], 2)
        with Image.open(io.BytesIO(manager.collage)) as collage:
            self.assertEqual(collage.format, "JPEG")
        self.assertTrue(all("secret" not in row["url"] for row in state["results"]))

    def test_capture_frame_with_gstreamer_video(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.webm"
            frame = Path(directory) / "frame.jpg"
            subprocess.run(
                ["gst-launch-1.0", "-q", "videotestsrc", "num-buffers=10",
                 "!", "videoconvert", "!", "vp8enc", "!", "webmmux",
                 "!", "filesink", f"location={video}"],
                check=True, capture_output=True, timeout=10,
            )
            success, reason = capture_frame(video.as_uri(), frame, 4)
            self.assertTrue(success, reason)
            with Image.open(frame) as image:
                self.assertEqual(image.format, "JPEG")

    def test_empty_collage_is_reported_as_error(self):
        manager = ScanManager()
        config = prepare_config({"url": "rtsp://camera/Streaming/Channels/101", "start": 1, "end": 1})
        with patch("app.probe", return_value=("found", "Görüntü akışı bulundu")), patch(
            "app.capture_frame", return_value=(False, "Kare beklerken zaman aşımı")
        ):
            manager.start(config)
            deadline = time.monotonic() + 2
            while (manager.snapshot()["running"] or manager.snapshot()["collage_running"]) and time.monotonic() < deadline:
                time.sleep(0.01)
        state = manager.snapshot()
        self.assertFalse(state["collage_ready"])
        self.assertIsNone(manager.collage)
        self.assertEqual(state["collage_failures"], [{"channel": 1, "reason": "Kare beklerken zaman aşımı"}])


if __name__ == "__main__":
    unittest.main()
