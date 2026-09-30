"""The collage draft (order, crops, notes) is kept with the workspace, not only in one browser."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import server
import storage_manager
import workspace_store


class CollageDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, 'WORK', self.root),
            patch.object(server, '_storage', storage_manager.Manager(server)),
        ]
        for p in self.patches:
            p.start()
        result = {
            'run': 'abcd',
            'video_name': 'v.mp4',
            'params': {},
            'meta': {},
            'cuts': [{'kind': 'cut', 'frame_index': 45, 'source_frame': 30, 'label': '00:00:01.500', 'time': 1.5}],
        }
        server._sessions['s1'] = {
            'video_path': None,
            'video_name': 'v.mp4',
            'meta': {},
            'cache': {},
            'results': {'abcd': result},
            'latest_run': 'abcd',
            'worker_lock': threading.Lock(),
        }
        self.draft = {
            'ids': [30, 'legacy:old:2'],
            'crops': {'30': {'x': 0.1, 'y': 0, 'width': 0.5, 'height': 1}},
            'notes': {'30': ' 航拍开场 ', 'legacy:old:2': '', '12': '已不在选择里的备注'},
        }

    def tearDown(self):
        server._sessions.clear()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_round_trip_and_reload_from_disk(self):
        self.assertEqual(
            server.load_collage_draft('s1'), {'draft': {'ids': [], 'crops': {}, 'notes': {}}, 'saved': False}
        )
        server.save_collage_draft('s1', {'draft': self.draft})
        saved = server.load_collage_draft('s1')
        self.assertTrue(saved['saved'])
        self.assertEqual(saved['draft']['ids'], ['30', 'legacy:old:2'])
        self.assertEqual(saved['draft']['notes'], {'30': '航拍开场', '12': '已不在选择里的备注'})
        self.assertEqual(saved['draft']['crops']['30']['width'], 0.5)
        reloaded = workspace_store.load(self.root, 's1', load_cache=False)
        self.assertEqual(reloaded['collage'], saved['draft'])

    def test_invalid_drafts_are_rejected_without_losing_the_saved_one(self):
        server.save_collage_draft('s1', {'draft': self.draft})
        for bad in (
            None,
            [],
            {'ids': [30, 30]},
            {'ids': [True]},
            {'ids': [-1]},
            {'notes': {'30': 'x' * 61}},
            {'notes': {'30': 'a\nb'}},
            {'crops': {'30': {'x': 0, 'y': 0, 'width': 2, 'height': 1}}},
            {'ids': list(range(2001))},
        ):
            with self.subTest(bad=bad), self.assertRaises(HTTPException):
                server.save_collage_draft('s1', {'draft': bad})
        self.assertEqual(server.load_collage_draft('s1')['draft']['ids'], ['30', 'legacy:old:2'])

    def test_a_corrupt_stored_draft_starts_fresh(self):
        server._sessions['s1']['collage'] = {'ids': 'broken'}
        self.assertEqual(server.load_collage_draft('s1')['draft'], {'ids': [], 'crops': {}, 'notes': {}})

    def test_publishing_a_new_result_keeps_the_draft(self):
        server.save_collage_draft('s1', {'draft': self.draft})
        ses = server._sessions['s1']
        new = {**ses['results']['abcd'], 'run': 'efgh'}
        staged = workspace_store.current_update(ses, new)
        server._persist_session('s1', staged, new)
        self.assertEqual(workspace_store.load(self.root, 's1', load_cache=False)['collage']['notes']['30'], '航拍开场')


if __name__ == '__main__':
    unittest.main()
