"""Read-only Codex CLI activity detection, independent of quota requests."""
import hashlib
import json
import os
from pathlib import Path
import threading

from account_labels import codex_labels
from core import ACCOUNTS, SwitchError, load_auth

API_OVERRIDES={b'OPENAI_API_KEY',b'CODEX_API_KEY',b'CODEX_ACCESS_TOKEN',
               b'OPENAI_IDENTITY_TOKEN_FILE',b'OPENAI_WORKLOAD_IDENTITY'}
HELPERS={b'app-server',b'daemon',b'mcp-server',b'--help',b'--version',b'login',b'logout'}


class CodexActivity:
    def __init__(self,base,profiles,proc=Path('/proc'),uid=None):
        self.base=Path(base).resolve();self.profiles=Path(profiles).resolve()
        self.proc=Path(proc);self.uid=os.getuid() if uid is None else uid
        self.lock=threading.RLock();self.cache={}

    def _identity(self,path):
        try:
            if path.is_symlink():return None
            stat=path.stat();signature=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            old=self.cache.get(path)
            if old and old[0]==signature:return old[1]
            _,identity=load_auth(path)
            after=path.stat()
            if signature!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                return None
            value=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            self.cache[path]=(signature,value)  # No token or account identity retained.
            return value
        except (OSError,SwitchError):
            self.cache.pop(path,None)
            return None

    def registered(self):
        with self.lock:
            return {label:value for label in codex_labels(self.base)
                    if (value:=self._identity(self.base/f'auth_account_{label}.json')) is not None}

    def __call__(self):
        with self.lock:
            saved=self.registered();counts={}
            for entry in self.proc.iterdir():
                if not entry.name.isdigit():continue
                try:
                    if (entry/'exe').resolve().name!='codex':continue
                    uid=int(next(line.split()[1] for line in (entry/'status').read_text().splitlines() if line.startswith('Uid:')))
                    if uid!=self.uid:continue
                    args=(entry/'cmdline').read_bytes().split(b'\0')[1:]
                    if any(arg in HELPERS for arg in args):continue
                    environ=(entry/'environ').read_bytes().split(b'\0')
                    values={key:value for item in environ if b'=' in item
                            for key,value in [item.split(b'=',1)]
                            if key in API_OVERRIDES|{b'CODEX_HOME',b'HOME'}}
                    if any(values.get(key) for key in API_OVERRIDES):continue
                    home=Path(os.fsdecode(values[b'CODEX_HOME'])) if values.get(b'CODEX_HOME') else Path(os.fsdecode(values.get(b'HOME',os.fsencode(self.base.parent))))/'.codex'
                    if not home.is_absolute() or home.is_symlink():continue
                    home=home.resolve()
                    isolated=next((label for label in codex_labels(self.base) if home==self.profiles/label),None)
                    if home!=self.base and isolated is None:continue
                    identity=self._identity(home/'auth.json')
                    matches=[label for label,value in saved.items() if value==identity]
                    if len(matches)!=1 or isolated and isolated!=matches[0]:continue
                    label=matches[0];counts[label]=counts.get(label,0)+1
                except (OSError,ValueError,StopIteration):
                    continue  # Processes can exit while the scan is in progress.
            return counts
