import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from starlette.datastructures import UploadFile
import server
import storage_manager


class ImmediateThread:
    def __init__(self,target,args,**kwargs):self.target,self.args=target,args
    def start(self):self.target(*self.args)


class SingleResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='single-result-')
        self.root=Path(self.tmp.name)
        self.wp=patch.object(server,'WORK',self.root);self.wp.start()
        self.mp=patch.object(server,'_storage',storage_manager.Manager(server));self.mp.start()
        server._sessions.clear();server._jobs.clear()
        source=self.root/'fixture.avi'
        writer=cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*'FFV1'),30,(80,60))
        self.assertTrue(writer.isOpened())
        for i in range(270):
            frame=np.full((60,80,3),20 if (i//30)%2==0 else 210,np.uint8)
            frame[:6,:6]=i%256;writer.write(frame)
        writer.release()
        self.sid=asyncio.run(server.upload(UploadFile(filename='fixture.avi',file=io.BytesIO(source.read_bytes()))))['session_id']
        self.analyze()

    def tearDown(self):
        server._sessions.clear();server._jobs.clear();self.mp.stop();self.wp.stop();self.tmp.cleanup()

    def analyze(self):
        with patch.object(server.threading,'Thread',ImmediateThread):
            server.analyze({'session_id':self.sid,'include_ends':False})
        self.assertEqual(server._jobs[self.sid]['status'],'done')

    def edit(self,target,index=0):
        result=server._jobs[self.sid]['result']
        return server.edit(self.sid,dict(base_run=result['run'],action='move',frame_index=target,index=index))['result']

    def test_one_edit_does_not_copy_unchanged_images(self):
        before=server._jobs[self.sid]['result']
        before_paths=before['frames'][1:]
        with patch.object(server.shutil,'copyfile',side_effect=AssertionError('copied unchanged image')):
            after=self.edit(31)
        self.assertEqual(after['frames'][1:],before_paths)
        self.assertEqual(len(server._sessions[self.sid]['results']),1)

    def test_one_hundred_edits_keep_one_set_and_constant_file_count(self):
        result=server._jobs[self.sid]['result']
        server.edit(self.sid,dict(base_run=result['run'],action='add',frame_index=15))
        self.assertEqual(len(server._jobs[self.sid]['result']['cuts']),9)
        for i in range(100):self.edit(16 if i%2==0 else 15,index=0)
        for folder in ('frames','thumbs'):
            self.assertEqual(len(list((self.root/self.sid/folder).rglob('*.jpg'))),9)
        self.assertEqual(len(server._sessions[self.sid]['results']),1)

    def test_reanalysis_replaces_current_set_and_restart_preserves_it(self):
        self.edit(31);self.analyze()
        server._sessions.clear();server._jobs.clear()
        state=server.status(self.sid)
        self.assertEqual(len(server._sessions[self.sid]['results']),1)
        self.assertEqual(len(list((self.root/self.sid/'frames').rglob('*.jpg'))),len(state['result']['cuts']))

    def test_active_download_defers_old_image_removal(self):
        before=server._jobs[self.sid]['result']
        url=before['frames'][0].split('/')
        response=server.frame(self.sid,url[-2],int(url[-1]))
        original=Path(response.path)
        self.edit(31)
        self.assertTrue(original.exists())
        response._release_lease()
        self.assertFalse(original.exists())

    def test_manifest_failure_preserves_old_pixels_and_result(self):
        before=server._jobs[self.sid]['result']
        images={str(p):p.read_bytes() for p in (self.root/self.sid/'frames').rglob('*.jpg')}
        with patch.object(server,'_persist_session',side_effect=OSError('full')):
            with self.assertRaises(Exception):self.edit(31)
        self.assertIs(server._jobs[self.sid]['result'],before)
        self.assertEqual(images,{str(p):p.read_bytes() for p in (self.root/self.sid/'frames').rglob('*.jpg')})

    def test_restart_recovers_committed_result_and_collects_retired_files(self):
        before=server._jobs[self.sid]['result'];url=before['frames'][0].split('/')
        response=server.frame(self.sid,url[-2],int(url[-1]));old=Path(response.path)
        self.edit(31);self.assertTrue(old.exists())
        server._sessions.clear();server._jobs.clear();server._storage.active.clear()
        state=server.status(self.sid)
        self.assertEqual(state['result']['cuts'][0]['frame_index'],31)
        self.assertFalse(old.exists())

    def test_restart_removes_only_files_from_uncommitted_write_journal(self):
        import workspace_store
        current=server._jobs[self.sid]['result']
        workspace_store.begin_result(self.root,self.sid,'unfinished')
        for kind in ('frames','thumbs'):
            folder=self.root/self.sid/kind/'unfinished';folder.mkdir()
            (folder/'0.jpg').write_bytes(b'incomplete')
            (folder/'user-note.txt').write_text('keep')
        server._sessions.clear();server._jobs.clear()
        state=server.status(self.sid)
        self.assertEqual(state['result']['run'],current['run'])
        self.assertFalse((self.root/self.sid/'frames'/'unfinished'/'0.jpg').exists())
        self.assertTrue((self.root/self.sid/'frames'/'unfinished'/'user-note.txt').exists())

    def test_storage_inventory_and_export_follow_unchanged_asset_references(self):
        import zipfile
        self.edit(31)
        result=server._jobs[self.sid]['result']
        info=server._storage.session_info(self.sid)
        actual=sum(p.stat().st_size for kind in ('frames','thumbs') for p in (self.root/self.sid/kind).rglob('*.jpg'))
        self.assertEqual(len(info['versions']),1)
        self.assertEqual(info['versions'][0]['bytes'],actual)
        self.assertEqual(info['versions'][0]['count'],len(result['cuts']))
        archive,_=server._export_archive(self.sid)
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(len(z.namelist()),len(result['cuts'])+1)
            image=cv2.imdecode(np.frombuffer(z.read(z.namelist()[0]),np.uint8),cv2.IMREAD_COLOR)
            self.assertAlmostEqual(float(image[:4,:4].mean()),31,delta=1)

    def test_previous_revision_cannot_be_exported_as_if_it_were_current(self):
        from fastapi import HTTPException
        old=server._jobs[self.sid]['result']['run'];self.edit(31)
        with self.assertRaises(HTTPException) as error:server._export_archive(self.sid,run=old)
        self.assertEqual(error.exception.status_code,409)

    def test_empty_current_result_is_a_valid_storage_record(self):
        with patch.object(server,'_select_entries',return_value=([],9.0)):
            self.analyze()
        info=server._storage.session_info(self.sid)
        self.assertEqual(info['versions'][0]['count'],0)
        self.assertEqual(info['versions'][0]['bytes'],0)

    def test_current_asset_url_and_single_download_name_match_after_edit(self):
        updated=self.edit(31)
        for position in (0,1):
            url=updated['frames'][position].split('/')
            response=server.frame_dl(self.sid,url[-2],int(url[-1]))
            expected=updated['cuts'][position]
            self.assertIn(expected['label'].replace(':','-'),response.headers['content-disposition'])
            self.assertIn(f'{position+1:03d}_',response.headers['content-disposition'])
            image=cv2.imread(str(response.path))
            self.assertAlmostEqual(float(image[:4,:4].mean()),expected['frame_index'],delta=1)
            response._release_lease()
