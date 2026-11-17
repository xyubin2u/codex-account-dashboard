"""Manage only this tool's official Codex login files, under persistent locks."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from account_labels import codex_labels,valid_label
from core import SwitchError,atomic_write,load_auth


def _check_directory(path,create=False):
    path=Path(path).absolute()
    for parent in reversed((path,*path.parents)):
        if parent.is_symlink():raise ValueError('账户目录不能使用符号链接。')
    if create:path.mkdir(mode=0o700,parents=True,exist_ok=True)
    value=path.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid!=os.getuid() or value.st_mode&0o022:
        raise ValueError('账户目录的所有者或权限异常。')
    return path


def _check_file(path):
    try:value=path.lstat()
    except FileNotFoundError:return False
    if not stat.S_ISREG(value.st_mode) or value.st_uid!=os.getuid() or value.st_mode&0o077 or value.st_nlink!=1:
        raise ValueError('账户凭据或锁文件的所有者、类型或权限异常。')
    return True


@contextmanager
def registration_lock(base):
    """Also used by the legacy save/login commands; never unlink lock files."""
    base=_check_directory(base,create=True)
    with exclusive_lock(base/'.account-register.lock'):
        yield


@contextmanager
def exclusive_lock(path):
    _check_file(path)
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        value=os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_uid!=os.getuid() or value.st_mode&0o077 or value.st_nlink!=1:
            raise ValueError('账户锁文件的所有者或权限异常。')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('账户正在使用或另一个账户变更正在进行，请稍后重试。') from None
        yield
    finally:os.close(fd)


class CodexAccounts:
    def __init__(self,base,profiles,proc=Path('/proc')):
        # Preserve lexical paths so a symlink cannot become trusted via resolve().
        self.base=Path(base).expanduser().absolute();self.profiles=Path(profiles).expanduser().absolute()
        self.proc=Path(proc)

    def registry(self):
        result={}
        for label in codex_labels(self.base):
            path=self.base/f'auth_account_{label}.json'
            try:
                if path.is_symlink():continue
                _,identity=load_auth(path)
                result[label]=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            except (SwitchError,OSError):continue
        return result

    def labels(self):
        return tuple(self.registry())

    def _paths(self,label):
        if not valid_label(label):raise ValueError('账户编号无效。')
        _check_directory(self.base,create=True);_check_directory(self.profiles,create=True)
        home=self.profiles/label
        if home.exists() or home.is_symlink():_check_directory(home)
        source=self.base/f'auth_account_{label}.json';target=home/'auth.json'
        _check_file(source)
        if home.exists():_check_file(target)
        return source,home,target

    def _busy(self,label):
        """Include native app-server/login, which the activity badge excludes."""
        home=self.profiles/label
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():continue
            try:
                status=(entry/'status').read_text().splitlines()
                uid=int(next(line.split()[1] for line in status if line.startswith('Uid:')))
                if uid!=os.getuid():continue
                try:
                    if (entry/'exe').resolve().name.removesuffix(' (deleted)')!='codex':continue
                except PermissionError:
                    name=next((line.split(':',1)[1].strip() for line in status if line.startswith('Name:')),None)
                    if name and name!='codex':continue
                    raise
                values={key:value for item in (entry/'environ').read_bytes().split(b'\0') if b'=' in item
                    for key,value in [item.split(b'=',1)] if key==b'CODEX_HOME'}
                candidate=values.get(b'CODEX_HOME')
                if candidate and Path(os.fsdecode(candidate)).resolve()==home.resolve():
                    raise ValueError('此账户目录仍有进程运行，请先退出该账户窗口或登录。')
            except (FileNotFoundError,ProcessLookupError,StopIteration):continue
            except PermissionError:
                # An inaccessible same-UID process cannot establish safe idleness.
                raise ValueError('无法核对账户进程，请稍后重试。') from None

    @contextmanager
    def _mutation(self,label):
        self._paths(label)
        with registration_lock(self.base):
            with exclusive_lock(self.profiles/f'.{label}.lock'):
                paths=self._paths(label);self._busy(label)
                yield paths

    @staticmethod
    def _credential(data):
        # Use the same official-file validation as the existing launcher.
        if not isinstance(data,dict):raise ValueError('不是有效的官方 Codex 登录文件。')
        try:
            with tempfile.TemporaryDirectory(prefix='codex-auth-check-') as directory:
                path=Path(directory)/'auth.json';atomic_write(path,data)
                clean,identity=load_auth(path)
        except (SwitchError,ValueError,TypeError,OverflowError):
            raise ValueError('不是有效的官方 Codex 登录文件。') from None
        return clean,hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _add(self,label,data,paths):
        source,home,target=paths
        if source.exists() or target.exists():raise ValueError('账户编号已登记，不能覆盖。')
        clean,identity=self._credential(data)
        if identity in self.registry().values():raise ValueError('此身份已登记，请使用现有账户。')
        home.mkdir(mode=0o700,exist_ok=True)
        atomic_write(source,clean)
        try:atomic_write(target,clean)
        except BaseException:
            source.unlink()
            raise
        return {'label':label,'platform':'codex'}

    def add(self,label,credential_dict):
        with self._mutation(label) as paths:return self._add(label,credential_dict,paths)

    def remove(self,label,expected_identity):
        with self._mutation(label) as (source,home,target):
            actual=self.registry().get(label)
            if not actual or not isinstance(expected_identity,str) or actual!=expected_identity:
                raise ValueError('账户身份已变化，请刷新后重新确认。')
            if target.exists():
                try:_,profile_identity=load_auth(target)
                except SwitchError:raise ValueError('独立目录凭据无法核对，未移除。') from None
                if hashlib.sha256(json.dumps(profile_identity).encode()).hexdigest()!=actual:
                    raise ValueError('独立目录身份不一致，未移除。')
            if target.exists():target.unlink()
            source.unlink()
            return {'label':label,'platform':'codex'}

    def login(self,label):
        binary=shutil.which('codex')
        if not binary:raise ValueError('找不到官方 codex 命令。')
        with self._mutation(label) as paths:
            if paths[0].exists() or paths[2].exists():raise ValueError('账户编号已登记，不能覆盖。')
            with tempfile.TemporaryDirectory(prefix='codex-account-login-') as directory:
                env=os.environ.copy();env['CODEX_HOME']=directory
                result=subprocess.run([binary,'-c','cli_auth_credentials_store="file"','login'],env=env)
                if result.returncode:return result.returncode
                path=Path(directory)/'auth.json'
                if path.is_symlink():raise ValueError('官方登录文件路径异常。')
                try:data,_=load_auth(path)
                except SwitchError:raise ValueError('官方 Codex 登录未生成有效账户。') from None
                # Both registration/profile locks remain held throughout login.
                self._add(label,data,self._paths(label))
                return 0
