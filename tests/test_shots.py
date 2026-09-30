"""Stage 0: every keyframe becomes the shot it belongs to — its span, length and first / middle / last frames."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from fastapi import HTTPException
from PIL import Image

import server
import shots
import storage_manager


def write_video(path, count=120, fps=24):
    """Each frame is flat; blue and green encode the frame number in steps of 16, which survives compression."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (160, 90))
    for i in range(count):
        writer.write(np.full((90, 160, 3), ((i % 16) * 16 + 8, (i // 16) * 16 + 8, 128), np.uint8))
    writer.release()


def frame_number(path):
    blue, green, _ = cv2.imread(str(path)).reshape(-1, 3).mean(axis=0)
    return round((green - 8) / 16) * 16 + round((blue - 8) / 16)


class SpanTests(unittest.TestCase):
    result = {
        'meta': {'fps': 25, 'frames': 250},
        'cuts': [
            {'kind': 'cut', 'frame_index': 60, 'source_frame': 50},  # picked 0.4 s after the cut
            {'kind': 'manual', 'frame_index': 120},
            {'kind': 'cut', 'frame_index': 10, 'source_frame': 10},  # out of order on purpose
        ],
    }

    def test_each_shot_runs_to_the_frame_before_the_next(self):
        found = shots.spans(self.result)
        self.assertEqual((found[2]['start_frame'], found[2]['end_frame']), (10, 49))
        self.assertEqual((found[0]['start_frame'], found[0]['end_frame']), (50, 119))
        self.assertEqual((found[1]['start_frame'], found[1]['end_frame']), (120, 249))
        self.assertEqual(found[0]['frames'], 70)
        self.assertAlmostEqual(found[0]['duration'], 70 / 25)
        self.assertEqual(found[0]['strip'], [50, 84, 119])

    def test_real_timestamps_are_used_when_known(self):
        times = np.arange(250) * 0.04 + 1.0  # stream starting at 1 s
        found = shots.spans(self.result, times)
        self.assertAlmostEqual(found[0]['start'], 3.0)
        self.assertAlmostEqual(found[0]['end'], 1.0 + 119 * 0.04 + 0.04)

    def test_old_records_without_frame_numbers(self):
        self.assertEqual(shots.spans({'meta': {'fps': 25, 'frames': 10}, 'cuts': [{'frame_index': None}]}), [None])
        self.assertEqual(shots.spans({'meta': {}, 'cuts': [{'frame_index': 3}]}), [None])

    def test_single_frame_shot(self):
        found = shots.spans({'meta': {'fps': 25, 'frames': 100}, 'cuts': [{'frame_index': 99}]})
        self.assertEqual(found[0]['strip'], [99, 99, 99])


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / 'clip.avi'
        write_video(self.video)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_the_exact_frames_near_and_far(self):
        strip = self.root / 'strip'
        done = []
        failed = shots.extract(self.video, [3, 0, 40, 41, 110, 41], strip, lambda n, total: done.append((n, total)))
        self.assertEqual(failed, [])
        for frame in (0, 3, 40, 41, 110):
            self.assertEqual(frame_number(strip / f'{frame}.jpg'), frame, frame)
        self.assertEqual(done[-1], (5, 5))
        # Already there: nothing is read again.
        self.assertEqual(shots.missing(strip, [0, 3, 40]), [])
        self.assertEqual(shots.extract(self.video, [0, 3], strip), [])

    def test_frames_past_the_end_are_reported(self):
        self.assertEqual(shots.extract(self.video, [10, 500], self.root / 'strip'), [500])

    def test_prune_keeps_only_frames_in_use(self):
        strip = self.root / 'strip'
        shots.extract(self.video, [1, 2, 3], strip)
        shots.prune(strip, [2])
        self.assertEqual(sorted(p.name for p in strip.iterdir()), ['2.jpg'])


class ShotJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, 'WORK', self.root),
            patch.object(server, '_storage', storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        server._analysis_items.clear()
        server._analysis_jobs.clear()
        video = self.root / 's1' / 'video.avi'
        video.parent.mkdir(parents=True)
        write_video(video)
        for i in range(2):
            path = self.root / 's1' / 'frames' / 'r1' / f'{i}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.full((90, 160, 3), 100 + i * 50, np.uint8)).save(path)
        (self.root / 's1' / 'manifest.json').write_text('{}', encoding='utf-8')
        cuts = [{'kind': 'cut', 'frame_index': 0, 'source_frame': 0}, {'kind': 'cut', 'frame_index': 60}]
        server._sessions['s1'] = {
            'results': {'r1': {'run': 'r1', 'cuts': cuts, 'meta': {'fps': 24, 'frames': 120}}},
            'latest_run': 'r1',
            'video_path': str(video),
            'worker_lock': threading.Lock(),
        }

    def tearDown(self):
        server._sessions.clear()
        server._analysis_items.clear()
        server._analysis_jobs.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 20
        while server._analysis_jobs['s1']['status'] == 'running':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_spans_show_before_the_button_and_strips_after(self):
        state = server.analysis('s1', 'r1')
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(state['strip_missing'], 6)  # 0, 29, 59 and 60, 89, 119
        self.assertEqual(state['shots'][0]['end_frame'], 59)
        self.assertEqual(state['shots'][1]['strip'], [60, 89, 119])
        self.assertEqual(state['shots'][0]['strip_ready'], [False, False, False])
        job = server.start_analysis('s1', {'run': 'r1'})['job']
        self.assertEqual(state['motion_missing'], 2)
        self.assertEqual(state['shots'][0]['motion'], None)
        # 2 pictures to analyse, plus every frame read for the strips and the camera movement.
        self.assertGreater(job['total'], 2 + 6)
        self.wait()
        state = server.analysis('s1', 'r1')
        self.assertEqual((state['status'], state['strip_missing'], state['motion_missing']), ('done', 0, 0))
        self.assertIn('text', state['shots'][0]['motion'])  # flat test frames: no texture to track
        self.assertEqual(set(server._motion_cache('s1')), {'0-59', '60-119'})
        self.assertEqual(state['shots'][1]['strip_ready'], [True, True, True])
        self.assertEqual(frame_number(self.root / 's1' / 'strip' / '89.jpg'), 89)
        self.assertEqual(Path(server.strip_image('s1', 89).path).name, '89.jpg')
        with self.assertRaises(HTTPException):
            server.strip_image('s1', 7)

    def test_without_the_video_the_rest_still_works(self):
        Path(server._sessions['s1']['video_path']).unlink()
        state = server.analysis('s1', 'r1')
        self.assertEqual((state['strip_missing'], state['video']), (0, False))
        self.assertEqual(server.start_analysis('s1', {'run': 'r1'})['job']['total'], 2)
        self.wait()
        self.assertEqual(server.analysis('s1', 'r1')['status'], 'done')


if __name__ == '__main__':
    unittest.main()
