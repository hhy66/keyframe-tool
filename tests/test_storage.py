import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
import server
import storage_manager
import workspace_store

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='keyframe-storage-test-')
        self.root=Path(self.tmp.name)
        self.patch=patch.object(server,'WORK',self.root); self.patch.start()
        server._sessions.clear(); server._jobs.clear()
        self.m=storage_manager.Manager(server)
        self.mp=patch.object(server,'_storage',self.m); self.mp.start()
        d=self.root/'demo'; d.mkdir(); (d/'video.mp4').write_bytes(b'video')
        results={}
        for run in ('old','latest'):
            for folder in ('frames','thumbs'):
                p=d/folder/run; p.mkdir(parents=True); (p/'0.jpg').write_bytes(b'1234')
            results[run]=dict(run=run,video_name='测试.mp4',params=dict(workspace_store.DEFAULT_PARAMS),meta={},cuts=[dict(frame_index=0,time=0,label='00:00:00.000',kind='cut')],frames=[],thumbs=[])
        ses=dict(video_path=d/'video.mp4',video_name='测试.mp4',meta={},results=results,latest_run='latest',cache={})
        workspace_store.save(self.root,'demo',ses)
    def tearDown(self):
        self.mp.stop(); self.patch.stop(); server._sessions.clear(); server._jobs.clear(); self.tmp.cleanup()
    def plan(self,kind='versions',**extra): return self.m.preview(dict(session_id='demo',kind=kind,runs=['old'],**extra))
    def execute(self,plan,mode='trash'): return self.m.execute(dict(token=plan['token'],mode=mode,confirmation=plan['require_phrase']))
    def test_inventory_exact_capacity(self):
        inv=self.m.inventory(); actual=sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        self.assertEqual(inv['total_bytes'],actual); self.assertEqual(inv['sessions'][0]['versions'][0]['bytes'],8)
    def test_current_and_pinned_protected(self):
        with self.assertRaises(HTTPException): self.m.preview(dict(session_id='demo',kind='versions',runs=['latest']))
        self.m.pin(dict(session_id='demo',run='old',pinned=True))
        with self.assertRaises(HTTPException): self.plan()
        self.assertTrue(workspace_store.load(self.root,'demo')['results']['old']['pinned'])
    def test_busy_worker_and_job(self):
        ses=server._session('demo'); ses['worker_lock'].acquire()
        with self.assertRaises(HTTPException): self.plan()
        ses['worker_lock'].release(); server._jobs['demo']['status']='running'
        with self.assertRaises(HTTPException): self.plan()
    def test_preview_changed_invalidates(self):
        p=self.plan(); (self.root/'demo'/'frames'/'old'/'0.jpg').write_bytes(b'changed')
        with self.assertRaises(HTTPException): self.execute(p)
    def test_token_replay_and_expiry(self):
        p=self.plan(); self.execute(p)
        with self.assertRaises(HTTPException): self.execute(p)
        p=self.plan('video'); self.m.tokens[p['token']]['expires']=0
        with self.assertRaises(HTTPException): self.execute(p)
    def test_trash_restore_and_restart(self):
        server._session('demo'); result=self.execute(self.plan())
        self.assertEqual(result['released_bytes'],0); self.assertNotIn('demo',server._sessions)
        self.assertNotIn('old',workspace_store.load(self.root,'demo')['results'])
        restarted=storage_manager.Manager(server); restarted.restore(result['trash_id'])
        self.assertIn('old',workspace_store.load(self.root,'demo')['results'])
    def test_permanent_purge(self):
        r=self.execute(self.plan()); p=self.m.preview(dict(kind='purge',trash_id=r['trash_id']))
        result=self.execute(p,'permanent'); self.assertEqual(result['released_bytes'],8)
        self.assertEqual(self.m.inventory()['trash'],[])
    def test_delete_session_and_restore(self):
        (self.root/'demo'/'notes.txt').write_bytes(b'unknown')
        p=self.plan('session'); self.assertEqual(p['require_phrase'],'删除整条记录')
        r=self.execute(p); self.assertFalse((self.root/'demo').exists())
        self.m.restore(r['trash_id']); self.assertEqual((self.root/'demo'/'notes.txt').read_bytes(),b'unknown')
    def test_restore_refuses_changed_record(self):
        r=self.execute(self.plan()); (self.root/'demo'/'notes.txt').write_text('changed')
        with self.assertRaises(HTTPException): self.m.restore(r['trash_id'])
        self.assertTrue((self.m.trashdir(r['trash_id'])/'data'/'frames'/'old'/'0.jpg').exists())
    def test_unknown_is_not_temporary(self):
        d=self.root/'demo'/'exports'; d.mkdir(); (d/'notes.txt').write_bytes(b'keep'); (d/'a.zip').write_bytes(b'zip')
        self.assertEqual(self.m.inventory()['sessions'][0]['unknown_bytes'],4)
        self.execute(self.plan('temporary'),'permanent'); self.assertTrue((d/'notes.txt').exists())
    def test_path_traversal_and_management(self):
        for sid in ('../outside','.storage','..','a/b'):
            with self.assertRaises((ValueError,HTTPException)): self.m.preview(dict(session_id=sid,kind='session'))
    def test_symlink_rejected(self):
        link=self.root/'demo'/'link'
        try: link.symlink_to(self.root/'demo'/'video.mp4')
        except OSError: self.skipTest('requires symlink privilege')
        with self.assertRaises(HTTPException): self.plan('session')
    def test_video_removal_disables_edit_but_keeps_gallery(self):
        self.execute(self.plan('video')); ses=server._session('demo')
        self.assertIsNone(ses['video_path']); self.assertTrue(ses['results']['latest']['meta']['gallery_only'])
        response=server.frame('demo','latest',0); response._release_lease()
        self.assertEqual(response.media_type,'image/jpeg')
    def test_cache_manifest_and_memory_invalidated(self):
        d=self.root/'demo'; (d/'scan.npz').write_bytes(b'cache')
        manifest=json.loads((d/'manifest.json').read_text()); manifest['cache_file']='scan.npz'; workspace_store.atomic_json(d/'manifest.json',manifest)
        server._sessions['demo']={'worker_lock':threading.Lock(),'cache':{'times':[1]}}
        self.execute(self.plan('cache'))
        self.assertNotIn('demo',server._sessions); self.assertIsNone(workspace_store.load(self.root,'demo')['cache_file'])
    def test_legacy_versions(self):
        (self.root/'demo'/'manifest.json').unlink()
        info=self.m.inventory()['sessions'][0]; old=next(x['run'] for x in info['versions'] if not x['current'])
        p=self.m.preview(dict(session_id='demo',kind='versions',runs=[old])); r=self.execute(p)
        self.m.restore(r['trash_id']); self.assertFalse((self.root/'demo'/'manifest.json').exists())
    def test_partial_delete_retry(self):
        r=self.execute(self.plan()); td=self.m.trashdir(r['trash_id']); original=Path.unlink
        def refuse(path,*a,**k):
            if path.name=='0.jpg': raise PermissionError('in use')
            return original(path,*a,**k)
        with patch.object(Path,'unlink',refuse): result=self.m.purge(td,self.m.readj(td))
        self.assertEqual(result['status'],'partial'); self.assertFalse(self.m.inventory()['trash'][0]['restorable'])
        p=self.m.preview(dict(kind='purge',trash_id=td.name)); self.assertEqual(self.execute(p,'permanent')['status'],'done')
    def test_lease_released_on_failed_send(self):
        response=server.frame('demo','old',0)
        with self.assertRaises(HTTPException): self.plan()
        async def send(_): raise ConnectionResetError('disconnect')
        async def receive(): return {'type':'http.disconnect'}
        async def run():
            with self.assertRaises(ConnectionResetError): await response({'type':'http','method':'GET','headers':[],'extensions':{},'asgi':{'spec_version':'2.4'}},receive,send)
        asyncio.run(run()); self.assertFalse(self.m.busy('demo'))
    def test_prepared_zip_expires(self):
        import time
        self.m.prepared[('demo','a.zip')]=time.monotonic()+60
        with self.assertRaises(HTTPException): self.plan()
        self.m.prepared[('demo','a.zip')]=0; self.plan()
    def test_restart_rollback_partial_move(self):
        d=self.root/'demo'; before=json.loads((d/'manifest.json').read_text()); td=self.m.trashdir('interrupted'); td.mkdir()
        j=dict(sid='demo',name='demo',kind='versions',paths=['frames/old','thumbs/old'],before=before,after=before,created_at=storage_manager.now(),state='moving')
        self.m.writej(td,j); held=td/'data'/'frames'; held.mkdir(parents=True)
        (d/'frames'/'old').rename(held/'old')
        storage_manager.Manager(server).recover()
        self.assertTrue((d/'frames'/'old'/'0.jpg').exists()); self.assertEqual(self.m.readj(td)['state'],'rolled_back')
    def test_failed_move_rolls_back(self):
        original=storage_manager.os.replace
        def fail_second(src,dst):
            if str(src).endswith('thumbs\\old') or str(src).endswith('thumbs/old'): raise PermissionError('blocked')
            return original(src,dst)
        with patch.object(storage_manager.os,'replace',fail_second):
            with self.assertRaises(HTTPException): self.execute(self.plan())
        self.assertIn('old',workspace_store.load(self.root,'demo')['results'])
        self.assertTrue((self.root/'demo'/'frames'/'old'/'0.jpg').exists())
    def test_restart_rollback_partial_restore_and_retry(self):
        r=self.execute(self.plan()); td=self.m.trashdir(r['trash_id']); j=self.m.readj(td)
        j['state']='restoring'; self.m.writej(td,j)
        (td/'data'/'frames'/'old').rename(self.root/'demo'/'frames'/'old')
        restarted=storage_manager.Manager(server); restarted.recover()
        self.assertEqual(self.m.readj(td)['state'],'ready')
        restarted.restore(td.name); self.assertIn('old',workspace_store.load(self.root,'demo')['results'])
    def test_missing_trash_file_not_restorable(self):
        r=self.execute(self.plan()); td=self.m.trashdir(r['trash_id'])
        (td/'data'/'frames'/'old'/'0.jpg').unlink()
        self.assertFalse(self.m.inventory()['trash'][0]['restorable'])
        with self.assertRaises(HTTPException): self.m.restore(td.name)
    def test_http_contract(self):
        async def scenario():
            async def request(method,path,data=None):
                body=json.dumps(data).encode() if data is not None else b''
                messages=[]
                scope=dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=method,
                           scheme='http',path=path,raw_path=path.encode(),query_string=b'',
                           root_path='',headers=[(b'content-type',b'application/json')],
                           client=('127.0.0.1',1),server=('test',80))
                async def receive():return {'type':'http.request','body':body,'more_body':False}
                async def send(message):messages.append(message)
                await server.app(scope,receive,send)
                status=next(m['status'] for m in messages if m['type']=='http.response.start')
                content=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
                self.assertEqual(status,200,content)
                return json.loads(content)
            inv=await request('GET','/api/storage')
            self.assertEqual(inv['sessions'][0]['session_id'],'demo')
            version=await request('GET','/api/storage/version/demo/old')
            self.assertEqual(version['run'],'old')
            plan=await request('POST','/api/storage/preview',dict(session_id='demo',kind='versions',runs=['old']))
            result=await request('POST','/api/storage/execute',dict(token=plan['token'],mode='trash',confirmation=''))
            self.assertEqual(result['status'],'done')
            restored=await request('POST',f"/api/storage/trash/{result['trash_id']}/restore",{})
            self.assertEqual(restored['status'],'done')
        asyncio.run(scenario())

    def test_upload_is_protected_until_probe_and_manifest_finish(self):
        import io
        from starlette.datastructures import UploadFile
        observed=[]
        def probe(path):
            sid=Path(path).parent.name; observed.append(sid)
            self.assertTrue(self.m.busy(sid))
            with self.assertRaises(HTTPException):
                self.m.preview(dict(session_id=sid,kind='session'))
            return dict(width=160,height=90,frames=1,fps=30,duration=1/30)
        with patch.object(server,'probe',side_effect=probe):
            asyncio.run(server.upload(UploadFile(filename='upload.avi',file=io.BytesIO(b'fixture'))))
        self.assertEqual(len(observed),1)
        self.assertFalse(self.m.busy(observed[0]))
