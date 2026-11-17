"""Manual AGY account management with isolated official login and safe removal."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys

from account_labels import valid_label,sort_key
from agy_quota import AgyError,validate_credential
from agy_runtime.files import directory,private_dir,private_file,exists,write_json
from agy_runtime.shared_history import attach_shared_history,sync_continue_pointer
from agy_runtime.launcher import permission_defaults

HERE=Path(__file__).resolve().parent
API_ENV=('GEMINI_API_KEY','GOOGLE_API_KEY','GOOGLE_GEMINI_BASE_URL','AGY_ADC_AUTH',
         'ANTIGRAVITY_LS_ADDRESS','ANTIGRAVITY_CSRF_TOKEN',
         'ANTIGRAVITY_EXECUTABLE_DATA_DIR','ANTIGRAVITY_CONFIG_REL_DIR')


def canonical(email):
    local,domain=email.casefold().split('@',1)
    return local.split('+',1)[0].replace('.','')+'@gmail.com' if domain in ('gmail.com','googlemail.com') else email.casefold()


def file_key(path):
    try:
        info=path.stat();return info.st_dev,info.st_ino
    except OSError:return None


class AgyAccounts:
    def __init__(self,profiles):self.profiles=profiles

    def labels(self):return sorted(self.profiles.registry(),key=sort_key)

    def registry(self):return {label:row['identity'] for label,row in self.profiles.registry().items()}

    def _label(self,label):
        if not valid_label(label):raise AgyError('AGY 账号标签无效，请使用大写英文字母。')
        return label

    @contextmanager
    def _mutation(self,label):
        private_dir(self.profiles.root);private_dir(self.profiles.root/'runtime');fds=[]
        try:
            with directory(self.profiles.root/'runtime') as parent:
                for name in ('accounts-mutation.lock',label+'.lock'):
                    try:fd=os.open(name,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
                    except OSError:raise AgyError('账号锁不可用，已停止更改。') from None
                    fds.append(fd);info=os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise AgyError('账号锁必须为当前用户的私有普通文件。')
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise AgyError('此账号正在使用或有账号更改尚未结束，请稍后再试。') from None
            yield tuple(fds)
        finally:
            for fd in reversed(fds):os.close(fd)

    def _assert_idle(self,label):
        token=self.profiles.path(label);gemini=self.profiles.gemini(label)
        token_key=file_key(token);directory_key=file_key(gemini)
        try:entries=list(self.profiles.proc.iterdir())
        except OSError:raise AgyError('暂时无法核对 AGY 进程，已停止更改。') from None
        for proc in entries:
            if not proc.name.isdigit():continue
            try:
                if proc.stat().st_uid!=os.getuid():continue
                try:binary_name=Path(os.readlink(proc/'exe')).name.removesuffix(' (deleted)')
                except PermissionError:
                    status=(proc/'status').read_text().splitlines()
                    name=next((line.split(':',1)[1].strip() for line in status if line.startswith('Name:')),None)
                    if name and name!='agy':continue
                    raise
                known_binary=file_key(Path(self.profiles.binary))
                if binary_name!='agy' and (known_binary is None or file_key(proc/'exe')!=known_binary):continue
                view=proc/'root'/str(self.profiles.path('A')).lstrip('/')
                home_view=proc/'root'/str(self.profiles.gemini('A')).lstrip('/')
                if token_key and file_key(view)==token_key or directory_key and file_key(home_view)==directory_key:
                    raise AgyError('此 AGY 账号仍有原生进程在使用，请退出该账号窗口后重试。')
                # Native clients may retain the old open auth inode even after a rename.
                try:
                    if token_key and any(file_key(fd)==token_key for fd in (proc/'fd').iterdir()):
                        raise AgyError('此 AGY 账号仍有登录文件被使用，请稍后再试。')
                except FileNotFoundError:pass
                if not file_key(home_view) and not file_key(view):
                    raise AgyError('AGY 进程归属暂不可确认，已停止更改。')
            except (FileNotFoundError,ProcessLookupError):continue
            except PermissionError:raise AgyError('暂时无法核对 AGY 进程，已停止更改。') from None

    def _prepare(self,label):
        if label=='A':return
        for path in (self.profiles.root/'accounts',self.profiles.root/'accounts'/label,
                     self.profiles.gemini(label),self.profiles.path(label).parent,
                     self.profiles.root/'accounts'/label/'config-antigravity'):
            private_dir(path)

    def _command(self,label,args):
        if label=='A':return [self.profiles.binary,*args]
        # Bind targets are empty view anchors; original native data is untouched.
        with directory(self.profiles.home/'.gemini',create=True):pass
        with directory(self.profiles.home/'.config/antigravity',create=True):pass
        return ['/usr/bin/unshare','--user','--map-current-user','--keep-caps','--mount','--propagation','private',
            sys.executable,str(HERE/'agy_runtime/namespace_exec.py'),str(self.profiles.gemini(label)),
            str(self.profiles.gemini('A')),str(self.profiles.root/'accounts'/label/'config-antigravity'),
            str(self.profiles.home/'.config/antigravity'),'--',self.profiles.binary,*args]

    def _call(self,command,locks):
        env={key:value for key,value in os.environ.items() if key not in API_ENV};env['HOME']=str(self.profiles.home)
        code=subprocess.call(command,env=env,pass_fds=locks)
        return code if code>=0 else 128-code

    def _unique(self,label,row):
        # Tombstoned native A is still a real identity, never a spare account.
        for other in set(self.profiles.labels())|{'A'}:
            if other==label:continue
            try:previous=self.profiles.read(other)
            except AgyError:continue
            if canonical(previous['email'])==canonical(row['email']):
                raise AgyError('此身份已存在于 AGY 账户 '+other+'，不会重复保存。')

    def _tombstone(self,label,removed):
        labels=self.profiles.removed()
        if removed:labels.add(label)
        else:labels.discard(label)
        write_json(self.profiles.root/'runtime/removed-accounts.json',{'version':1,'removed':sorted(labels,key=sort_key)},replace=True)

    def add(self,label,credential_dict):
        label=self._label(label);row=validate_credential(credential_dict)
        with self._mutation(label):
            self._assert_idle(label);self._unique(label,row)
            if label=='A':
                native=self.profiles.read('A')
                if native['identity']!=row['identity']:raise AgyError('A 只能重新纳管现有原生账号，不能替换默认登录。')
                self._tombstone(label,False)
            else:
                if exists(self.profiles.path(label)):raise AgyError('此位置已有登录文件，未覆盖原文件。')
                self._prepare(label);attach_shared_history(self.profiles,label)
                write_json(self.profiles.path(label),{'auth_method':'consumer','id_token_email':row['email'],'token':row['token']})
                self._tombstone(label,False)
        return {'platform':'agy','label':label}

    def remove(self,label,expected_identity):
        label=self._label(label)
        with self._mutation(label):
            if self.registry().get(label)!=expected_identity or not expected_identity:raise AgyError('账号身份已变化或未登记，请刷新后重试。')
            self._assert_idle(label)
            if label=='A':self._tombstone(label,True)
            else:
                path=self.profiles.path(label)
                with private_file(path) as fd,directory(path.parent) as parent:
                    # Check the same file one last time before unlinking.
                    if file_key(path)!=(os.fstat(fd).st_dev,os.fstat(fd).st_ino) or self.profiles.read(label)['identity']!=expected_identity:
                        raise AgyError('登录文件已变化，请刷新后重试。')
                    os.unlink(path.name,dir_fd=parent)
        return {'platform':'agy','label':label}

    def login(self,label):
        label=self._label(label)
        if label=='A':raise AgyError('A 保留原生登录，请从 B 开始添加 AGY 账号。')
        with self._mutation(label) as locks:
            self._assert_idle(label)
            if exists(self.profiles.path(label)):raise AgyError('此位置已有登录文件，未覆盖原文件。')
            self._prepare(label);attach_shared_history(self.profiles,label)
            # The official CLI can leave credentials even after cancellation or
            # an error. Only a completed, locally validated login is managed.
            self._tombstone(label,True)
            code=self._call(self._command(label,['acp','--login','--disable-telemetry']),locks)
            if code==0:
                row=self.profiles.read(label)
                try:self._unique(label,row)
                except AgyError:
                    # Keep the official result for inspection, but do not register duplicates.
                    self._tombstone(label,True);raise
                self._tombstone(label,False)
            return code

    def run(self,label,args):
        label=self._label(label)
        if not isinstance(args,(list,tuple)) or any(not isinstance(arg,str) or '\0' in arg for arg in args):
            raise AgyError('AGY 启动参数无效。')
        with self.profiles.account_lock(label) as fd:
            if label not in self.registry():raise AgyError('此 AGY 账号尚未纳管，请先添加账号。')
            self._prepare(label);attach_shared_history(self.profiles,label)
            try:return self._call(self._command(label,permission_defaults(args)),(fd,))
            finally:
                try:sync_continue_pointer(self.profiles,label)
                except (OSError,ValueError):print('最近会话指针同步未完成，历史正文仍保留。',file=sys.stderr)
