"""Independent read cache and durable manual redemption ledger for each account."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import threading
import time
import uuid

from account_labels import LEGACY_LABELS,valid_label,sort_key
from core import atomic_write
from reset_credits import CreditError, MESSAGES


class ResetCreditStore:
    def __init__(self,path,registry,network_mode,capture,refresh_quota,cooldown=5):
        self.path=Path(path);self.registry=registry;self.network_mode=network_mode
        self.capture=capture;self.refresh_quota=refresh_quota;self.cooldown=cooldown
        self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix='reset-credit')
        self.rows={};self.identities={};self.closed=False
        self.sequence=0
        saved={}
        try:
            saved=json.loads(self.path.read_text())
            self.rows=saved['accounts']
            self.identities=saved.get('identities',{})
        except (OSError,ValueError,KeyError,TypeError):pass
        # Migrate v1 before syncing labels. A removed/reused label must never
        # erase its previous identity's unknown redemption or request IDs.
        for row in list(self.identities.values())+list(self.rows.values()):
            row['busy']=None
            for op in row.get('redeems',{}).values():
                if op['status']=='running':op['status']='unknown'
        for row in self.rows.values():
            identity=row.get('identity')
            if identity:
                archived=self.identities.get(identity)
                if archived is None:self.identities[identity]=row
                elif archived is not row:
                    for rid,op in row.get('redeems',{}).items():
                        previous=archived.setdefault('redeems',{}).get(rid)
                        if previous is None or op.get('status')=='unknown' and not op.get('resolution'):
                            archived['redeems'][rid]=op
        with self.lock:
            self._sync()
            if saved and saved.get('version')!=2:self._save()

    @staticmethod
    def _empty(identity):
        return {'identity':identity,'snapshot':None,'fetched_at':None,
            'mode':None,'busy':None,'error':None,'revision':None,'attempted_at':None,
            'redeems':{},'last_reset':None}

    def _sync(self):
        identities=self.registry();changed=False
        labels=sorted(set(LEGACY_LABELS)|{label for label in identities if valid_label(label)},key=sort_key)
        for label in list(self.rows):
            if label not in labels:
                self.rows.pop(label);changed=True
        for label in labels:
            identity=identities.get(label)
            previous=self.rows.get(label)
            if identity:
                row=self.identities.setdefault(identity,self._empty(identity))
            else:
                row=previous if previous and previous.get('identity') is None else self._empty(None)
            if previous is not row:
                self.rows[label]=row;self.sequence+=1;changed=True
        if changed:self._save()

    def signal_state(self):
        with self.lock:
            return {'credit_revision':self.sequence,'credit_busy':any(row['busy'] for row in self.rows.values())}

    def _save(self):
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic_write(self.path,{'version':2,'accounts':self.rows,'identities':self.identities})

    def _row(self,label):
        if not valid_label(label):
            raise ValueError('账户无效。')
        self._sync()
        row=self.rows.get(label)
        if row is None or not row['identity'] or self.closed:
            raise ValueError('账户未登记或服务正在停止。')
        return row

    def _stale(self,row):
        expired=bool(row['snapshot'] and any(v<=time.time() for v in row['snapshot']['expires_at']))
        return not row['snapshot'] or bool(row['error']) or expired or row['mode']!=self.network_mode() or time.time()-(row['fetched_at'] or 0)>300

    def _pending(self,row):
        return next((rid for rid,op in row['redeems'].items()
            if op['status']=='unknown' and not op.get('resolution')),None)

    def public_state(self):
        with self.lock:
            self._sync();result={}
            for label,row in self.rows.items():
                result[label]=copy.deepcopy({key:row[key] for key in (
                    'snapshot','fetched_at','mode','busy','error','revision','last_reset')})
                result[label]['stale']=self._stale(row)
                result[label]['pending_request_id']=self._pending(row)
            return result

    def query(self,label):
        with self.lock:
            row=self._row(label)
            if row['busy']:return {'accepted':False,'busy':True}
            if time.time()-(row['attempted_at'] or 0)<self.cooldown:
                return {'accepted':False,'busy':False}
            return self._start(label,row,'query',None)

    def consume(self,label,confirmed,revision,request_id):
        with self.lock:
            row=self._row(label)
            if confirmed is not True:
                raise ValueError('需要明确确认用券。')
            try:
                if str(uuid.UUID(request_id))!=request_id:raise ValueError()
            except (ValueError,TypeError,AttributeError):
                raise ValueError('请求编号无效。') from None
            old=row['redeems'].get(request_id)
            if old and (old['status']!='unknown' or old.get('resolution')):
                return {'accepted':False,'busy':bool(row['busy']),'duplicate':True}
            pending=self._pending(row)
            if pending and pending!=request_id:
                raise ValueError('上次重置结果未确认，必须沿用原请求编号。')
            if row['busy']:return {'accepted':False,'busy':True}
            if self._stale(row) or not revision or revision!=row['revision'] or (not pending and row['snapshot']['available_count']<=0):
                raise ValueError('请先查询此账户的可用重置券。')
            if row['last_reset'] and time.time()-row['last_reset']['at']<30 and not pending:
                raise ValueError('刚刚已用券，请稍后刷新额度确认。')
            row['redeems'][request_id]={'status':'running','at':time.time()}
            return self._start(label,row,'consume',request_id)

    def resolve_unknown(self,label,confirmed,request_id):
        """End local confirmation without asserting success or sending a POST."""
        with self.lock:
            row=self._row(label)
            if confirmed is not True:
                raise ValueError('需要明确确认结束上次确认。')
            if type(request_id) is not str:
                raise ValueError('请求编号无效。')
            old=row['redeems'].get(request_id)
            if old and old.get('resolution'):
                return {'accepted':False,'busy':bool(row['busy']),'duplicate':True}
            if self._pending(row)!=request_id or old is None:
                raise ValueError('待确认请求已变化，请刷新页面后重试。')
            if row['busy']:return {'accepted':False,'busy':True}
            resolution={'resolution':'manually_closed','resolved_at':time.time()}
            old.update(resolution)  # Keep the unknown outcome and original identifier.
            if row['last_reset']:row['last_reset'].update(resolution)
            row['snapshot']=None;row['revision']=None;row['error']=None
            self.sequence+=1;self._save()
            return {'accepted':True,'busy':False}

    def _start(self,label,row,action,request_id):
        operation=self.capture();row['busy']=action;row['attempted_at']=time.time()
        self.sequence+=1
        self._save()  # Record the request identifier before any upstream POST.
        self.pool.submit(self._perform,label,row['identity'],self.network_mode(),operation,action,request_id)
        return {'accepted':True,'busy':True}

    def _perform(self,label,identity,mode,operation,action,request_id):
        result=None;error=None
        try:result=operation(label,action,request_id,identity)
        except CreditError as exc:error={'kind':exc.kind,'message':MESSAGES[exc.kind]}
        except Exception:error={'kind':'internal','message':MESSAGES['internal']}
        refresh=False
        with self.lock:
            self._sync();row=self.identities.get(identity)
            if row is None:return
            row['busy']=None;row['error']=error
            self.sequence+=1
            if action=='query':
                if error is None and mode==self.network_mode():
                    row.update(snapshot=result,fetched_at=time.time(),mode=mode,revision=str(uuid.uuid4()))
            else:
                code=result.get('code','reset') if result and error is None else None
                status=('not_applied' if code in ('nothing_to_reset','no_credit') else 'succeeded') if error is None else ('failed' if error['kind'] in ('auth','unsupported','limited','rejected','identity','cancelled') else 'unknown')
                row['redeems'][request_id]['status']=status
                row['last_reset']={'status':status,'at':time.time(),'windows_reset':result.get('windows_reset') if result else None}
                if code:
                    row['redeems'][request_id]['code']=code
                    row['last_reset']['code']=code
                row['snapshot']=None;row['revision']=None
                refresh=True
            self._save()
        if refresh:
            if self.registry().get(label)==identity:self.refresh_quota(label)
            # Only GET follows a terminal business response. A failed follow-up
            # cannot turn the recorded outcome into a retryable unknown.
            if error is None:
                with self.lock:
                    if not self.closed and self.rows.get(label,{}).get('identity')==identity and not self.rows[label]['busy']:
                        self._start(label,self.rows[label],'query',None)

    def close(self):
        with self.lock:self.closed=True
        self.pool.shutdown(wait=True,cancel_futures=True)
