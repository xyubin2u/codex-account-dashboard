"""Attribute newly observed usage to an isolated CLI's account, read-only.

Evidence is a current writable rollout FD plus a matching isolated login.
Creation metadata, global mtime and a lone active account are not evidence.
"""
import json
import os
import stat
from pathlib import Path
import threading
import time

from activity import API_OVERRIDES, HELPERS, CodexActivity
from account_labels import valid_label
from usage_parser import clean, counters, digest, stamp

MAX_LINE = 8*1024*1024
ENV_KEYS = API_OVERRIDES | {b'CODEX_HOME', b'OPENAI_BASE_URL', b'CODEX_BASE_URL'}
EXEC_CONFIG_KEYS={b'model',b'model_reasoning_effort',b'model_reasoning_summary',
                  b'model_verbosity',b'approval_policy',b'sandbox_mode'}


def subscription_exec(args):
    """Recognize direct exec without treating its caller as the account owner."""
    if args[:1]!=[b'exec']:return False
    index=1
    while index<len(args):
        value=args[index]
        if (value in (b'--profile',b'-p',b'--oss',b'--local-provider') or
                value.startswith((b'--profile=',b'--local-provider=')) or
                value.startswith(b'-p') and not value.startswith(b'--')):
            return False
        config=None
        if value in (b'-c',b'--config'):
            index+=1
            if index>=len(args):return False
            config=args[index]
        elif value.startswith(b'--config='):config=value[len(b'--config='):]
        elif value.startswith(b'-c') and not value.startswith(b'--'):config=value[2:]
        if config is not None:
            key,separator,setting=config.partition(b'=');key=key.strip()
            if not separator:return False
            if key==b'cli_auth_credentials_store':
                if setting.strip() not in (b'file',b'"file"',b"'file'"):return False
            elif key==b'model_provider':
                if setting.strip() not in (b'openai',b'"openai"',b"'openai'"):return False
            elif key not in EXEC_CONFIG_KEYS:return False
        index+=1
    return True


class UsageAccountObserver:
    def __init__(self, store, base, profiles, proc=Path('/proc'), clock=time.time,
                 monotonic=time.monotonic, interval=.5):
        self.store, self.base, self.profiles = store, Path(base).resolve(), Path(profiles).resolve()
        self.proc, self.clock, self.monotonic, self.interval = Path(proc), clock, monotonic, interval
        self.activity = CodexActivity(self.base,self.profiles,proc=self.proc)
        self.tracked, self._lock, self._stop = {}, threading.Lock(), threading.Event()
        self._thread = None
        self._signal = dict(enabled=True,started_at=clock(),checked_at=None,error=None)

    def signal(self):
        with self._lock:
            return dict(self._signal)

    def start(self):
        if self._thread is None:
            self.poll()
            self._thread=threading.Thread(target=self._watch,name='codex-usage-accounts',daemon=True)
            self._thread.start()

    def _watch(self):
        while not self._stop.wait(self.interval):
            self.poll()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _writers(self):
        saved = self.activity.registered()
        result = {}
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry/'exe').resolve().name!='codex':
                    continue
                uid=int(next(line.split()[1] for line in (entry/'status').read_text().splitlines() if line.startswith('Uid:')))
                # Every readable Codex writer participates in conflict detection,
                # even if it is a helper, API client or has an unverified login.
                # Filtering these out would falsely make another owner unique.
                owner=self._owner(entry,saved) if uid==self.activity.uid else None
                for fd in (entry/'fd').iterdir():
                    try:
                        flags=int(next(line.split()[1] for line in (entry/'fdinfo'/fd.name).read_text().splitlines()
                                       if line.startswith('flags:')),8)
                        if flags & os.O_ACCMODE not in (os.O_WRONLY,os.O_RDWR):
                            continue
                        st=fd.stat()
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        # Conflict detection is by physical file, including
                        # hard links outside the source tree or with another
                        # suffix. Such paths are never opened for their content.
                        key=(st.st_dev,st.st_ino)
                        group=result.setdefault(key,{'path':None,'stat':st,'owners':{}})
                        group['owners'][entry.name]=owner
                        path=fd.readlink()
                        if not path.is_absolute() or path.suffix!='.jsonl' or path.is_symlink():
                            continue
                        path=path.resolve()
                        if not any(path.is_relative_to(self.base/folder) for folder in ('sessions','archived_sessions')):
                            continue
                        file_stat=path.stat()
                        if (st.st_dev,st.st_ino)!=(file_stat.st_dev,file_stat.st_ino):
                            continue
                        group['path']=path;group['stat']=file_stat
                    except (OSError,ValueError,StopIteration):
                        continue
                # Do not rely on an identity that changed during the FD scan.
                if owner and self.activity._identity(self.profiles/owner[2]/'auth.json')!=owner[3]:
                    for group in result.values():
                        if entry.name in group['owners']:
                            group['owners'][entry.name]=None
            except (OSError,ValueError,IndexError,StopIteration):
                continue
        return result

    def _owner(self,entry,saved):
        try:
            args=(entry/'cmdline').read_bytes().split(b'\0')[1:]
            # Automation callers commonly use `codex exec` without --no-daemon.
            # Only the actual command position is evidence, never prompt text.
            if args[:1]==[b'exec']:
                if not subscription_exec(args):return None
            elif b'--no-daemon' not in args:return None
            if any(arg in HELPERS for arg in args):
                return None
            values={key:value for item in (entry/'environ').read_bytes().split(b'\0') if b'=' in item
                    for key,value in [item.split(b'=',1)] if key in ENV_KEYS}
            if any(values.get(key) for key in ENV_KEYS-{b'CODEX_HOME'}):
                return None
            home=Path(os.fsdecode(values.get(b'CODEX_HOME',b'')))
            label=home.name
            if not valid_label(label) or home!=self.profiles/label or home.is_symlink():
                return None
            identity=self.activity._identity(home/'auth.json')
            if identity is None or saved.get(label)!=identity or list(saved.values()).count(identity)!=1:
                return None
            process_start=(entry/'stat').read_text().rsplit(')',1)[1].split()[19]
            return (entry.name,process_start,label,identity)
        except (OSError,ValueError,IndexError):
            return None

    @staticmethod
    def _session(path,expected):
        with path.open('rb') as stream:
            st=os.fstat(stream.fileno())
            if (st.st_dev,st.st_ino)!=expected:
                return None
            raw=stream.readline(MAX_LINE)
        if len(raw)>=MAX_LINE or not raw.endswith(b'\n'):
            return None
        try:
            event=json.loads(raw)
            if event.get('type')=='session_meta' and isinstance(event.get('payload'),dict):
                return clean(event['payload'].get('id') or event['payload'].get('session_id'), '') or None
        except (ValueError,AttributeError):
            pass
        return None

    @staticmethod
    def _event_id(raw,session,earliest,latest):
        if b'"token_usage_record"' not in raw and b'"token_count"' not in raw:
            return None
        try:
            event=json.loads(raw)
        except (ValueError,UnicodeDecodeError):
            return None
        if not isinstance(event,dict) or not isinstance(event.get('payload'),dict):
            return None
        at=stamp(event.get('timestamp'))
        if at is None or at<earliest or at>latest+5:
            return None
        p=event['payload']
        if event.get('type')=='token_usage_record':
            if p.get('thread_id') and p['thread_id']!=session:
                return None
            values=counters(p.get('usage'))
            if values is not None and any(values):
                return digest(['record',session,clean(p.get('response_id'),'') or [at,values]])
        if event.get('type')=='event_msg' and p.get('type')=='token_count' and isinstance(p.get('info'),dict):
            info=p['info'];last=counters(info.get('last_token_usage'));total=counters(info.get('total_token_usage'))
            if last is not None or total is not None:
                return digest(['snapshot',session,at,digest([last,total])])
        return None

    @staticmethod
    def _tail(stream,offset):
        position=stream.tell();start=max(0,offset-512)
        stream.seek(start);value=digest(list(stream.read(offset-start)))
        stream.seek(position)
        return value

    def poll(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            now,mono=self.clock(),self.monotonic()
            writers=self._writers();next_tracked={};claims=[]
            for key,group in writers.items():
                if len(group['owners'])!=1 or group['path'] is None:
                    continue
                owner=next(iter(group['owners'].values()))
                if owner is None:
                    continue
                path=group['path'];st=group['stat']
                previous=self.tracked.get(key)
                if (previous is None or previous['owner']!=owner or
                        mono-previous['checked']>3 or st.st_size<previous['offset']):
                    session=self._session(path,key)
                    if session:
                        with path.open('rb') as stream:
                            opened=os.fstat(stream.fileno())
                            if (opened.st_dev,opened.st_ino)!=key:
                                continue
                            tail=self._tail(stream,st.st_size)
                        next_tracked[key]=dict(owner=owner,offset=st.st_size,session=session,
                            since=now,checked=mono,tail=tail)
                    continue
                current=dict(previous,checked=mono)
                with path.open('rb') as stream:
                    opened=os.fstat(stream.fileno())
                    if (opened.st_dev,opened.st_ino)!=key:
                        continue
                    if self._tail(stream,previous['offset'])!=previous['tail']:
                        current.update(offset=st.st_size,since=now,tail=self._tail(stream,st.st_size))
                    else:
                        stream.seek(previous['offset'])
                        while stream.tell()<st.st_size and not self._stop.is_set():
                            offset=stream.tell();raw=stream.readline(MAX_LINE)
                            if current.get('discarding') or len(raw)>=MAX_LINE:
                                current['discarding']=not raw.endswith(b'\n')
                                current['offset']=stream.tell()
                                continue
                            if not raw.endswith(b'\n'):
                                stream.seek(offset)
                                break
                            ident=self._event_id(raw,current['session'],current['since'],now)
                            if ident:
                                claims.append(dict(id=ident,account=owner[2],identity=owner[3]))
                            current['offset']=stream.tell()
                        current['tail']=self._tail(stream,current['offset'])
                next_tracked[key]=current
            if claims:
                self.store.attribute_accounts(claims)
            self.tracked=next_tracked
            self._signal.update(checked_at=now,error=None)
        except Exception:
            self.tracked={}
            self._signal.update(checked_at=self.clock(),error='账号归属观察暂时不可用，新记录暂保留未归属。')
        finally:
            self._lock.release()
