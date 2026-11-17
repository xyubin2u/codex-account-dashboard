"""Explicit AUTO membership; credentials and quota state are stored separately."""
import fcntl
import json
import os
from pathlib import Path
import tempfile

from account_labels import valid_label,sort_key

LABELS = 'ABCDEFGH'
DEFAULT = set('BCDEFGH')
NAME = 'codex-switch-auto-pool.json'

class PoolError(ValueError):
    pass

def read_pool(base):
    path=Path(base)/NAME
    if path.is_symlink():raise PoolError('AUTO 号池配置路径异常。')
    try:
        data=json.loads(path.read_text())
    except FileNotFoundError:
        return set(DEFAULT)
    except (OSError,ValueError,UnicodeError):
        raise PoolError('AUTO 号池配置无法读取，已停止自动选号。') from None
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('enabled'),list):
        raise PoolError('AUTO 号池配置格式错误，已停止自动选号。')
    enabled=data['enabled']
    if any(not valid_label(x) for x in enabled) or len(set(enabled))!=len(enabled):
        raise PoolError('AUTO 号池账户配置无效，已停止自动选号。')
    return set(enabled)

def set_member(base,label,enabled):
    if not valid_label(label) or type(enabled) is not bool:
        raise ValueError('账户或开关无效。')
    base=Path(base);base.mkdir(mode=0o700,parents=True,exist_ok=True)
    lockpath=base/(NAME+'.lock')
    flags=os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(lockpath,flags,0o600)
    with os.fdopen(fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        selected=read_pool(base)
        if enabled:selected.add(label)
        else:selected.discard(label)
        outfd,tmp=tempfile.mkstemp(prefix='.auto-pool-',dir=base)
        try:
            with os.fdopen(outfd,'w') as out:
                os.fchmod(out.fileno(),0o600);json.dump({'version':1,'enabled':sorted(selected,key=sort_key)},out);out.write('\n');out.flush();os.fsync(out.fileno())
            os.replace(tmp,base/NAME)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        return selected
