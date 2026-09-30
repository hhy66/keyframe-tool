import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi import HTTPException
import server
import storage_manager

class CollageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.p=patch.object(server,'WORK',self.root); self.p.start()
        self.m=storage_manager.Manager(server)
        self.q=patch.object(server,'_storage',self.m); self.q.start()
        self.result={'run':'abcd','video_name':'测试.mp4','cuts':[{'label':'00:00:01','frame_index':i} for i in range(4)]}
        for i in range(4):
            p=self.root/'sample'/'frames'/'abcd'/f'{i}.jpg'; p.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(32,18),(i*60,20,255-i*50)).save(p)
        server._sessions['sample']={'latest_run':'abcd','results':{'abcd':self.result},'worker_lock':threading.Lock()}
        self.body={'run':'abcd','ids':[3,1,0,2],'grid':2,'mode':'original'}
    def tearDown(self):
        server._sessions.clear();server._jobs.clear();self.q.stop();self.p.stop();self.tmp.cleanup()
    def test_original_exact_order_and_temp_only(self):
        before=set((self.root/'sample'/'frames').rglob('*'))
        out=server.render_collage('sample',self.body)
        self.assertEqual((out['width'],out['height']),(64,36))
        plan=out['plan'];self.assertEqual((plan['gap'],plan['caption_height']),(0,0))
        with Image.open(self.root/'sample'/'exports'/f"collage-{out['token']}.png") as im:
            for n,i in enumerate(self.body['ids']):
                with Image.open(self.root/'sample'/'frames'/'abcd'/f'{i}.jpg') as src:
                    self.assertEqual(im.crop(((n%2)*32,(n//2)*18,(n%2+1)*32,(n//2+1)*18)).tobytes(),src.convert('RGB').tobytes())
        self.assertEqual(before,set((self.root/'sample'/'frames').rglob('*')))
    def test_invalid_and_stale(self):
        for delta in ({'run':'stale'},{'ids':[0,0]},{'ids':['../x']},{'width':float('nan')},{'grid':4}):
            with self.subTest(delta=delta),self.assertRaises(HTTPException):server.plan_collage('sample',{**self.body,**delta})
    def test_scaled_crop_jpeg_and_source_without_caption(self):
        body={**self.body,'mode':'custom','width':101,'shape':'square','fit':'cover','index':True,'time':True,'format':'jpeg','crops':{'3':{'x':0.1,'y':0.1,'width':0.8,'height':0.8}}}
        out=server.render_collage('sample',body)
        self.assertEqual(out['format'],'jpeg');self.assertEqual(out['width'],101)
        self.assertGreater(out['plan']['caption_height'],0)
        with Image.open(self.root/'sample'/'exports'/f"collage-{out['token']}.jpg") as im:self.assertEqual(im.format,'JPEG')
        p=server.plan_collage('sample',{**body,'shape':'source'})
        self.assertEqual(p['caption_height'],0)
        self.assertEqual(p['items'][0]['cell']['x']+p['items'][0]['cell']['width'],p['items'][1]['cell']['x'])
    def test_large_plan_blocked_missing_and_bad_crop(self):
        p=server.plan_collage('sample',{**self.body,'mode':'custom','width':12000})
        self.assertFalse(p['can_render'])
        with self.assertRaises(HTTPException):server.render_collage('sample',{**self.body,'mode':'custom','width':12000})
        for crop in ({'x':0,'y':0,'width':2,'height':1},{'x':float('nan'),'y':0,'width':1,'height':1}):
            with self.assertRaises(HTTPException):server.plan_collage('sample',{**self.body,'crops':{'3':crop}})
        (self.root/'sample'/'frames'/'abcd'/'0.jpg').unlink()
        with self.assertRaises(HTTPException):server.plan_collage('sample',self.body)
    def test_lease_delete_and_repeat_preview(self):
        out=server.render_collage('sample',self.body);token=out['token']
        response=server.collage_file('sample',token,'preview')
        self.assertTrue(self.m.busy('sample'))
        with self.assertRaises(HTTPException):server.delete_collage('sample',token)
        response._release_lease()
        full=server.collage_file('sample',token,'download');self.assertIn('filename*=',full.headers['content-disposition']);full._release_lease()
        server.delete_collage('sample',token)
        self.assertFalse(any((self.root/'sample'/'exports').iterdir()))
        with self.assertRaises(HTTPException):server.collage_file('sample','../x','image')
    def test_asset_ref_and_dimension_mismatch(self):
        self.result['cuts'][0]['asset_run']='old'
        p=self.root/'sample'/'frames'/'old'/'0.jpg';p.parent.mkdir();Image.new('RGB',(32,18),'red').save(p)
        plan=server.plan_collage('sample',self.body);self.assertIn('/old/0',plan['items'][2]['src'])
        Image.new('RGB',(18,32),'red').save(p)
        with self.assertRaises(HTTPException):server.plan_collage('sample',self.body)
        self.result['cuts'][0]['asset_run']='../outside'
        with self.assertRaises(HTTPException):server.plan_collage('sample',self.body)
    def test_expiry_and_strict_temp_names(self):
        import os,time
        from collage_engine import managed_file
        out=server.render_collage('sample',self.body)
        paths=list((self.root/'sample'/'exports').iterdir())
        unknown=self.root/'sample'/'exports'/'my-photo.png';unknown.write_bytes(b'keep')
        self.assertTrue(all(managed_file(p.name) for p in paths));self.assertFalse(managed_file(unknown.name))
        self.m.prepared.clear()
        for p in paths:os.utime(p,(time.time()-7200,time.time()-7200))
        server._expire_collages('sample')
        self.assertEqual(list((self.root/'sample'/'exports').iterdir()),[unknown])
    def test_nine_fullhd_original_plan(self):
        import collage_engine
        p=self.root/'hd.jpg';Image.new('RGB',(1920,1080)).save(p)
        sources=[dict(id=i,path=p,src=f'/frame/{i}',thumb=f'/thumb/{i}') for i in range(9)]
        plan=collage_engine.plan({'grid':3,'mode':'original','shape':'square','index':True},sources)
        self.assertEqual((plan['width'],plan['height']),(5760,3240))
        self.assertEqual(plan['caption_height'],0);self.assertTrue(plan['can_render'])
    def test_contain_preserves_edges_and_original_ignores_crop(self):
        out=server.render_collage('sample',{**self.body,'mode':'custom','width':128,'shape':'square','fit':'contain'})
        item=out['plan']['items'][0]
        self.assertEqual(item['crop'],dict(x=0,y=0,width=32,height=18))
        self.assertEqual(item['target']['height'],36)
        p=server.plan_collage('sample',{**self.body,'fit':'cover','crops':{'3':{'x':0.1,'y':0.1,'width':0.5,'height':0.5}}})
        self.assertEqual(p['items'][0]['crop'],dict(x=0,y=0,width=32,height=18))
    def test_oversized_prepared_batch_is_bounded(self):
        import time
        for n in range(8):self.m.prepared[('sample',f'collage-{n}.json')]=time.monotonic()+60
        with self.assertRaises(HTTPException) as caught:server.render_collage('sample',self.body)
        self.assertEqual(caught.exception.status_code,429)
