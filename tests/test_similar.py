"""Hints for near-duplicate screenshots: each result compared with the one before it."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from PIL import Image

import server
import storage_manager


class SimilarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, 'WORK', self.root),
            patch.object(server, '_storage', storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        server._similar_cache.clear()
        rng = np.random.default_rng(3)
        base = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
        noisy = np.clip(base.astype(int) + rng.integers(-4, 5, base.shape), 0, 255).astype(np.uint8)
        other = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
        images = [base, noisy, other, other]
        for i, image in enumerate(images):
            folder = 'thumbs' if i != 3 else 'frames'  # the last one has no thumbnail: falls back to the full frame
            path = self.root / 's1' / folder / 'r1' / f'{i}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(image).save(path, quality=95)
        cuts = [{'kind': 'cut', 'frame_index': i * 10, 'label': ''} for i in range(5)]
        server._sessions['s1'] = {
            'results': {'r1': {'run': 'r1', 'cuts': cuts}},
            'latest_run': 'r1',
            'worker_lock': threading.Lock(),
        }

    def tearDown(self):
        server._sessions.clear()
        server._similar_cache.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_scores_compare_each_shot_with_the_previous_one(self):
        out = server.similar('s1', 'r1')
        scores = out['scores']
        self.assertIsNone(scores[0])
        self.assertGreaterEqual(scores[1], out['threshold'])  # same picture with a little noise
        self.assertLess(scores[2], out['threshold'])  # a different picture
        self.assertGreaterEqual(scores[3], 95)  # identical, read from the full frame
        self.assertIsNone(scores[4])  # missing image gives no score
        self.assertIs(server.similar('s1', 'r1'), out)  # cached per result

    def test_unknown_result(self):
        with self.assertRaises(HTTPException):
            server.similar('s1', 'old')


if __name__ == '__main__':
    unittest.main()
