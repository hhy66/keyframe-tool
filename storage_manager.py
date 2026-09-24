"""Conservative workspace cleanup: preview tokens, quarantined transactions and recovery."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import HTTPException
from starlette.responses import FileResponse
import workspace_store as store

MANAGEMENT = '.storage'
def now(): return datetime.now(timezone.utc).isoformat()
def fail(message, code=409): raise HTTPException(code, message)

def tree(path):
    """Inventory without following any reparse point, including unknown files."""
    path = Path(path)
    if path.is_symlink() or getattr(path, 'is_junction', lambda:False)(): fail('目录包含链接，禁止清理', 400)
    if not path.exists(): return []
    if path.is_file():
        st = path.stat()
        return [(str(path), st.st_size, st.st_mtime_ns)]
    result = []
    for child in sorted(path.iterdir()): result.extend(tree(child))
    return result

def size(path): return sum(x[1] for x in tree(path))
def fingerprint(path):
    entries = [(str(Path(p).relative_to(path)) if Path(p) != path else '.', n, t) for p,n,t in tree(path)]
    manifest = path / 'manifest.json'
    raw = manifest.read_bytes() if manifest.is_file() else b''
    return hashlib.sha256(json.dumps(entries).encode() + raw).hexdigest()

class LeasedResponse(FileResponse):
    async def __call__(self, scope, receive, send):
        try: return await super().__call__(scope, receive, send)
        finally: self._release_lease()

class Manager:
    def __init__(self, server):
        self.s = server
        self.tokens = {}
        self.active = {}
        self.prepared = {}
        self.recovered = set()
    @property
    def root(self): return self.s.WORK
    def directory(self, sid):
        if sid == MANAGEMENT: fail('管理目录不是工作记录',400)
        return store.safe_path(self.root, sid)
    def management(self):
        p = store.safe_path(self.root, MANAGEMENT)
        p.mkdir(exist_ok=True)
        return p
    def trashdir(self, tid): return store.safe_path(self.management(), tid)
    def busy(self, sid):
        ses = self.s._sessions.get(sid, {})
        return bool(self.active.get(sid,0) or ses.get('worker_lock') and ses['worker_lock'].locked() or
                    self.s._jobs.get(sid,{}).get('status') == 'running' or
                    any(k[0] == sid and expiry > time.monotonic() for k,expiry in self.prepared.items()))
    def require_idle(self, sid):
        if self.busy(sid): fail('记录正在分析、编辑、打包或下载，请稍后重试')
    def lease(self, sid, response):
        self.active[sid] = self.active.get(sid,0) + 1
        released = False
        def release():
            nonlocal released
            with self.s._lock:
                if not released:
                    self.active[sid] = max(0,self.active.get(sid,0)-1)
                    released = True
        response.__class__ = LeasedResponse
        response._release_lease = release
        return response
    def invalidate(self, sid):
        self.s._sessions.pop(sid,None); self.s._jobs.pop(sid,None)
    def audit(self, event, **data):
        with (self.management()/'audit.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps(dict(at=now(),event=event,**data),ensure_ascii=False)+'\n')
            out.flush(); os.fsync(out.fileno())
    def writej(self, td, j): store.atomic_json(td/'journal.json',j)
    def readj(self, td):
        store.safe_path(self.management(),td.name,'journal.json')
        j = json.loads((td/'journal.json').read_text(encoding='utf-8'))
        self.directory(j['sid'])
        if j.get('kind') not in ('session','versions','cache','video','temporary'): fail('回收记录类型无效')
        for rel in j['paths']:
            if rel != '.': store.safe_path(self.directory(j['sid']),*rel.split('/'))
        return j
    def recover(self):
        key = str(self.root.resolve())
        if key in self.recovered: return
        self.recovered.add(key)
        if not (self.root/MANAGEMENT).exists(): return
        for td in self.management().iterdir():
            if not td.is_dir(): continue
            try:
                j = self.readj(td)
                if j['state'] in ('moving','restoring'):
                    # Roll back incomplete move/restore; final manifest was not committed.
                    self._rollback(td,j)
            except Exception:
                # Keep damaged journals visible; never infer that they are safe to purge/restore.
                continue
    def _setmanifest(self, sid, data):
        d = self.directory(sid)
        if data is None:
            (d/'manifest.json').unlink(missing_ok=True)
        else: store.atomic_json(d/'manifest.json',data)
    def _rollback(self, td, j):
        d = self.directory(j['sid'])
        restoring = j['state'] == 'restoring'
        for rel in reversed(j['paths']):
            original = d if rel == '.' else store.safe_path(d,*rel.split('/'))
            held = td/'data' if rel == '.' else store.safe_path(td,'data',*rel.split('/'))
            source,target = (original,held) if restoring else (held,original)
            if not source.exists() and not target.exists(): fail('中断恢复文件缺失，请保留记录并人工检查')
            if source.exists():
                if target.exists(): fail('中断恢复存在冲突，请保留文件并人工检查')
                target.parent.mkdir(parents=True,exist_ok=True); os.replace(source,target)
        if j['kind'] != 'session': self._setmanifest(j['sid'],j['after'] if restoring else j['before'])
        j['state'] = 'ready' if restoring else 'rolled_back'
        if restoring:
            j['after_fingerprint'] = fingerprint(d) if d.exists() else None
            j['data_fingerprint'] = fingerprint(td/'data')
        self.writej(td,j); self.invalidate(j['sid'])
    def session_info(self, sid):
        d = self.directory(sid)
        total = size(d)
        ses = store.load(self.root,sid,load_cache=False)
        versions=[]; known=set()
        for run,res in ses['results'].items():
            paths=[store.safe_path(d,'frames',run),store.safe_path(d,'thumbs',run)]
            files=[f for p in paths for f in tree(p)]
            known.update(p for p,_,_ in files)
            versions.append(dict(run=run,bytes=sum(n for _,n,_ in files),count=sum(1 for name,_,_ in tree(paths[0]) if Path(name).suffix.lower()=='.jpg'),
                current=run==ses.get('latest_run'),pinned=bool(res.get('pinned')),manual=res.get('action')=='manual' or any(c.get('kind') in ('manual','adjusted') for c in res['cuts']),
                created_at=res.get('created_at') or datetime.fromtimestamp(max((p.stat().st_mtime for p in paths if p.exists()),default=d.stat().st_mtime),timezone.utc).isoformat(),
                thumbnail_url=(res.get('thumbs') or [''])[0]))
        video=ses.get('video_path'); cache=ses.get('cache_file')
        vp=tree(video) if video else []; cp=tree(store.safe_path(d,cache)) if cache else []
        tmp=[x for x in tree(store.safe_path(d,'exports')) if Path(x[0]).suffix=='.zip']
        known.update(p for p,_,_ in vp+cp+tmp)
        known.add(str(d/'manifest.json'))
        unknown=sum(n for p,n,_ in tree(d) if p not in known)
        result=ses['results'].get(ses.get('latest_run'),{})
        return dict(session_id=sid,video_name=ses['video_name'],bytes=total,video_bytes=sum(n for _,n,_ in vp),cache_bytes=sum(n for _,n,_ in cp),temp_bytes=sum(n for _,n,_ in tmp),unknown_bytes=unknown,busy=self.busy(sid),updated_at=ses.get('updated_at'),thumbnail_url=(result.get('thumbs') or [''])[0],note=ses.get('workspace_note','')+' 旧版本无生成时间时显示目录修改时间。',versions=versions)
    def inventory(self):
        self.recover(); sessions=[]; trash=[]
        for d in self.root.iterdir():
            if d.name == MANAGEMENT or not d.is_dir(): continue
            try: sessions.append(self.session_info(d.name))
            except Exception as exc:
                sessions.append(dict(session_id=d.name,video_name=d.name,bytes=0,video_bytes=0,cache_bytes=0,temp_bytes=0,unknown_bytes=0,busy=True,updated_at='',thumbnail_url='',note='无法安全读取：'+str(exc),versions=[]))
        if (self.root/MANAGEMENT).exists():
            for td in self.management().iterdir():
                if not td.is_dir(): continue
                try:
                    j=self.readj(td)
                    if j['state'] in ('restored','purged','rolled_back'): continue
                    trash.append(dict(id=td.name,name=j['name'],bytes=size(td/'data'),created_at=j['created_at'],kind=j['kind'],session_id=j['sid'],restorable=j['state']=='ready' and j.get('data_fingerprint')==fingerprint(td/'data'),status=j['state'],note='已移入回收区，尚未释放空间' if j['state']=='ready' else '操作未完整完成，请检查或重试永久删除'))
                except Exception:
                    trash.append(dict(id=td.name,name=td.name,bytes=0,created_at='',kind='unknown',session_id='',restorable=False,status='damaged',note='回收记录损坏，保留文件，需人工检查'))
        return dict(path=str(self.root),total_bytes=size(self.root),trash_bytes=sum(x['bytes'] for x in trash),free_bytes=shutil.disk_usage(self.root).free,sessions=sessions,trash=trash)
    def manifest(self,ses):
        return dict(schema=1,video_file=Path(ses['video_path']).name if ses.get('video_path') else None,video_name=ses['video_name'],meta=ses['meta'],updated_at=now(),latest_run=ses.get('latest_run'),results=ses['results'],cache_file=ses.get('cache_file'),manual_additions=sorted(ses.get('manual_additions',[])),manual_adjustments={str(k):v for k,v in ses.get('manual_adjustments',{}).items()},excluded=ses.get('excluded',[]))
    def preview(self,payload):
        self.recover(); kind=payload.get('kind'); token=uuid.uuid4().hex
        if kind=='purge':
            td=self.trashdir(payload.get('trash_id')); j=self.readj(td)
            if j['state'] not in ('ready','partial'): fail('回收记录当前不能删除')
            files=tree(td/'data'); snapshot=fingerprint(td)
            plan=dict(trash_id=td.name,kind=kind,snapshot=snapshot)
            sid=j['sid']; name=j['name']; consequences=['永久删除回收区文件，不能撤销。']; retained=['工作区现有记录不受影响。']; phrase='永久删除'
            items=[dict(label='回收区所选文件',bytes=sum(n for _,n,_ in files),count=len(files))]
        else:
            sid=payload.get('session_id'); d=self.directory(sid); self.require_idle(sid)
            ses=store.load(self.root,sid,load_cache=False); info=self.session_info(sid); paths=[]
            if kind=='versions':
                runs=payload.get('runs')
                if not isinstance(runs,list) or not runs or len(set(runs))!=len(runs): fail('请选择历史版本',400)
                for run in runs:
                    res=ses['results'].get(run)
                    if not res: fail('版本不存在')
                    if run==ses.get('latest_run') or res.get('pinned'): fail('当前或标记保留的版本不能清理')
                    paths.extend([f'frames/{run}',f'thumbs/{run}'])
            elif kind=='temporary': paths=[str(Path(p).relative_to(d)).replace('\\','/') for p,_,_ in tree(store.safe_path(d,'exports')) if Path(p).suffix=='.zip']
            elif kind=='video': paths=[Path(ses['video_path']).name] if ses.get('video_path') else []
            elif kind=='cache': paths=[ses['cache_file']] if ses.get('cache_file') else []
            elif kind=='session': paths=['.']
            else: fail('未知清理类型',400)
            paths=[r for r in paths if (d if r=='.' else store.safe_path(d,*r.split('/'))).exists()]
            if not paths: fail('没有可清理文件')
            items=[]; files=[]
            for rel in paths:
                rows=tree(d if rel=='.' else store.safe_path(d,*rel.split('/'))); files.extend(rows)
                label = ('历史版本 '+rel.split('/')[-1]+' 的原分辨率截图') if rel.startswith('frames/') else ('历史版本 '+rel.split('/')[-1]+' 的预览图') if rel.startswith('thumbs/') else '临时下载压缩包 '+Path(rel).name if rel.startswith('exports/') else {'session':'整条工作记录（含全部版本及未知文件）','video':'工作区中的视频副本','cache':'扫描缓存'}.get(kind,rel)
                items.append(dict(label=label,bytes=sum(n for _,n,_ in rows),count=len(rows)))
            consequences={'versions':['所选历史版本从工作区移除，当前结果保留。移入回收区可尝试恢复；永久删除后不能恢复。'],'temporary':['临时ZIP可重新打包，不影响其他目录的下载文件。'],'video':['不能继续分析、补帧或微调；已有截图仍可下载。原始视频不受影响。'],'cache':['后续分析和逐帧编辑需要重新扫描；已有截图保留。'],'session':['整条工作记录及全部版本将移除。',f'包括未知文件 {info["unknown_bytes"]} 字节。']}[kind]
            retained=['工作区外原始视频、已下载文件均保留。'] + ([] if kind=='session' else ['未选择的文件和当前截图保留。'])
            phrase='删除整条记录' if kind=='session' else ''
            plan=dict(session_id=sid,kind=kind,runs=payload.get('runs',[]),paths=paths,snapshot=fingerprint(d))
            name=ses['video_name']
        response=dict(token=token,session_id=sid,name=name,kind=kind,items=items,bytes=sum(n for _,n,_ in files),file_count=len(files),consequences=consequences,retained=retained,require_phrase=phrase)
        self.tokens[token]=dict(plan=plan,response=response,expires=time.monotonic()+300)
        return response
    def execute(self,payload):
        token=self.tokens.pop(payload.get('token'),None)
        if not token or token['expires']<time.monotonic(): fail('确认已过期或已使用，请重新预览')
        p=token['plan']; response=token['response']; mode=payload.get('mode')
        if mode not in ('trash','permanent'): fail('删除方式无效',400)
        if payload.get('confirmation','')!=response['require_phrase']: fail('确认文字不匹配',400)
        if p['kind']=='purge':
            if mode!='permanent': fail('回收区仅支持永久删除',400)
            td=self.trashdir(p['trash_id'])
            if fingerprint(td)!=p['snapshot']: fail('文件已变化，请重新预览')
            return self.purge(td,self.readj(td))
        sid=p['session_id']; self.require_idle(sid); d=self.directory(sid)
        if fingerprint(d)!=p['snapshot']: fail('文件或选择已变化，请重新预览')
        ses=store.load(self.root,sid,load_cache=False)
        before=json.loads((d/'manifest.json').read_text(encoding='utf-8')) if (d/'manifest.json').exists() else None
        after=self.manifest(copy.deepcopy(ses))
        if p['kind']=='versions':
            for run in p['runs']:
                if run==ses.get('latest_run') or ses['results'][run].get('pinned'): fail('版本已被保护')
                after['results'].pop(run)
        if p['kind']=='cache': after['cache_file']=None
        if p['kind']=='video': after['video_file']=None
        tid=uuid.uuid4().hex; td=self.trashdir(tid); td.mkdir()
        j=dict(sid=sid,name=response['name'],kind=p['kind'],paths=p['paths'],before=before,after=after,created_at=now(),state='moving')
        self.writej(td,j)
        try:
            for rel in p['paths']:
                src=d if rel=='.' else store.safe_path(d,*rel.split('/'))
                dest=td/'data' if rel=='.' else store.safe_path(td,'data',*rel.split('/'))
                dest.parent.mkdir(parents=True,exist_ok=True); os.replace(src,dest)
            if p['kind']!='session': self._setmanifest(sid,after)
            j['after_fingerprint']=fingerprint(d) if d.exists() else None
            j['data_fingerprint']=fingerprint(td/'data')
            j['state']='ready'; self.writej(td,j); self.invalidate(sid)
        except Exception as exc:
            self._rollback(td,j)
            fail('清理失败，已尝试恢复原记录：'+str(exc),500)
        self.audit('trash',sid=sid,trash_id=tid,kind=p['kind'])
        if mode=='permanent': return self.purge(td,j)
        return dict(status='done',message='已移入回收区，可恢复；尚未释放磁盘空间',processed_bytes=response['bytes'],released_bytes=0,trash_id=tid,failures=[])
    def purge(self,td,j):
        files=tree(td/'data'); released=0; failures=[]
        j['state']='partial'; self.writej(td,j) # Irreversible phase is durable before first unlink.
        for name,n,_ in files:
            try: Path(name).unlink(); released+=n
            except OSError as exc: failures.append(f'{Path(name).relative_to(td)}: {exc}')
        if not failures:
            try:
                if (td/'data').exists(): shutil.rmtree(td/'data')
                j['state']='purged'
            except OSError as exc: failures.append(str(exc))
        self.writej(td,j); self.audit('purge',sid=j['sid'],trash_id=td.name,released_bytes=released,failures=failures)
        return dict(status='partial' if failures else 'done',message='部分文件未删除，可重试' if failures else '永久删除完成',processed_bytes=released,released_bytes=released,trash_id=td.name,failures=failures)
    def restore(self,tid):
        self.recover(); td=self.trashdir(tid); j=self.readj(td); sid=j['sid']; d=self.directory(sid)
        self.require_idle(sid)
        if j['state']!='ready': fail('此回收记录不完整，不能恢复')
        if j.get('data_fingerprint')!=fingerprint(td/'data'): fail('回收文件已变化或缺失，禁止恢复')
        if (fingerprint(d) if d.exists() else None)!=j['after_fingerprint']: fail('原记录已变化，禁止覆盖；回收文件已保留')
        for rel in j['paths']:
            src=td/'data' if rel=='.' else store.safe_path(td,'data',*rel.split('/'))
            dest=d if rel=='.' else store.safe_path(d,*rel.split('/'))
            if not src.exists() or dest.exists(): fail('恢复路径缺失或冲突，禁止覆盖')
            tree(src)
        j['state']='restoring'; self.writej(td,j)
        try:
            for rel in j['paths']:
                src=td/'data' if rel=='.' else store.safe_path(td,'data',*rel.split('/'))
                dest=d if rel=='.' else store.safe_path(d,*rel.split('/'))
                dest.parent.mkdir(parents=True,exist_ok=True); os.replace(src,dest)
            if j['kind']!='session': self._setmanifest(sid,j['before'])
            j['state']='restored'; self.writej(td,j); self.invalidate(sid)
        except Exception:
            self._rollback(td,j); raise
        self.audit('restore',sid=sid,trash_id=tid)
        return dict(status='done',message='已恢复，可从本地工作区打开')
    def pin(self,payload):
        sid=payload.get('session_id'); self.require_idle(sid)
        if not isinstance(payload.get('pinned'),bool): fail('保留标记无效',400)
        ses=store.load(self.root,sid,load_cache=False); run=payload.get('run')
        if run not in ses['results']: fail('版本不存在',404)
        ses['results'][run]['pinned']=payload['pinned']
        self._setmanifest(sid,self.manifest(ses)); self.invalidate(sid)
        return {'ok':True}

def install(server):
    manager=Manager(server)
    def guarded(fn):
        from functools import wraps
        @wraps(fn)
        def call(*args,**kwargs):
            with server._lock:
                try: return fn(*args,**kwargs)
                except (ValueError,TypeError) as exc: fail(str(exc),400)
                except FileNotFoundError as exc: fail(str(exc),404)
        return call
    @server.app.get('/api/storage')
    @guarded
    def inventory(): return manager.inventory()
    @server.app.post('/api/storage/preview')
    @guarded
    def preview(payload:dict): return manager.preview(payload)
    @server.app.post('/api/storage/execute')
    @guarded
    def execute(payload:dict): return manager.execute(payload)
    @server.app.post('/api/storage/trash/{tid}/restore')
    @guarded
    def restore(tid:str): return manager.restore(tid)
    @server.app.put('/api/storage/pin')
    @guarded
    def pin(payload:dict): return manager.pin(payload)
    @server.app.get('/api/storage/version/{sid}/{run}')
    @guarded
    def version(sid:str,run:str):
        manager.directory(sid)
        result=store.load(server.WORK,sid,load_cache=False)['results'].get(run)
        if result is None: fail('该版本已被清理',404)
        return result
    return manager
