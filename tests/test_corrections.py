"""Corrections: what the user says a picture or shot really is, kept with the measurements for calibration."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from PIL import Image

import corrections
import server
import storage_manager
from test_shots import write_video


class CleanTests(unittest.TestCase):
    def test_only_known_fields_and_sensible_lengths(self):
        fix = corrections.clean(
            {
                'shot': ' 特写 ',
                'depth': '',
                'moves': ['推近', '推近', 3, ' 右摇/移'],
                'note': '  其实是逆光  ',
                'colour': '红',
                'cut': None,
            }
        )
        self.assertEqual(fix, {'shot': '特写', 'moves': ['推近', '右摇/移'], 'note': '其实是逆光'})
        self.assertEqual(len(corrections.clean({'note': 'x' * 5000})['note']), corrections.NOTE_MAX)

    def test_empty_means_remove(self):
        self.assertIsNone(corrections.clean({}))
        self.assertIsNone(corrections.clean({'shot': ' ', 'moves': [], 'note': ''}))
        with self.assertRaises(ValueError):
            corrections.clean('特写')

    def test_broken_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / corrections.FILE
            self.assertEqual(corrections.load(path), {})
            path.write_text('{not json', encoding='utf-8')
            self.assertEqual(corrections.load(path), {})
            path.write_text('{"items": {"r/0": {"fix": {}}, "r/1": 5}}', encoding='utf-8')
            self.assertEqual(corrections.load(path), {'r/0': {'fix': {}}})


class CorrectionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, 'WORK', self.root),
            patch.object(server, '_storage', storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        for cache in (server._analysis_items, server._motion_items, server._analysis_jobs, server._corrections):
            cache.clear()
        video = self.root / 's1' / 'video.avi'
        video.parent.mkdir(parents=True)
        write_video(video)
        for i in range(2):
            path = self.root / 's1' / 'frames' / 'r1' / f'{i}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.full((90, 160, 3), 100 + i * 50, np.uint8)).save(path)
        (self.root / 's1' / 'manifest.json').write_text('{}', encoding='utf-8')
        cuts = [
            {'kind': 'cut', 'frame_index': 0, 'source_frame': 0, 'time': 0.0, 'label': '00:00:00.000'},
            {'kind': 'cut', 'frame_index': 60, 'source_frame': 60, 'time': 2.5, 'label': '00:00:02.500'},
        ]
        diffs = np.zeros(120, np.float32)
        diffs[60], diffs[90] = 40.0, 12.0
        server._sessions['s1'] = {
            'video_name': 'clip.avi',
            'results': {
                'r1': {'run': 'r1', 'cuts': cuts, 'meta': {'fps': 24, 'frames': 120}, 'params': {'sensitivity': 50}}
            },
            'latest_run': 'r1',
            'video_path': str(video),
            'worker_lock': threading.Lock(),
            'cache': {'diffs': diffs},
        }

    def tearDown(self):
        server._sessions.clear()
        for cache in (server._analysis_items, server._motion_items, server._analysis_jobs, server._corrections):
            cache.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def analyse(self):
        server.start_analysis('s1', {'run': 'r1'})
        deadline = time.monotonic() + 20
        while server._analysis_jobs['s1']['status'] == 'running':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_saved_with_the_measurements_and_shown_on_the_cards(self):
        self.analyse()
        saved = server.save_correction('s1', {'run': 'r1', 'index': 1, 'fix': {'shot': '特写', 'moves': ['推近']}})
        self.assertEqual(saved, {'fix': {'shot': '特写', 'moves': ['推近']}, 'total': 1})
        record = json.loads((self.root / 's1' / corrections.FILE).read_text(encoding='utf-8'))['items']['r1/1']
        self.assertEqual((record['time'], record['frame_index'], record['kind']), (2.5, 60, 'cut'))
        self.assertEqual(record['shot_span']['end_frame'], 119)
        self.assertIn('shot', record['analysis'])
        self.assertIn('text', record['motion'])
        self.assertEqual(record['params'], {'sensitivity': 50})
        # How strong the picture changed where the shot starts, and inside it (for missed or extra cuts).
        self.assertEqual(record['cut_signal']['at_start'], 40.0)
        self.assertEqual(record['cut_signal']['peaks'][0], [90, 12.0])
        state = server.analysis('s1', 'r1')
        self.assertEqual(state['corrections'], [None, {'shot': '特写', 'moves': ['推近']}])
        self.assertEqual(state['corrections_total'], 1)

    def test_kept_after_a_restart_and_cleared_when_emptied(self):
        server.save_correction('s1', {'run': 'r1', 'index': 0, 'fix': {'note': '说不清，像是跟拍'}})
        server._corrections.clear()  # as after restarting the program
        self.assertEqual(server.analysis('s1', 'r1')['corrections'][0], {'note': '说不清，像是跟拍'})
        self.assertEqual(server.save_correction('s1', {'run': 'r1', 'index': 0, 'fix': {}}), {'fix': None, 'total': 0})
        self.assertEqual(server.analysis('s1', 'r1')['corrections'], [None, None])

    def test_bad_requests(self):
        for payload in (
            {'run': 'gone', 'index': 0, 'fix': {}},
            {'run': 'r1', 'index': 5, 'fix': {}},
            {'run': 'r1', 'index': True, 'fix': {}},
            {'run': 'r1', 'index': 0, 'fix': 'x'},
        ):
            with self.assertRaises(HTTPException):
                server.save_correction('s1', payload)

    def test_export_has_numbers_and_words_only(self):
        # Corrected before the analysis ran: the export fills in the measurements known by then.
        server.save_correction('s1', {'run': 'r1', 'index': 1, 'fix': {'depth': '浅景深'}})
        server.save_correction('s1', {'run': 'r1', 'index': 0, 'fix': {'cut': '漏切了'}})
        self.analyse()
        response = server.export_corrections('s1')
        self.assertIn("filename*=UTF-8''", response.headers['content-disposition'])
        data = json.loads(response.body)
        self.assertEqual(data['count'], 2)
        self.assertEqual([r['fix'] for r in data['records']], [{'cut': '漏切了'}, {'depth': '浅景深'}])
        self.assertIn('depth', data['records'][1]['analysis'])
        self.assertIn('text', data['records'][1]['motion'])
        self.assertEqual(set(data['versions']), {'analysis', 'motion'})
        self.assertNotIn('.jpg', response.body.decode('utf-8'))

    def test_not_counted_as_cache_or_clutter(self):
        server.save_correction('s1', {'run': 'r1', 'index': 0, 'fix': {'shot': '远景'}})
        server._storage.invalidate('s1')
        self.assertNotIn('s1', server._corrections)


if __name__ == '__main__':
    unittest.main()
