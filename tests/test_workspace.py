import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from starlette.datastructures import UploadFile
from fastapi import HTTPException
import server

class ImmediateThread:
    def __init__(self, target, args, **kwargs): self.target, self.args = target, args
    def start(self): self.target(*self.args)

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wp = patch.object(server, 'WORK', self.root); self.wp.start()
        server._sessions.clear(); server._jobs.clear()
    def tearDown(self):
        server._sessions.clear(); server._jobs.clear(); self.wp.stop(); self.tmp.cleanup()
    def uploaded(self):
        video = self.root / 'input.avi'
        out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'FFV1'), 30, (32, 24))
        for n in range(12): out.write(np.full((24,32,3), n*20, np.uint8))
        out.release()
        r = asyncio.run(server.upload(UploadFile(filename='原视频.avi', file=io.BytesIO(video.read_bytes()))))
        return r['session_id']
    def analyzed(self):
        sid = self.uploaded()
        with patch.object(server.threading, 'Thread', ImmediateThread):
            server.analyze({'session_id':sid, 'include_ends':True})
        self.assertEqual(server.status(sid)['status'], 'done')
        return sid
    def test_restart_restores_cache_edits_selection_and_export(self):
        sid = self.analyzed(); run = server.status(sid)['result']['run']
        server.edit(sid, {'base_run':run, 'action':'add', 'frame_index':5})
        run = server.status(sid)['result']['run']
        server.save_selection(sid, {'run':run, 'excluded':[5]})
        server._sessions.clear(); server._jobs.clear()
        state = server.status(sid)
        self.assertEqual(state['excluded'], [5])
        self.assertIn(5, server._sessions[sid]['manual_additions'])
        self.assertEqual(server.preview(sid, frame_index=5)['frame_index'], 5)
        archive,_ = server._export_archive(sid)
        with zipfile.ZipFile(archive) as z: self.assertIn('cuts.txt', z.namelist())
    def test_old_workspace_has_unknown_times_and_original_file_indices(self):
        folder = self.root/'legacy'/'frames'/'oldrun'; folder.mkdir(parents=True)
        for i in (0,2): cv2.imwrite(str(folder/f'{i}.jpg'), np.zeros((8,8,3), np.uint8))
        state = server.restore_workspace('legacy')
        self.assertTrue(state['result']['meta']['gallery_only'])
        self.assertEqual([c['file_index'] for c in state['result']['cuts']], [0,2])
        self.assertIsNone(state['result']['cuts'][0]['frame_index'])
        archive,_ = server._export_archive('legacy')
        with zipfile.ZipFile(archive) as z: self.assertIn('unknown', z.read('cuts.txt').decode())
    def test_corrupt_manifest_is_reported_not_silently_legacy(self):
        folder=self.root/'bad'; folder.mkdir(); (folder/'manifest.json').write_text('{')
        entry=server.workspace()['entries'][0]
        self.assertFalse(entry['can_restore'])
        with self.assertRaises(HTTPException): server.restore_workspace('bad')
    def test_manifest_failure_preserves_old_result(self):
        sid=self.analyzed(); before=server.status(sid)['result']
        with patch.object(server, '_persist_session', side_effect=OSError('disk full')):
            with self.assertRaises(HTTPException): server.edit(sid, {'base_run':before['run'], 'action':'add', 'frame_index':5})
        self.assertEqual(server.status(sid)['result'], before)
    def test_selection_rejects_stale_run(self):
        sid=self.analyzed()
        with self.assertRaises(HTTPException): server.save_selection(sid, {'run':'old', 'excluded':[]})
    def test_traversal_rejected(self):
        with self.assertRaises(HTTPException): server.restore_workspace('../outside')
    def test_missing_image_is_marked_and_export_refuses_partial(self):
        sid=self.analyzed(); run=server.status(sid)['result']['run']
        (self.root/sid/'frames'/run/'0.jpg').unlink()
        server._sessions.clear(); server._jobs.clear()
        state=server.status(sid)
        self.assertFalse(state['result']['cuts'][0]['available'])
        with self.assertRaises(HTTPException): server._export_archive(sid)
    def test_manifest_cannot_reference_outside_video(self):
        sid=self.analyzed(); path=self.root/sid/'manifest.json'
        data=json.loads(path.read_text(encoding='utf-8')); data['video_file']='../input.avi'
        path.write_text(json.dumps(data),encoding='utf-8')
        server._sessions.clear(); server._jobs.clear()
        with self.assertRaises(HTTPException): server.status(sid)
    def test_uploaded_workspace_can_restore_before_analysis(self):
        sid=self.uploaded()
        self.assertEqual(server.restore_workspace(sid)['status'], 'idle')
    def test_precise_result_without_cache_still_has_times_but_cannot_edit(self):
        sid=self.analyzed(); path=self.root/sid/'manifest.json'
        data=json.loads(path.read_text(encoding='utf-8')); data['cache_file']=None
        path.write_text(json.dumps(data),encoding='utf-8')
        server._sessions.clear(); server._jobs.clear()
        state=server.status(sid)
        self.assertFalse(state['result']['meta']['can_edit'])
        self.assertFalse(state['result']['meta'].get('gallery_only',False))
    def test_legacy_selection_survives_restart(self):
        folder=self.root/'legacy'/'frames'/'oldrun'; folder.mkdir(parents=True)
        cv2.imwrite(str(folder/'2.jpg'),np.zeros((8,8,3),np.uint8))
        server.restore_workspace('legacy')
        server.save_selection('legacy',{'run':'oldrun','excluded':['legacy:oldrun:2']})
        server._sessions.clear(); server._jobs.clear()
        self.assertEqual(server.status('legacy')['excluded'], ['legacy:oldrun:2'])

    def test_missing_scan_cache_keeps_saved_screenshots_accessible(self):
        sid=self.analyzed(); path=self.root/sid/'manifest.json'
        data=json.loads(path.read_text(encoding='utf-8'))
        (self.root/sid/data['cache_file']).unlink()
        server._sessions.clear(); server._jobs.clear()
        state=server.status(sid)
        self.assertFalse(state['result']['meta']['can_edit'])
        archive,_=server._export_archive(sid)
        self.assertTrue(archive.is_file())

    def test_reanalysis_keeps_excluded_frames_on_disk(self):
        sid=self.analyzed(); run=server.status(sid)['result']['run']
        server.save_selection(sid,{'run':run,'excluded':[0]})
        with patch.object(server.threading,'Thread',ImmediateThread):
            server.analyze({'session_id':sid,'include_ends':True})
        server._sessions.clear(); server._jobs.clear()
        self.assertEqual(server.status(sid)['excluded'],[0])
