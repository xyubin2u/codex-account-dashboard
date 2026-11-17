"""Private, identity-bound calendar dates entered by the owner; no auth writes."""
import copy
from datetime import date as calendar_date
import fcntl
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from account_labels import valid_label
from core import ACCOUNTS, atomic_write


def valid_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise ValueError('请填写完整日期，格式为 YYYY-MM-DD。')
    try:parsed=calendar_date.fromisoformat(value)
    except ValueError:raise ValueError('请填写有效的日历日期。') from None
    if not 2000<=parsed.year<=2199:raise ValueError('日期年份需在2000至2199之间。')
    return value


class SubscriptionDates:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock();self.cached=None;self.signature=None

    def _read(self):
        if self.path.is_symlink():raise ValueError('手动日期配置路径异常，未保存。')
        try:
            stat=self.path.stat();signature=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            if signature==self.signature:return copy.deepcopy(self.cached)
            data=json.loads(self.path.read_text())
            if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('accounts'),dict):raise ValueError()
            for label,row in data['accounts'].items():
                if not valid_label(label) or not isinstance(row,dict) or set(row)!={'identity','date','updated_at'}:raise ValueError()
                if not isinstance(row['identity'],str) or not row['identity']:raise ValueError()
                valid_date(row['date'])
                if type(row['updated_at']) not in (int,float) or not math.isfinite(row['updated_at']) or row['updated_at']<=0:raise ValueError()
            self.signature=signature;self.cached=copy.deepcopy(data)
            return data
        except FileNotFoundError:
            self.signature=None;self.cached=None;return {'version':1,'accounts':{}}
        except (OSError,ValueError,UnicodeError,TypeError,KeyError):
            raise ValueError('手动日期配置无法读取；请先检查配置，原文件未覆盖。') from None

    def get(self,label,identity):
        with self.lock:
            row=self._read()['accounts'].get(label)
            return copy.deepcopy(row) if row and row['identity']==identity else None

    def set(self,label,identity,value,now=None):
        if not valid_label(label) or not isinstance(identity,str) or not identity:raise ValueError('账户无效。')
        if value is not None:valid_date(value)
        with self.lock:
            self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            fd=os.open(self.path.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX);data=self._read()
                if value is None:
                    old=data['accounts'].get(label)
                    if old and old['identity']==identity:data['accounts'].pop(label)
                    result=None
                else:
                    result={'identity':identity,'date':value,'updated_at':time.time() if now is None else now}
                    data['accounts'][label]=result
                atomic_write(self.path,data)
                self.signature=None;self.cached=None
                return copy.deepcopy(result)
