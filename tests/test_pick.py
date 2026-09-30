"""Which frame of each detected shot is captured: first, about 0.5 s later, or the sharpest early frame."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi import HTTPException

import server
import storage_manager


class ImmediateThread:
    def __init__(self, target, args, **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="keyframe-pick-")
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, "WORK", self.root),
            patch.object(server, "_storage", storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        server._sessions.clear()
        server._jobs.clear()

    def tearDown(self):
        server._sessions.clear()
        server._jobs.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def video(self, sharp_from=50, frames=90):
        """Flat red until frame 30, then a textured shot that is blurred until sharp_from."""
        path = self.root / "pick.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 30, (160, 90))
        self.assertTrue(writer.isOpened(), "FFV1 test encoder unavailable")
        texture = np.kron(np.random.default_rng(1).integers(0, 2, (23, 40)) * 255, np.ones((4, 4)))[:90, :160]
        texture = np.repeat(texture[:, :, None], 3, axis=2).astype(np.uint8)
        blurred = cv2.GaussianBlur(texture, (0, 0), 1.0)
        red = np.zeros_like(texture)
        red[:, :, 2] = 255
        # The red -> texture change is the strongest peak; blurred -> sharp is weaker and merged away.
        for n in range(frames):
            writer.write(red if n < 30 else blurred if n < sharp_from else texture)
        writer.release()
        return path

    def run_analysis(self, sid="pick", **params):
        if sid not in server._sessions:
            path = self.video()
            server._sessions[sid] = {
                "video_path": path,
                "video_name": path.name,
                "meta": server.probe(path),
                "cache": {},
                "worker_lock": threading.Lock(),
            }
        with patch.object(server.threading, "Thread", ImmediateThread):
            server.analyze({"session_id": sid, "min_scene_seconds": 2.5, **params})
        job = server._jobs[sid]
        self.assertEqual(job["status"], "done", job.get("error"))
        return job["result"]

    def only_cut(self, result):
        cuts = [c for c in result["cuts"] if c["kind"] == "cut"]
        self.assertEqual(len(cuts), 1, cuts)
        return cuts[0]

    def test_strategies_choose_first_settled_or_sharpest_frame(self):
        for pick, expected in (("first", 30), ("settle", 45), ("sharp", 50)):
            with self.subTest(pick=pick):
                result = self.run_analysis(pick=pick)
                cut = self.only_cut(result)
                self.assertEqual((cut["frame_index"], cut["source_frame"]), (expected, 30))
                self.assertEqual(cut["label"], server._fmt_time(expected / 30))
                self.assertEqual(result["params"]["pick"], pick)
        self.assertEqual(server.status("pick")["params"]["pick"], "sharp")

    def test_saved_image_is_the_chosen_frame(self):
        result = self.run_analysis(pick="sharp")
        index = result["cuts"].index(self.only_cut(result))
        image = cv2.imread(str(server.workspace_store.asset_path(server.WORK, "pick", result, index)))
        self.assertGreater(server._sharpness(image), 1000)

    def test_pick_never_crosses_into_the_next_entry(self):
        cap = cv2.VideoCapture(str(self.video()))
        try:
            self.assertEqual(server._pick_frame(cap, 30, 36, "settle", 30)[0], 36)
            self.assertEqual(server._pick_frame(cap, 30, 40, "sharp", 30)[0], 30)
            self.assertEqual(server._pick_frame(cap, 30, 89, "first", 30)[0], 30)
        finally:
            cap.release()

    def test_invalid_strategy_is_rejected(self):
        path = self.video()
        server._sessions["pick"] = {
            "video_path": path,
            "video_name": path.name,
            "meta": server.probe(path),
            "cache": {},
            "worker_lock": threading.Lock(),
        }
        with self.assertRaises(HTTPException):
            server.analyze({"session_id": "pick", "pick": "middle"})

    def test_skip_state_and_fine_tuning_follow_the_detected_cut(self):
        result = self.run_analysis(pick="settle")
        cut = self.only_cut(result)
        index = result["cuts"].index(cut)
        # The skip list is keyed by the detected cut, so it survives a change of strategy.
        server.save_selection("pick", {"run": result["run"], "excluded": [30]})
        with self.assertRaises(HTTPException):
            server.save_selection("pick", {"run": result["run"], "excluded": [45]})
        moved = server.edit("pick", {"base_run": result["run"], "action": "move", "index": index, "frame_index": 60})
        self.assertEqual(server._sessions["pick"]["manual_adjustments"], {30: 60})
        self.assertEqual(server._sessions["pick"]["excluded"], [60])
        again = self.run_analysis(pick="sharp")
        frames = [c["frame_index"] for c in again["cuts"]]
        self.assertIn(60, frames)
        self.assertNotIn(50, frames)
        self.assertEqual(moved["from_frame"], 45)


if __name__ == "__main__":
    unittest.main()
