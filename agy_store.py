"""Independent, identity-bound AGY quota snapshots; never enters Codex AUTO."""
from concurrent.futures import ThreadPoolExecutor,wait
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from agy_quota import AgyError
from account_labels import valid_label,sort_key


class AgyStore:
    def __init__(self,path,registry,query,activity=None,clock=time.time,interval=15,mode='clash'):
        self.path=Path(path);self.registry=registry;self.query=query;self.activity=activity or (lambda:{})
        self.clock=clock;self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='agy-quota')
        self.rows={};self.futures=set();self.closed=False;self.revision=0
        self.interval=interval;self.mode=mode;self.generation=0;self.active={};self.next_scan=0;self.deferred={};self.route_pending=set()
        self.snapshot_observer=None
        try:
            saved=json.loads(self.path.read_text());self.rows=saved['accounts']
            if not isinstance(self.rows,dict):self.rows={}
            self.rows={k:v for k,v in self.rows.items() if valid_label(k) and isinstance(v,dict)}
        except (OSError,ValueError,KeyError):pass
        for row in self.rows.values():row['refreshing']=False
        with self.lock:self._sync()

    def _sync(self):
        try:identities=self.registry()
        except Exception:identities={}
        identities={k:v for k,v in identities.items() if valid_label(k) and isinstance(v,dict)}
        labels=set(identities)|{'A','B'}
        for label in set(self.rows)-labels:
            del self.rows[label];self.deferred.pop(label,None);self.route_pending.discard(label);self.revision+=1
        for label in sorted(labels,key=sort_key):
            identity=identities.get(label,{});old=self.rows.get(label,{})
            if label not in self.rows or old.get('identity')!=identity.get('identity'):
                self.rows[label]={'identity':identity.get('identity'),'email':identity.get('email'),
                    'snapshot':None,'fetched_at':None,'attempted_at':None,'completed_at':None,'snapshot_mode':None,
                    'error':None,'refreshing':False}
                self.revision+=1
                self.deferred.pop(label,None)
                self.route_pending.discard(label)
            else:old['email']=identity.get('email')
        return identities

    def configure(self,interval,mode):
        if interval not in (0,15,60,300,3600) or mode not in ('clash','direct'):raise ValueError('AGY 更新设置无效。')
        with self.lock:
            if self.mode!=mode:
                self.generation+=1;self.mode=mode;self.revision+=1;self.deferred.clear()
                self.route_pending.update(self.rows)
            if self.interval!=interval:self.interval=interval;self.revision+=1

    def effective_interval(self,label):
        return (15 if self.active.get(label,0)>0 else 3600) if self.interval==15 else self.interval

    def _due(self,label):
        row=self.rows[label];period=self.effective_interval(label)
        if label in self.route_pending:return 0
        if not period:return None
        if label in self.deferred:return self.deferred[label]
        if row.get('snapshot_mode')!=self.mode and not row.get('error'):return 0
        stamps=[row.get(k) for k in ('attempted_at','completed_at','fetched_at')]
        latest=max((x for x in stamps if type(x) in (int,float)),default=0)
        return latest+period

    def _save(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.agy-quota-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                os.fchmod(stream.fileno(),0o600);json.dump({'version':1,'accounts':self.rows},stream,allow_nan=False)
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    def refresh(self,target='all'):
        if target!='all' and not valid_label(target):raise ValueError('AGY 账号标签无效。')
        result={'accepted':[],'busy':[]}
        with self.lock:
            if self.closed:return result
            self._sync()
            for label in (sorted(self.rows,key=sort_key) if target=='all' else (target,)):
                row=self.rows.get(label)
                if row is None:continue
                if not row['identity']:continue
                if row['refreshing']:result['busy'].append(label);continue
                last=max((row.get(k) or 0 for k in ('attempted_at','completed_at')))
                if label not in self.route_pending and self.clock()-last<5:continue
                self.deferred.pop(label,None)
                self.route_pending.discard(label)
                row['attempted_at']=self.clock();row['refreshing']=True;self.revision+=1
                # Each task keeps its identity and route, even if controls change.
                future=self.pool.submit(self._run,label,row['identity'],self.mode,self.generation,row['attempted_at'])
                self.futures.add(future);future.add_done_callback(self._finished)
                result['accepted'].append(label)
        return result

    def _finished(self,future):
        with self.lock:self.futures.discard(future)

    def _run(self,label,identity,mode,generation,started):
        result=None;error=None
        try:result=self.query(label,mode)
        except AgyError as exc:error=str(exc)
        except Exception:error='AGY 额度暂时无法查询，请稍后重试。'
        with self.lock:
            self._sync();row=self.rows.get(label)
            if row is None or row['identity']!=identity:return
            row['refreshing']=False;row['completed_at']=self.clock()
            if generation!=self.generation:
                self.revision+=1;return
            if result and result.get('identity')!=identity:error='AGY 查询期间身份变化，请重新刷新。';result=None
            if result:
                row['snapshot']={key:copy.deepcopy(result[key]) for key in ('plan_type','groups')}
                row['fetched_at']=self.clock();row['snapshot_mode']=mode;row['error']=None
                if self.snapshot_observer:
                    try:self.snapshot_observer(label,identity,row['snapshot'],row['fetched_at'],started)
                    except Exception:pass
            else:row['error']=error
            self.revision+=1
            try:self._save()
            except Exception:
                # A cache write failure must not falsify a successful official response.
                pass

    def tick(self):
        with self.lock:
            if self.closed:return
            now=self.clock()
            if now>=self.next_scan:
                self.next_scan=now+3;self._sync()
                try:counts=self.activity()
                except Exception:counts={}
                counts={k:v for k,v in counts.items() if k in self.rows and type(v) is int and v>0}
                newly=set(counts)-set(self.active)
                if counts!=self.active:self.active=counts;self.revision+=1
            else:newly=set()
            due=[label for label,row in self.rows.items() if row['identity'] and not row['refreshing'] and
                (self.interval or label in self.route_pending) and (label in newly or (self._due(label) or 0)<=now)]
        for label in due:self.refresh(label)

    def public_state(self):
        with self.lock:
            self._sync();rows=[];now=self.clock()
            for label in sorted(self.rows,key=sort_key):
                row=self.rows[label];period=self.effective_interval(label)
                snapshot=copy.deepcopy(row.get('snapshot'));fetched=row.get('fetched_at')
                stale=not fetched or now-fetched>max(90,(period or 3600)*2) or row.get('snapshot_mode')!=self.mode
                rows.append({'label':label,'registered':bool(row['identity']),'email':row.get('email'),
                    'snapshot':snapshot,'fetched_at':fetched,'refreshing':row['refreshing'],
                    'error':row.get('error'),'stale':stale,'active':self.active.get(label,0)>0,
                    'refresh_interval':period,'next_refresh_at':self._due(label) if row['identity'] else None})
            return {'accounts':rows,'server_time':now,'revision':self.revision,'network_mode':self.mode,'interval':self.interval}

    def signal_state(self):
        with self.lock:return {'agy_revision':self.revision,'agy_busy':any(row['refreshing'] for row in self.rows.values())}

    def wait(self):
        with self.lock:pending=tuple(self.futures)
        wait(pending)

    def defer_initial_refresh(self):
        with self.lock:
            self._sync()
            try:self.active=self.activity()
            except Exception:self.active={}
            self.next_scan=self.clock()+3
            for label,row in self.rows.items():
                if row['identity'] and not row.get('attempted_at') and self.interval:
                    self.deferred[label]=self.clock()+self.effective_interval(label)

    def close(self):
        with self.lock:self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
