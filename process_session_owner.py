"""Observe the exact AUTO child while its native process still owns rollouts."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from datetime import datetime

from account_display import UUID


class RootActivity:
    """Numeric lifecycle projection for the roots held by one native process."""
    def __init__(self, started_at=0):
        self.started_at, self.files = started_at, {}

    def read(self, path, session_id, expected_inode=None):
        try:
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                key=(stat.st_dev,stat.st_ino)
                if expected_inode is not None and key!=expected_inode:
                    self.files.pop(session_id,None)
                    return 'unknown'
                value=self.files.get(session_id)
                if value is None or value['key']!=key or stat.st_size<value['offset'] or (
                        stat.st_size==value['offset'] and value['mtime']!=stat.st_mtime_ns):
                    meta=json.loads(stream.readline(1024*1024))
                    if meta.get('type')!='session_meta' or meta.get('payload',{}).get('id')!=session_id:
                        return 'unknown'
                    value=dict(key=key,offset=max(0,stat.st_size-1024*1024),pending=b'',
                        skip=stat.st_size>1024*1024,state='unknown',turn=None,mtime=stat.st_mtime_ns)
                    self.files[session_id]=value
                stream.seek(value['offset'])
                chunk=stream.read(4*1024*1024)
                value['offset']=stream.tell()
                lines=(value['pending']+chunk).split(b'\n')
                value['pending']=lines.pop()
                for line in lines:
                    if value['skip']:
                        value['skip']=False
                        continue
                    if len(line)>1024*1024 or b'"event_msg"' not in line:
                        continue
                    try:event=json.loads(line)
                    except (ValueError,UnicodeError):continue
                    payload=event.get('payload') if isinstance(event,dict) else None
                    if event.get('type')!='event_msg' or not isinstance(payload,dict):continue
                    kind=payload.get('type');turn=payload.get('turn_id')
                    if kind=='task_started':
                        try:at=datetime.fromisoformat(event.get('timestamp','').replace('Z','+00:00')).timestamp()
                        except (ValueError,TypeError):at=None
                        value['state']='idle' if at is not None and at<self.started_at else 'active'
                        value['turn']=turn
                    elif kind in ('task_complete','turn_aborted') and (
                            not turn or not value['turn'] or turn==value['turn']):
                        value['state']='idle'
                value['mtime']=stat.st_mtime_ns
                if len(value['pending'])>1024*1024:
                    value['pending']=b'';value['skip']=True
                after=path.stat()
                if (after.st_dev,after.st_ino)!=key or value['offset']>after.st_size:
                    self.files.pop(session_id,None)
                    return 'unknown'
                if value['offset']<after.st_size:
                    return 'unknown'
                # Keep the last complete lifecycle event during a partial
                # message. The selected root's input-persistence guard decides
                # when that turn can safely be interrupted and resumed.
                if (value['pending'] or value['skip']) and value['state']=='idle':
                    return 'pending'
                return value['state']
        except (OSError,ValueError,TypeError,AttributeError):
            self.files.pop(session_id,None)
            return 'unknown'


def process_stamp(root):
    # comm can contain spaces or parentheses; starttime is field 22.
    data = (root/'stat').read_text().rsplit(')', 1)[1].split()
    uid = int(next(line.split()[1] for line in (root/'status').read_text().splitlines()
                   if line.startswith('Uid:')))
    return uid, data[19]


def native_process(root, proc):
    """Follow only the Popen process and the official Node wrapper's children."""
    if (root/'exe').resolve().name == 'codex':
        return root
    if (root/'exe').resolve().name not in ('node', 'nodejs'):
        return None
    children = (root/'task'/root.name/'children').read_text().split()
    natives = [proc/pid for pid in children if (proc/pid/'exe').resolve().name == 'codex']
    return natives[0] if len(natives) == 1 else None


def inspect_process(base, home, pid, birth=None, kind='cli', proc=Path('/proc'), uid=None, activity=None):
    """Return numeric/ID evidence only; never retain transcript or auth bodies."""
    uid = os.getuid() if uid is None else uid
    root = proc/str(pid)
    result = dict(status='unavailable', reason='process_unavailable', root_count=0, child_count=0)
    try:
        owner, started = process_stamp(root)
        if owner != uid or birth is not None and started != birth:
            return {**result, 'status':'blocked', 'reason':'process_identity_changed'}
        native = native_process(root, proc)
        if native is None:
            return {**result, 'reason':'native_pending'}
        native_stamp = process_stamp(native)
        result.update(native_pid=int(native.name), native_start=native_stamp[1])
        if native_stamp[0] != uid:
            return {**result, 'status':'blocked', 'reason':'native_uid_changed'}
        values = dict(item.split(b'=', 1) for item in (native/'environ').read_bytes().split(b'\0') if b'=' in item)
        if Path(os.fsdecode(values.get(b'CODEX_HOME', b''))).resolve() != Path(home).resolve():
            return {**result, 'status':'blocked', 'reason':'account_home_mismatch'}
        args = (native/'cmdline').read_bytes().split(b'\0')
        if b'--no-daemon' not in args:
            return {**result, 'status':'blocked', 'reason':'not_isolated_session_process'}
        roots = [(Path(base)/name).resolve() for name in ('sessions', 'archived_sessions')]
        candidates = {}
        unclassified = 0
        for reference in (native/'fd').iterdir():
            relevant = False
            try:
                target = reference.resolve()
                if target.suffix != '.jsonl' or not any(target.is_relative_to(path) for path in roots):
                    continue
                flags = int(next(line.split()[1] for line in (native/'fdinfo'/reference.name).read_text().splitlines()
                                 if line.startswith('flags:')), 8)
                if flags & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR):
                    continue
                relevant = True
                with reference.open('rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    raw = stream.readline(1024*1024)
                    event = json.loads(raw)
                    after = os.fstat(stream.fileno())
                original = target.stat()
                current = reference.stat()
                identities = {(value.st_dev, value.st_ino) for value in (before, after, original, current)}
                final_flags = int(next(line.split()[1] for line in (native/'fdinfo'/reference.name).read_text().splitlines()
                                      if line.startswith('flags:')), 8)
                if len(identities) != 1 or final_flags & os.O_ACCMODE != flags & os.O_ACCMODE:
                    raise ValueError('Writer changed')
                payload = event.get('payload') if isinstance(event, dict) else None
                if event.get('type') != 'session_meta' or not isinstance(payload, dict):
                    raise ValueError('No session header')
                sid = payload.get('id')
                if not isinstance(sid, str) or not UUID.fullmatch(sid):
                    raise ValueError('Invalid session id')
                source = payload.get('source')
                if (isinstance(source, dict) and 'subagent' in source or
                        payload.get('parent_thread_id') or payload.get('thread_source') == 'subagent'):
                    result['child_count'] += 1
                    continue
                if source != kind:
                    raise ValueError('Unclassified root')
                candidates[sid]=(target,(before.st_dev,before.st_ino),reference)
            except (OSError, ValueError, TypeError, AttributeError, StopIteration):
                if relevant:
                    unclassified += 1
        result['root_count'] = len(candidates)
        if process_stamp(root) != (uid, started) or process_stamp(native) != native_stamp:
            return {**result, 'status':'blocked', 'reason':'process_identity_changed'}
        if unclassified:
            return {**result, 'status':'blocked', 'reason':'unclassified_writer'}
        states={sid:activity.read(file,sid,inode) for sid,(file,inode,_) in candidates.items()} if activity is not None else {}
        for file,inode,reference in candidates.values():
            current=file.stat();writer=reference.stat()
            if (current.st_dev,current.st_ino)!=inode or (writer.st_dev,writer.st_ino)!=inode:
                return {**result,'status':'blocked','reason':'writer_changed_during_activity'}
        if process_stamp(root)!=(uid,started) or process_stamp(native)!=native_stamp:
            return {**result,'status':'blocked','reason':'process_identity_changed'}
        if len(candidates) > 1:
            if activity is not None:
                result['root_states']=states
                result['active_count']=sum(value=='active' for value in states.values())
                result['idle_count']=sum(value=='idle' for value in states.values())
                if result['active_count']>1 or any(value=='unknown' for value in states.values()):
                    return {**result,'status':'blocked','reason':'multiple_live_roots'}
                if any(value=='pending' for value in states.values()):
                    return {**result,'status':'pending','reason':'incomplete_lifecycle_snapshot'}
                if all(value=='idle' for value in states.values()):
                    return {**result,'status':'idle','reason':'all_loaded_roots_idle'}
                active=[sid for sid,value in states.items() if value=='active']
                if len(active)==1 and all(value in ('active','idle') for value in states.values()):
                    return {**result,'status':'unique','reason':'only_active_loaded_root','session_id':active[0]}
            return {**result, 'status':'blocked', 'reason':'multiple_live_roots'}
        if candidates:
            return {**result, 'status':'unique', 'reason':'owned_writable_rollout', 'session_id':next(iter(candidates))}
        return {**result, 'status':'none', 'reason':'no_live_root'}
    except (OSError, ValueError, TypeError, StopIteration):
        return result


class ProcessSessionObserver:
    def __init__(self, base, home, proof, nonce, kind='cli', proc=Path('/proc')):
        self.base, self.home = Path(base), Path(home)
        self.proof, self.nonce, self.kind, self.proc = Path(proof), nonce, kind, proc
        self.lock, self.stop = threading.RLock(), threading.Event()
        self.pid = self.birth = self.thread = self.last = None
        self.frozen = False
        self.activity = RootActivity(time.time())

    def start(self, pid):
        self.pid = pid
        self.birth = process_stamp(self.proc/str(pid))[1]
        self.capture()
        self.thread = threading.Thread(target=self.watch, name='codex-session-owner', daemon=True)
        self.thread.start()

    def watch(self):
        while not self.stop.wait(.2):
            self.capture()

    def publish(self, value, target=None):
        fd, temporary = tempfile.mkstemp(prefix='.session-owner-', dir=self.proof.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.write('\n')
            os.replace(temporary, target or self.proof)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def capture(self, finished=False, freezing=False):
        if self.frozen and not freezing:
            return
        # Signal handlers can interrupt this same thread while it is capturing.
        # Normal sampling never waits; termination permits only a short bound.
        if not self.lock.acquire(timeout=.75 if freezing else 0):
            if freezing:
                self.last = dict(status='blocked',reason='final_snapshot_busy',root_count=0,child_count=0)
            return
        try:
            try:
                value = inspect_process(self.base, self.home, self.pid, self.birth, self.kind, self.proc, activity=self.activity)
                # Closing the owned process closes its /proc view; retain the
                # last observation, including a negative/ambiguous observation.
                if (not (self.proc/str(self.pid)).exists() or
                        value['reason'] == 'native_pending' and self.last and self.last.get('native_pid') and
                        not (self.proc/str(self.last['native_pid'])).exists()):
                    value = self.last or value
                elif value['status'] == 'unavailable' and (value.get('native_pid') or self.last and self.last.get('native_pid')):
                    value = {**value, 'status':'blocked', 'reason':'native_evidence_unavailable'}
                value = {**value, 'version':1, 'nonce':self.nonce, 'bridge_pid':os.getpid(), 'finished':finished}
                if self.frozen and not freezing:
                    return
                if value == self.last:
                    return
                self.publish(value)
                self.last = value
            except (OSError, ValueError, TypeError):
                # Ownership observation must not break terminal/signal handling.
                return
        finally:
            self.lock.release()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.frozen:
            return
        self.capture(finished=True)

    def freeze(self):
        """Latch the final live observation before this bridge sends SIGTERM."""
        if self.frozen:
            return
        self.stop.set()
        self.frozen = True
        self.capture(finished=True, freezing=True)
        # This separate terminal record is written only by the stopping path.
        # A background capture cannot overwrite it after releasing its lock.
        value = {**(self.last or dict(status='blocked',reason='no_final_observation')),
            'version':1, 'nonce':self.nonce, 'bridge_pid':os.getpid(), 'finished':True}
        self.publish(value, self.proof.with_name(self.proof.name+'.final'))
