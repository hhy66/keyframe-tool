import asyncio
import io
import tempfile
import threading
import unittest
import weakref
import zipfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.responses import FileResponse

import server


class ImmediateThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keyframe-test-")
        self.root = Path(self.tmp.name)
        self.work_patch = patch.object(server, "WORK", self.root)
        self.work_patch.start()
        server._sessions.clear()
        server._jobs.clear()

    def tearDown(self):
        server._sessions.clear()
        server._jobs.clear()
        self.work_patch.stop()
        self.tmp.cleanup()

    def video(self, colors, fps=30, size=(160, 90)):
        path = self.root / f"sample-{len(list(self.root.glob('*.avi')))}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), fps, size)
        self.assertTrue(writer.isOpened(), "FFV1 test encoder unavailable")
        for n, color in enumerate(colors):
            frame = np.full((size[1], size[0], 3), color, np.uint8)
            frame[:8, :8] = n % 256
            writer.write(frame)
        writer.release()
        return path

    def register(self, path, sid="sample"):
        server._sessions[sid] = {"video_path": path, "video_name": path.name,
                                 "meta": server.probe(path), "cache": {},
                                 "worker_lock": threading.Lock()}
        return sid

    def analyze(self, sid, **params):
        with patch.object(server.threading, "Thread", ImmediateThread):
            server.analyze({"session_id": sid, **params})
        return server._jobs[sid]

    def normal(self):
        return self.register(self.video([20] * 30 + [210] * 30))

    def cut_frames(self, colors, **params):
        sid = self.register(self.video(colors))
        job = self.analyze(sid, **params)
        self.assertEqual(job["status"], "done", job.get("error"))
        return [round(c["time"] * 30) for c in job["result"]["cuts"] if c["kind"] == "cut"]

    def test_exported_pixels_and_timecodes_match_exact_frames(self):
        sid = self.normal()
        job = self.analyze(sid, include_ends=True)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual([round(c["time"] * 30) for c in job["result"]["cuts"]], [0, 30, 59])
        for i, expected in enumerate([0, 30, 59]):
            f = cv2.imread(str(self.root / sid / "frames" / job["run"] / f"{i}.jpg"))
            self.assertAlmostEqual(float(f[:6, :6].mean()), expected, delta=1)

    def test_same_luminance_color_cut(self):
        self.assertEqual(self.cut_frames([(255, 0, 0)] * 30 + [(0, 0, 97)] * 30), [30])

    def test_two_cuts_200ms_apart(self):
        self.assertEqual(self.cut_frames([20] * 30 + [210] * 6 + [20] * 24), [30, 36])

    def test_single_frame_flash_is_not_a_scene(self):
        self.assertEqual(self.cut_frames([20] * 32 + [255] + [20] * 27), [])

    def test_short_shot_between_old_sample_points(self):
        self.assertEqual(self.cut_frames([20] * 29 + [210] * 2 + [20] * 29,
                                         min_scene_seconds=0), [29, 31])

    def test_min_scene_interval_is_configurable(self):
        self.assertEqual(self.cut_frames([20] * 30 + [210] * 6 + [20] * 24,
                                         min_scene_seconds=0.4), [30])

    def test_two_frame_video_can_export_ends(self):
        sid = self.register(self.video([30, 30]))
        job = self.analyze(sid, include_ends=True)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(len(job["result"]["cuts"]), 2)

    def test_single_frame_video_has_one_endpoint(self):
        sid = self.register(self.video([30]))
        job = self.analyze(sid, include_ends=True)
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertEqual(len(job["result"]["cuts"]), 1)

    def test_invalid_upload_preserves_previous_result(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)
        with self.assertRaises(HTTPException):
            asyncio.run(server.upload(UploadFile(filename="broken.mp4", file=io.BytesIO(b"bad"))))
        self.assertIs(server._jobs.get(sid), old)
        self.assertTrue((self.root / sid / "frames" / old["run"] / "0.jpg").is_file())

    def test_valid_upload_does_not_destroy_other_tab(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)
        data = self.video([60] * 6).read_bytes()
        new = asyncio.run(server.upload(UploadFile(filename="new.avi", file=io.BytesIO(data))))
        self.assertNotEqual(new["session_id"], sid)
        self.assertIs(server._jobs.get(sid), old)

    def test_delayed_workers_do_not_run_latest_job_twice(self):
        sid = self.normal()
        queue = []
        class DeferredThread(ImmediateThread):
            def start(self):
                queue.append((self.target, self.args))
        with patch.object(server.threading, "Thread", DeferredThread):
            server.analyze({"session_id": sid, "sensitivity": 10})
            old = server._jobs[sid]
            server.analyze({"session_id": sid, "sensitivity": 90})
        with patch.object(server, "_select_entries", wraps=server._select_entries) as select:
            for target, args in queue:
                target(*args)
        self.assertEqual(select.call_count, 1)
        self.assertIsNot(server._jobs[sid], old)
        self.assertEqual(server._jobs[sid]["status"], "done")

    def test_failed_reanalysis_preserves_last_good_result(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)
        with patch.object(server, "_grab_frame_at", return_value=None):
            failed = self.analyze(sid, include_ends=True)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["result"], old["result"])
        self.assertTrue((self.root / sid / "frames" / old["run"] / "0.jpg").is_file())

    def test_image_encode_failure_is_not_reported_as_success(self):
        sid = self.normal()
        with patch.object(server.cv2, "imencode", return_value=(False, None)):
            job = self.analyze(sid, include_ends=True)
        self.assertEqual(job["status"], "error")

    def test_export_is_disk_backed_and_selected_in_time_order(self):
        sid = self.normal()
        self.analyze(sid, include_ends=True)
        response = server.export(sid, "2,0,2")
        self.assertIsInstance(response, FileResponse)
        with zipfile.ZipFile(response.path) as archive:
            self.assertEqual(archive.namelist(), ["001_00-00-00.000.jpg", "003_00-00-01.967.jpg", "cuts.txt"])
        asyncio.run(response.background())
        self.assertFalse(Path(response.path).exists())

    def test_invalid_selection_does_not_export_everything(self):
        sid = self.normal()
        self.analyze(sid, include_ends=True)
        with self.assertRaises(HTTPException):
            server.export(sid, "999,broken")

    def test_completed_scan_is_reused(self):
        sid = self.normal()
        self.analyze(sid)
        with patch.object(server, "_scan_pass1", side_effect=AssertionError("rescanned")):
            self.assertEqual(self.analyze(sid, sensitivity=90)["status"], "done")

    def test_truncated_video_does_not_publish_incomplete_results(self):
        path = self.video([20] * 30 + [210] * 30)
        data = path.read_bytes()
        path.write_bytes(data[:len(data) * 3 // 4])
        sid = self.register(path)
        self.assertEqual(server._sessions[sid]["meta"]["frames"], 60)
        job = self.analyze(sid, include_ends=True)
        self.assertEqual(job["status"], "error")
        self.assertIsNone(job["result"])

    def test_disable_flash_suppression_keeps_single_frame_shot(self):
        self.assertEqual(self.cut_frames([20] * 32 + [255] + [20] * 27,
                                         suppress_flash=False, min_scene_seconds=0), [32, 33])

    def test_cancelled_job_preserves_result(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)
        queue = []
        class DeferredThread(ImmediateThread):
            def start(self):
                queue.append((self.target, self.args))
        with patch.object(server.threading, "Thread", DeferredThread):
            server.analyze({"session_id": sid, "sensitivity": 90})
        job = server._jobs[sid]
        server.cancel(sid, {"run": job["run"]})
        for target, args in queue:
            target(*args)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["result"], old["result"])

    def test_stale_cancel_cannot_cancel_newer_job(self):
        sid = self.normal()
        with patch.object(server.threading, "Thread"):
            server.analyze({"session_id": sid})
            old_run = server._jobs[sid]["run"]
            server.analyze({"session_id": sid})
        with self.assertRaises(HTTPException):
            server.cancel(sid, {"run": old_run})
        self.assertFalse(server._jobs[sid]["cancel"])

    def test_cancel_during_decoding_releases_worker_without_partial_cache(self):
        sid = self.normal()
        entered, release = threading.Event(), threading.Event()
        threads = []
        real_thread, small_color = threading.Thread, server._small_color
        def make_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread
        def blocked_color(frame):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timeout")
            return small_color(frame)
        try:
            with patch.object(server.threading, "Thread", make_thread), patch.object(server, "_small_color", blocked_color):
                server.analyze({"session_id": sid})
                self.assertTrue(entered.wait(3))
                job = server._jobs[sid]
                server.cancel(sid, {"run": job["run"]})
                release.set()
                for thread in threads:
                    thread.join(3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(job["status"], "cancelled")
                self.assertNotIn("diffs", server._sessions[sid]["cache"])
        finally:
            release.set()
            for thread in threads:
                thread.join(3)

    def test_scan_does_not_retain_a_video_of_small_images(self):
        path = self.video([80] * 300)
        refs, maximum = [], [0]
        # Track arrays returned by resize without retaining the arrays ourselves.
        original = cv2.resize
        def track(*args, **kwargs):
            image = original(*args, **kwargs)
            refs.append(weakref.ref(image))
            maximum[0] = max(maximum[0], sum(r() is not None for r in refs))
            return image
        with patch.object(server.cv2, "resize", side_effect=track):
            server._scan_pass1(str(path), {}, {})
        self.assertLessEqual(maximum[0], 5)


    def edit(self, sid, action, frame_index, **extra):
        return server.edit(sid, {"base_run": server._jobs[sid]["result"]["run"],
                                 "action": action, "frame_index": frame_index, **extra})

    def test_manual_preview_time_mapping_and_pixels(self):
        sid = self.normal()
        self.analyze(sid, include_ends=True)
        self.assertEqual(server.preview(sid, time=1.01)["frame_index"], 30)
        self.assertEqual(server.preview(sid, frame_index=-4)["frame_index"], 0)
        self.assertEqual(server.preview(sid, time=999)["frame_index"], 59)
        response = server.preview_image(sid, 31)
        image = cv2.imdecode(np.frombuffer(response.body, np.uint8), cv2.IMREAD_COLOR)
        self.assertAlmostEqual(float(image[:6, :6].mean()), 31, delta=1)
        self.assertIsInstance(server.video(sid), FileResponse)
        for value in [float("nan"), float("inf"), "oops"]:
            with self.assertRaises(HTTPException):
                server.preview(sid, time=value)

    def test_manual_add_move_reanalysis_and_export(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)["result"]
        added = self.edit(sid, "add", 12)["result"]
        self.assertEqual([c["frame_index"] for c in added["cuts"]], [0, 12, 30, 59])
        self.assertEqual(added["cuts"][1]["kind"], "manual")
        moved = self.edit(sid, "move", 32, index=2)["result"]
        self.assertEqual(moved["cuts"][2]["kind"], "adjusted")
        self.edit(sid, "move", 33, index=2)
        self.edit(sid, "move", 13, index=1)
        result = self.analyze(sid, include_ends=True, sensitivity=90)["result"]
        self.assertEqual([c["frame_index"] for c in result["cuts"]], [0, 13, 33, 59])
        self.assertTrue((self.root / sid / "frames" / old["run"] / "0.jpg").exists())
        for i, cut in enumerate(result["cuts"]):
            image = cv2.imread(str(self.root / sid / "frames" / result["run"] / f"{i}.jpg"))
            self.assertAlmostEqual(float(image[:6, :6].mean()), cut["frame_index"], delta=1)
        response = server.export(sid)
        with zipfile.ZipFile(response.path) as archive:
            self.assertIn("adjusted\t33", archive.read("cuts.txt").decode())
        asyncio.run(response.background())

    def test_manual_conflicts_invalid_values_and_failure_preserve_result(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)["result"]
        for value in [True, 1.5, "3", -1, 60]:
            with self.assertRaises(HTTPException):
                self.edit(sid, "add", value)
        with self.assertRaises(HTTPException) as duplicate:
            self.edit(sid, "add", 30)
        self.assertEqual(duplicate.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            server.edit(sid, {"base_run": "stale", "action": "add", "frame_index": 12})
        with patch.object(server, "_write_jpeg", side_effect=OSError("disk full")):
            with self.assertRaises(HTTPException):
                self.edit(sid, "add", 12)
        self.assertIs(server._jobs[sid]["result"], old)
        self.assertFalse(server._sessions[sid].get("manual_additions"))
        self.assertEqual(len(list((self.root / sid / "frames").iterdir())), 1)

    def test_manual_analysis_race_never_publishes_stale_edit(self):
        sid = self.normal()
        old = self.analyze(sid, include_ends=True)["result"]
        original = server._write_jpeg
        started = []
        def start_analysis(*args):
            if not started:
                started.append(True)
                with patch.object(server.threading, "Thread"):
                    server.analyze({"session_id": sid})
            original(*args)
        with patch.object(server, "_write_jpeg", side_effect=start_analysis):
            with self.assertRaises(HTTPException) as conflict:
                self.edit(sid, "add", 12)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertIs(server._jobs[sid]["result"], old)
        self.assertFalse(server._sessions[sid].get("manual_additions"))

if __name__ == "__main__":
    unittest.main()
