"""Read-only native AGY SQLite usage indexing and conservative live attribution.

Only generation metadata and referenced steps.metadata are read. Conversation
payloads, creator fields and credentials are never copied into the usage index.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time

from agy_quota import AgyProfiles
from agy_usage_parser import parse_generation, generation_step_indices, MAX_BLOB_BYTES

ROOT = Path.home()/'.local/share/agy-switch/shared/history/conversations'
COLUMNS = ('id','at','model','provider','session','source','input_tokens','cached_input_tokens',
           'cache_write_input_tokens','uncached_input_tokens','output_tokens',
           'reasoning_output_tokens','total_tokens','duration_ms','speed_tps','platform','quota_group','completed_at')
API_ENV = {b'GEMINI_API_KEY',b'GOOGLE_API_KEY',b'GOOGLE_GEMINI_BASE_URL',b'AGY_ADC_AUTH',
           b'ANTIGRAVITY_LS_ADDRESS',b'ANTIGRAVITY_CSRF_TOKEN',b'ANTIGRAVITY_EXECUTABLE_DATA_DIR',b'ANTIGRAVITY_CONFIG_REL_DIR'}
HELPERS = {b'models',b'help',b'--help',b'--version',b'update',b'acp',b'mcp',b'--login'}


def _file_key(path):
    try:
        s=path.stat()
        return (s.st_dev,s.st_ino) if stat.S_ISREG(s.st_mode) else None
    except OSError:return None


def _signature(path):
    result=[]
    for p in (path,Path(str(path)+'-wal')):
        try:
            s=p.stat();result.append((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
        except FileNotFoundError:result.append(None)
    return json.dumps(result,separators=(',',':'))


class AgyUsageIndexer:
    def __init__(self,usage,root=None,profiles=None,proc=Path('/proc'),clock=time.time,
                 monotonic=time.monotonic,interval=.5,budget_seconds=.3,parser=None,step_indices=None):
        self.usage,self.profiles=usage,profiles or AgyProfiles()
        self._explicit_root=root is not None
        self.root=Path(root) if root is not None else self.profiles.root/'shared/history/conversations'
        self.proc,self.clock,self.monotonic=Path(proc),clock,monotonic
        self.interval,self.budget_seconds=interval,budget_seconds
        self.parser,self.step_indices=parser or parse_generation,step_indices or generation_step_indices
        self._stop=threading.Event();self._thread=None;self._scan_lock=threading.Lock();self._poll_lock=threading.Lock()
        self.tracked={};self._rotation=0;self._signal={'enabled':True,'started_at':clock(),'checked_at':None,'error':None,'pending_sources':1,'running':False,'attribution_error':None}
        with self.usage._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS agy_usage_sources(
                path TEXT PRIMARY KEY,signature TEXT,cursor INTEGER DEFAULT -1,complete INTEGER DEFAULT 0,error TEXT);
                CREATE TABLE IF NOT EXISTS agy_usage_event_origins(
                path TEXT,gen_idx INTEGER,event_id TEXT,digest TEXT,
                PRIMARY KEY(path,gen_idx));
                CREATE INDEX IF NOT EXISTS agy_usage_origin_event ON agy_usage_event_origins(event_id);''')

    def signal(self):
        result=dict(self._signal)
        result['error']=result.get('error') or result.get('attribution_error')
        return result

    def _stopped(self):
        return self._stop.is_set() or bool(getattr(self.usage,'_stop',None) and self.usage._stop.is_set())

    def _paths(self):
        roots=[self.root]
        if not self._explicit_root:
            # A fresh install has native per-profile history and no shared
            # manifest. Discover metadata without reading login credentials;
            # removed accounts' retained history remains numeric history.
            labels={'A'}
            try:labels.update(self.profiles.labels())
            except (OSError,ValueError):pass
            roots.extend(self.profiles.path(label).parent/'conversations' for label in sorted(labels))
        result=[];seen=set()
        for root in roots:
            if self._stopped():break
            if not root.is_dir() or root.is_symlink():continue
            try:
                resolved=root.resolve()
                for path in root.rglob('*'):
                    if self._stopped():break
                    if path.suffix not in ('.db','.sqlite','.sqlite3') or path.is_symlink():continue
                    if not path.resolve().is_relative_to(resolved):continue
                    key=_file_key(path)
                    if key and key not in seen:result.append(path);seen.add(key)
            except OSError:continue
        return sorted(result,key=str)

    @contextmanager
    def _source(self,path,expected):
        if _file_key(path)!=expected or path.is_symlink():raise OSError('source replaced')
        # mode=ro is essential: never checkpoint, migrate, or write the source.
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.1)
        try:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            if _file_key(path)!=expected:raise OSError('source replaced')
            yield db
            if _file_key(path)!=expected:raise OSError('source replaced')
        finally:db.close()

    @staticmethod
    def _trajectory(db,path):
        try:
            row=db.execute('SELECT trajectory_id FROM trajectory_meta LIMIT 2').fetchall()
            if len(row)==1 and isinstance(row[0][0],str) and 0<len(row[0][0])<=512:return row[0][0]
        except sqlite3.OperationalError:pass
        return path.stem

    def _decode(self,db,trajectory,idx,blob):
        if not isinstance(blob,bytes) or len(blob)>MAX_BLOB_BYTES:return None,None
        indices=self.step_indices(blob);metadata={}
        for start in range(0,len(indices),400):
            selected=indices[start:start+400]
            if not selected:continue
            for step,value in db.execute('SELECT idx,metadata FROM steps WHERE idx IN ('+
                    ','.join('?' for _ in selected)+') AND length(metadata)<=?',(*selected,MAX_BLOB_BYTES)):
                if isinstance(value,bytes):metadata[step]=value
        digest=hashlib.sha256(blob)
        for step_idx,value in sorted(metadata.items()):digest.update(str(step_idx).encode());digest.update(value)
        return self.parser(trajectory,idx,blob,metadata),digest.hexdigest()

    def _write_events(self,path,items):
        changed=False
        with self.usage._connect() as db:
            has_accounts=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_accounts'").fetchone()
            for idx,event,digest in items:
                old=db.execute('SELECT digest,event_id FROM agy_usage_event_origins WHERE path=? AND gen_idx=?',
                    (str(path),idx)).fetchone()
                if old and old['digest']==digest:continue
                if old and old['event_id'] and has_accounts:
                    # A historical row can change outside the observer's tail.
                    # No current writer proof is available for that revision:
                    # revoke its old claim rather than inherit an account.
                    db.execute("UPDATE usage_accounts SET conflict=1 WHERE event_id=? AND NOT EXISTS "
                        "(SELECT 1 FROM events WHERE id=? AND platform!='agy')",
                        (old['event_id'],old['event_id']))
                    changed=True
                if event:
                    # Extra parser evidence is never persisted. Keep only the
                    # fixed numeric/model whitelist shared with UsageStore.
                    values={k:event.get(k) for k in COLUMNS};values['platform']='agy'
                    db.execute('INSERT INTO events('+','.join(COLUMNS)+') VALUES('+','.join('?' for _ in COLUMNS)+') '
                        'ON CONFLICT(id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in COLUMNS if k!='id')+
                        " WHERE events.platform='agy'",tuple(values[k] for k in COLUMNS))
                    changed=True
                db.execute('INSERT INTO agy_usage_event_origins VALUES(?,?,?,?) '
                    'ON CONFLICT(path,gen_idx) DO UPDATE SET event_id=excluded.event_id,digest=excluded.digest',
                    (str(path),idx,event['id'] if event else None,digest))
        if changed:self.usage._invalidate()

    def scan(self):
        if self._stopped() or not self._scan_lock.acquire(False):return self.signal()
        try:
            self._signal['running']=True
            started=self.monotonic();paths=self._paths()
            with self.usage._connect() as target:
                target.executemany('INSERT OR IGNORE INTO agy_usage_sources(path) VALUES(?)',[(str(p),) for p in paths])
                for path in paths:
                    target.execute('UPDATE agy_usage_sources SET complete=0 WHERE path=? AND (signature IS NULL OR signature!=?)',
                        (str(path),_signature(path)))
            if paths:
                offset=self._rotation%len(paths);paths=paths[offset:]+paths[:offset]
            for n,path in enumerate(paths):
                if self._stopped() or self.monotonic()-started>=self.budget_seconds:break
                try:
                    signature=_signature(path);key=_file_key(path)
                    with self.usage._connect() as target:
                        old=target.execute('SELECT * FROM agy_usage_sources WHERE path=?',(str(path),)).fetchone()
                    if old and old['complete'] and old['signature']==signature:continue
                    cursor=old['cursor'] if old and not old['complete'] and old['signature']==signature else -1
                    items=[];complete=True
                    with self._source(path,key) as source:
                        trajectory=self._trajectory(source,path)
                        for idx,blob in source.execute('SELECT idx,data FROM gen_metadata WHERE idx>? AND length(data)<=? ORDER BY idx',
                                                     (cursor,MAX_BLOB_BYTES)):
                            if self._stopped() or self.monotonic()-started>=self.budget_seconds:
                                complete=False;break
                            event,digest=self._decode(source,trajectory,idx,blob)
                            items.append((idx,event,digest));cursor=idx
                    self._write_events(path,items)
                    # If a WAL write raced the snapshot, the next pass must
                    # revisit the whole metadata set rather than seal it.
                    stable=signature==_signature(path)
                    with self.usage._connect() as target:
                        target.execute('INSERT INTO agy_usage_sources VALUES(?,?,?,?,NULL) '
                            'ON CONFLICT(path) DO UPDATE SET signature=excluded.signature,cursor=excluded.cursor,'
                            'complete=excluded.complete,error=NULL',(str(path),signature,cursor,int(complete and stable)))
                    self._signal['error']=None
                except (OSError,sqlite3.Error,ValueError):
                    with self.usage._connect() as target:
                        target.execute('INSERT INTO agy_usage_sources(path,error) VALUES(?,?) '
                            'ON CONFLICT(path) DO UPDATE SET error=excluded.error',
                            (str(path),'AGY 本地用量暂不可读，保留上次结果。'))
                    self._signal['error']='部分 AGY 本地用量暂不可读，保留上次结果。'
                self._rotation=offset+n+1
            with self.usage._connect() as target:
                pending=target.execute('SELECT COUNT(*) FROM agy_usage_sources WHERE complete=0').fetchone()[0]
                failed=target.execute('SELECT COUNT(*) FROM agy_usage_sources WHERE error IS NOT NULL').fetchone()[0]
            self._signal.update(pending_sources=pending,running=False,
                error='部分 AGY 本地用量暂不可读，保留上次结果。' if failed else None)
            return self.signal()
        finally:
            self._signal['running']=False
            self._scan_lock.release()

    def _owner(self,entry,saved):
        try:
            binary=Path(getattr(self.profiles,'binary',self.profiles.home/'.local/bin/agy'))
            if _file_key(entry/'exe')!=_file_key(binary) or _file_key(binary) is None:return None
            uid=int(next(s.split()[1] for s in (entry/'status').read_text().splitlines() if s.startswith('Uid:')))
            if uid!=os.getuid():return None
            args=(entry/'cmdline').read_bytes().split(b'\0')[1:]
            if any(arg in HELPERS for arg in args):return None
            env={k:v for part in (entry/'environ').read_bytes().split(b'\0') if b'=' in part for k,v in [part.split(b'=',1)]}
            if any(env.get(k) for k in API_ENV):return None
            view=entry/'root'/str(self.profiles.path('A')).lstrip('/')
            key=_file_key(view)
            matches=[label for label in saved if key and key==_file_key(self.profiles.path(label))]
            if len(matches)!=1:return None
            label=matches[0];row=self.profiles.read(label)
            if row['identity']!=saved[label]['identity']:return None
            start=(entry/'stat').read_text().rsplit(')',1)[1].split()[19]
            return (entry.name,start,label,row['identity'])
        except (OSError,ValueError,IndexError,StopIteration):return None

    def _writers(self,paths):
        wanted={key for path in paths for key in (_file_key(path),_file_key(Path(str(path)+'-wal'))) if key}
        result={key:{} for key in wanted}
        try:saved=self.profiles.registry();entries=list(self.proc.iterdir())
        except (OSError,ValueError):return result
        for entry in entries:
            if not entry.name.isdigit():continue
            try:
                owner=self._owner(entry,saved)
                for fd in (entry/'fd').iterdir():
                    try:
                        flags=int(next(s.split()[1] for s in (entry/'fdinfo'/fd.name).read_text().splitlines() if s.startswith('flags:')),8)
                        if flags&os.O_ACCMODE not in (os.O_WRONLY,os.O_RDWR):continue
                        key=_file_key(fd)
                        if key in result:result[key][entry.name]=owner
                    except (OSError,ValueError,StopIteration):continue
                if owner and self._owner(entry,saved)!=owner:
                    for group in result.values():
                        if entry.name in group:group[entry.name]=None
            except OSError:continue
        return result

    @staticmethod
    def _exclusive(path,writers):
        owners={}
        for key in (_file_key(path),_file_key(Path(str(path)+'-wal'))):
            if key:owners.update(writers.get(key,{}))
        if len(owners)!=1:return None
        return next(iter(owners.values()))

    def _latest(self,path,previous):
        key=_file_key(path)
        with self._source(path,key) as source:
            trajectory=self._trajectory(source,path)
            latest=source.execute('SELECT MAX(idx) FROM gen_metadata').fetchone()[0]
            if latest is None:return key,{},-1
            lower=previous['max_idx'] if previous else latest
            # Revisit tail rows for counters that finish or mutate in place.
            indices={r[0] for r in source.execute('SELECT idx FROM gen_metadata ORDER BY idx DESC LIMIT 4')}
            if previous:indices.update(previous.get('pending',()))
            rows={}
            for idx,blob in source.execute('SELECT idx,data FROM gen_metadata WHERE idx>? AND length(data)<=? ORDER BY idx LIMIT 256',
                                            (lower,MAX_BLOB_BYTES)):
                event,digest=self._decode(source,trajectory,idx,blob);rows[idx]=(event,digest)
            for idx in indices:
                if idx in rows:continue
                row=source.execute('SELECT data FROM gen_metadata WHERE idx=? AND length(data)<=?',(idx,MAX_BLOB_BYTES)).fetchone()
                if row:rows[idx]=self._decode(source,trajectory,idx,row[0])
            return key,rows,latest

    def _revoke(self,ids):
        if not ids:return
        with self.usage._connect() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_accounts'").fetchone()
            if not exists:return
            for eid in ids:
                db.execute("UPDATE usage_accounts SET conflict=1 WHERE event_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM events WHERE id=? AND platform!='agy')",(eid,eid))
        self.usage._invalidate()

    def poll(self):
        if self._stopped() or not self._poll_lock.acquire(False):return
        try:
            now,mono=self.clock(),self.monotonic();paths=self._paths();before=self._writers(paths)
            next_tracked={};candidates=[];revoke=[]
            for path in paths:
                if self._stopped():break
                owner=self._exclusive(path,before);old=self.tracked.get(str(path));signature=_signature(path)
                if old and old['signature']==signature:
                    next_tracked[str(path)]={**old,'owner':owner,'checked':mono,
                        'since':old['since'] if old['owner']==owner and mono-old['checked']<=3 else now}
                    continue
                try:key,rows,latest=self._latest(path,old)
                except (OSError,sqlite3.Error,ValueError):continue
                stable=bool(old and old['key']==key and old['owner']==owner and owner and mono-old['checked']<=3)
                hashes={idx:digest for idx,(_,digest) in rows.items()}
                for idx,(event,digest) in rows.items():
                    if not event or old and old['hashes'].get(idx)==digest:continue
                    new=bool(old and (idx>old['max_idx'] or idx in old['pending']))
                    if old and idx in old['hashes'] and not stable:revoke.append(event['id'])
                    if stable and new and event.get('completed_at') is not None and old['since']<=event['at']<=now+5:
                        candidates.append((path,key,owner,event))
                next_tracked[str(path)]={'owner':owner,'key':key,'signature':signature,'checked':mono,
                    'since':old['since'] if stable else now,'hashes':hashes,'max_idx':latest,
                    'pending':{idx for idx,(event,_) in rows.items() if event is None or event.get('completed_at') is None}}
            after=self._writers(paths);claims=[]
            for path,key,owner,event in candidates:
                if _file_key(path)==key and self._exclusive(path,after)==owner:
                    claims.append({'id':event['id'],'account':owner[2],'identity':owner[3]})
                else:revoke.append(event['id']);next_tracked.pop(str(path),None)
            self._revoke(revoke)
            if claims:self.usage.attribute_accounts(claims)
            self.tracked=next_tracked;self._signal.update(checked_at=now,attribution_error=None)
        except Exception:
            self.tracked={};self._signal.update(checked_at=self.clock(),attribution_error='AGY 新用量归属暂不可确认，保留未归属。')
        finally:self._poll_lock.release()

    def start(self):
        if self._thread is None and not self._stopped():
            self.poll();self._thread=threading.Thread(target=self._watch,name='agy-usage-accounts',daemon=True);self._thread.start()

    def _watch(self):
        while not self._stop.wait(self.interval):self.poll()

    def close(self):
        self._stop.set()
        if self._thread:self._thread.join(2)
