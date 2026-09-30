"""Picture analysis of a keyframe: aspect and black bars, colour, tone and light layout, and the
button-started background job that caches results in the workspace."""

import json
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

import frame_analysis as fa
import server
import storage_manager


def solid(bgr, size=(720, 1280)):
    image = np.zeros((*size, 3), np.uint8)
    image[:] = bgr
    return image


class FrameAnalysisTests(unittest.TestCase):
    def test_letterboxed_film_frame_is_measured_without_its_black_bars(self):
        image = solid((0, 0, 0), (1080, 1920))
        image[140:940] = (60, 120, 200)  # 1920×800 picture = 2.4:1
        cv2.putText(image, 'SUBTITLE IN THE BAR', (560, 1010), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 5)
        result = fa.analyze(image)
        self.assertTrue(result['frame']['letterbox'])
        self.assertEqual(result['frame']['aspect'], '2.39:1')
        self.assertAlmostEqual(result['frame']['bars']['top'], 140, delta=4)
        self.assertEqual(result['frame']['bars']['left'], 0)
        # The bars do not count as dark tones or as a colour of the picture.
        self.assertEqual(result['color']['palette'][0]['name'], '橙')
        self.assertLess(result['tone']['shadows'], 0.01)
        self.assertIn('宽银幕 2.39:1', result['tags'])

    def test_a_dark_band_on_one_side_only_is_not_a_letterbox(self):
        image = solid((90, 90, 90))
        image[:200] = 0  # dark sky, lit ground
        self.assertFalse(fa.analyze(image)['frame']['letterbox'])
        self.assertFalse(fa.analyze(solid((0, 0, 0)))['frame']['letterbox'])  # an all-black frame

    def test_aspect_names(self):
        self.assertEqual(fa.aspect_name(1920 / 1080), '16:9')
        self.assertEqual(fa.aspect_name(1080 / 1920), '9:16')
        self.assertEqual(fa.aspect_name(1.85), '1.85:1')
        self.assertEqual(fa.aspect_name(3.0), '3.00:1')
        self.assertEqual(fa.orientation(9 / 16), '竖屏')
        self.assertEqual(fa.analyze(solid((128, 128, 128), (1280, 720)))['tags'][0], '竖屏')

    def test_warm_cool_and_neutral(self):
        self.assertEqual(fa.analyze(solid((40, 140, 230)))['color']['temperature']['label'], '暖调')
        self.assertEqual(fa.analyze(solid((200, 120, 40)))['color']['temperature']['label'], '冷调')
        gray = fa.analyze(solid((128, 128, 128)))
        self.assertEqual(gray['color']['temperature']['label'], '中性')
        self.assertEqual(gray['color']['saturation']['label'], '接近黑白')
        # Green foliage is neither warm nor cool.
        self.assertEqual(fa.analyze(solid((60, 170, 60)))['color']['temperature']['label'], '中性')

    def test_palette_shares_and_teal_orange(self):
        image = solid((140, 120, 20))
        image[:, 640:] = (60, 140, 230)
        result = fa.analyze(image)
        palette = result['color']['palette']
        self.assertEqual(len(palette), 2)
        self.assertAlmostEqual(palette[0]['share'] + palette[1]['share'], 1.0, places=2)
        self.assertEqual({c['name'] for c in palette}, {'青', '橙'})
        self.assertEqual(result['color']['harmony']['label'], '青橙对比')
        self.assertEqual(result, fa.analyze(image), 'the same picture always gives the same result')

    def test_tone_key_and_contrast(self):
        dark = solid((20, 20, 20))
        dark[300:420, 560:720] = (120, 120, 120)
        self.assertEqual(fa.analyze(dark)['tone']['key'], '低调')
        bright = fa.analyze(solid((235, 235, 235)))['tone']
        self.assertEqual((bright['key'], bright['contrast']), ('高调', '低对比'))
        halves = solid((5, 5, 5))
        halves[:, 640:] = (250, 250, 250)
        self.assertEqual(fa.analyze(halves)['tone']['contrast'], '高对比')
        tone = fa.analyze(halves)['tone']
        self.assertEqual(len(tone['histogram']), 32)
        self.assertAlmostEqual(sum(tone['histogram']), 1.0, places=2)

    def test_light_layout(self):
        ramp = np.repeat(np.repeat(np.linspace(240, 30, 1280).astype(np.uint8)[None, :, None], 720, 0), 3, 2)
        self.assertEqual(fa.analyze(ramp)['light']['label'], '左亮右暗')
        spot = solid((30, 30, 30))
        cv2.circle(spot, (640, 360), 200, (230, 230, 230), -1)
        self.assertEqual(fa.analyze(spot)['light']['label'], '中心亮、四周暗')
        self.assertEqual(fa.analyze(solid((128, 128, 128)))['light']['label'], '明暗均匀')

    def test_results_are_plain_json(self):
        json.dumps(fa.analyze(solid((10, 200, 90))), allow_nan=False)
        with self.assertRaises(ValueError):
            fa.analyze(None)


class AnalysisJobTests(unittest.TestCase):
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
        for i, colour in enumerate([(230, 140, 40), (40, 120, 200), (128, 128, 128)]):
            path = self.root / 's1' / 'frames' / 'r1' / f'{i}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(solid(colour, (90, 160))).save(path, quality=95)
        (self.root / 's1' / 'manifest.json').write_text('{}', encoding='utf-8')
        cuts = [{'kind': 'cut', 'frame_index': i * 10, 'label': ''} for i in range(4)]  # the 4th image is missing
        server._sessions['s1'] = {
            'results': {'r1': {'run': 'r1', 'cuts': cuts}},
            'latest_run': 'r1',
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
        deadline = time.monotonic() + 10
        while server._analysis_jobs['s1']['status'] == 'running':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_nothing_is_computed_until_asked(self):
        state = server.analysis('s1', 'r1')
        self.assertEqual((state['status'], state['ready'], state['total']), ('idle', 0, 4))
        self.assertEqual(state['items'], [None] * 4)
        self.assertFalse((self.root / 's1' / 'analysis.json').exists())

    def test_started_job_fills_and_saves_the_cache(self):
        started = server.start_analysis('s1', {'run': 'r1'})
        self.assertNotIn('items', started)
        self.wait()
        self.assertEqual(server._storage.active.get('s1', 0), 0, 'the record is released afterwards')
        state = server.analysis('s1', 'r1')
        self.assertEqual(state['ready'], 3)
        self.assertEqual(state['job']['failed'], 1)
        self.assertEqual(state['items'][0]['color']['temperature']['label'], '暖调')  # saved as RGB: orange
        self.assertIsNone(state['items'][3])
        saved = json.loads((self.root / 's1' / 'analysis.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['version'], fa.ANALYSIS_VERSION)
        self.assertEqual(set(saved['items']), {'r1/0', 'r1/1', 'r1/2'})
        # A restart reads the file back; a newer algorithm version starts over.
        server._analysis_items.clear()
        self.assertEqual(server.analysis('s1', 'r1', items=False)['ready'], 3)
        server._analysis_items.clear()
        saved['version'] = fa.ANALYSIS_VERSION - 1
        (self.root / 's1' / 'analysis.json').write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(server.analysis('s1', 'r1', items=False)['ready'], 0)

    def test_only_new_pictures_are_computed_again(self):
        server.start_analysis('s1', {'run': 'r1'})
        self.wait()
        path = self.root / 's1' / 'frames' / 'r2' / '0.jpg'
        path.parent.mkdir(parents=True)
        Image.fromarray(solid((20, 20, 20), (90, 160))).save(path)
        cuts = server._sessions['s1']['results']['r1']['cuts'][:3]
        adjusted = [dict(c, asset_run='r1', file_index=i) for i, c in enumerate(cuts)]
        adjusted[1] = {'kind': 'adjusted', 'frame_index': 11, 'label': '', 'asset_run': 'r2', 'file_index': 0}
        server._sessions['s1']['results'] = {'r3': {'run': 'r3', 'cuts': adjusted}}
        self.assertEqual(server.analysis('s1', 'r3', items=False)['ready'], 2)
        job = server.start_analysis('s1', {'run': 'r3'})['job']
        self.assertEqual(job['total'], 1)
        self.wait()
        self.assertEqual(server.analysis('s1', 'r3', items=False)['status'], 'done')
        # Pictures no longer used by any result are dropped from the file.
        saved = json.loads((self.root / 's1' / 'analysis.json').read_text(encoding='utf-8'))
        self.assertEqual(set(saved['items']), {'r1/0', 'r2/0', 'r1/2'})

    def test_unknown_result(self):
        with self.assertRaises(HTTPException):
            server.analysis('s1', 'old')
        with self.assertRaises(HTTPException):
            server.start_analysis('s1', {'run': 'old'})

    def test_storage_counts_the_cache_and_can_clear_it(self):
        server.start_analysis('s1', {'run': 'r1'})
        self.wait()
        size = (self.root / 's1' / 'analysis.json').stat().st_size
        self.assertTrue(storage_manager.ANALYSIS_FILE == server.ANALYSIS_FILE)
        with patch.object(storage_manager.store, 'load', return_value={**server._sessions['s1'], 'video_name': 'a'}):
            info = server._storage.session_info('s1')
            self.assertEqual(info['cache_bytes'], size)
            self.assertEqual(info['unknown_bytes'], 0)
            plan = server._storage.preview({'kind': 'cache', 'session_id': 's1'})
        self.assertEqual([item['label'] for item in plan['items']], ['画面分析结果'])


if __name__ == '__main__':
    unittest.main()
