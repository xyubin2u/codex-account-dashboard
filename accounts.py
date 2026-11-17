"""Local account management without exposing credentials to status responses."""
from contextlib import ExitStack, contextmanager
import hmac
import os
from pathlib import Path
import secrets
import threading


class AccountManager:
    def __init__(self,base=None,profiles=None,store=None,credits=None,agy_store=None,adapters=None):
        self.store,self.credits,self.agy_store=store,credits,agy_store
        self.lock=threading.RLock()
        self.tickets={}
        if adapters is None:
            from codex_accounts import CodexAccounts
            from agy_accounts import AgyAccounts
            from agy_quota import AgyProfiles
            base=Path(base or os.environ.get('CODEX_MULTI_BASE',str(Path.home()/'.codex')))
            profiles=Path(profiles or os.environ.get('CODEX_MULTI_PROFILES',str(Path(__file__).parent/'accounts')))
            adapters={'codex':CodexAccounts(base,profiles),'agy':AgyAccounts(AgyProfiles())}
        if set(adapters)!={'codex','agy'}:raise ValueError('账号平台配置无效。')
        self.adapters=adapters

    def _adapter(self,platform):
        if not isinstance(platform,str) or platform not in self.adapters:
            raise ValueError('请选择 Codex 或 AGY。')
        return self.adapters[platform]

    @contextmanager
    def _guard(self):
        with self.lock,ExitStack() as locks:
            # No store calls another store while holding its lock. Keeping this
            # order also serializes queued refresh/credit acceptance with CRUD.
            for store in (self.store,self.credits,self.agy_store):
                if store is not None:locks.enter_context(store.lock)
            yield

    def _signature(self,platform,label):
        adapter=self.adapters[platform]
        if platform=='codex' and hasattr(adapter,'base'):
            paths=[Path(adapter.base)/f'auth_account_{label}.json']
        elif platform=='agy' and hasattr(adapter,'profiles'):
            paths=[adapter.profiles.path(label)]
        else:return None
        result=[]
        for path in paths:
            try:
                stat=path.lstat()
                result.append((stat.st_dev,stat.st_ino,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_size))
            except OSError:result.append(None)
        return tuple(result)

    def _ticket(self,platform,label,identity):
        key=(platform,label)
        binding=(identity,self._signature(platform,label))
        previous=self.tickets.get(key)
        if previous is None or previous[0]!=binding:
            previous=(binding,secrets.token_urlsafe(32))
            self.tickets[key]=previous
        return previous[1]

    def _next(self,platform):
        from account_labels import next_label
        occupied=set(self.adapters[platform].registry())
        if platform=='agy':occupied.add('A')  # A is the external native profile.
        return next_label(occupied)

    def public_state(self):
        from account_labels import sort_key
        with self._guard():
            rows=[]
            for platform,adapter in self.adapters.items():
                for label,identity in sorted(adapter.registry().items(),key=lambda item:sort_key(item[0])):
                    rows.append({'platform':platform,'label':label,
                        'external':platform=='agy' and label=='A',
                        'remove_token':self._ticket(platform,label,identity)})
            keys={(row['platform'],row['label']) for row in rows}
            self.tickets={key:value for key,value in self.tickets.items() if key in keys}
            next_labels={platform:self._next(platform) for platform in self.adapters}
            return {'accounts':rows,'next_labels':next_labels,
                'login_commands':{platform:f'codex-switch accounts login {platform} --label {label}'
                                  for platform,label in next_labels.items()}}

    def _busy(self,platform,label):
        stores=(self.store,self.credits) if platform=='codex' else (self.agy_store,)
        for store in stores:
            if store is not None:
                row=getattr(store,'rows',{}).get(label,{})
                if row.get('refreshing') or row.get('busy'):
                    raise ValueError('此账号有查询或用券任务正在进行，请完成后再移除。')

    def _changed(self,platform,label):
        self.tickets.pop((platform,label),None)
        stores=(self.store,self.credits) if platform=='codex' else (self.agy_store,)
        for store in stores:
            if store is not None and hasattr(store,'_sync'):
                store._sync()
                if hasattr(store,'_save'):store._save()

    def add(self,platform,label,credential):
        from account_labels import valid_label
        adapter=self._adapter(platform)
        if not isinstance(credential,dict):raise ValueError('请选择官方客户端的 JSON 登录文件。')
        if label is not None and not isinstance(label,str):raise ValueError('账号编号无效。')
        with self._guard():
            label=label or self._next(platform)
            if not valid_label(label):raise ValueError('账号编号须为大写字母，如 A、I、AA。')
            self._busy(platform,label)
            result=adapter.add(label,credential)
            self._changed(platform,label)
            return {'ok':True,'platform':platform,'label':result.get('label',label)}

    def remove(self,platform,label,remove_token,confirmed):
        from account_labels import valid_label
        adapter=self._adapter(platform)
        if not valid_label(label) or confirmed is not True or not isinstance(remove_token,str):
            raise ValueError('请重新打开移除确认框并确认当前账号。')
        with self._guard():
            identity=adapter.registry().get(label)
            if identity is None or not hmac.compare_digest(self._ticket(platform,label,identity),remove_token):
                raise ValueError('账号或登录资料已变更，请重新打开确认框。')
            self._busy(platform,label)
            result=adapter.remove(label,identity)
            self._changed(platform,label)
            return {'ok':True,'platform':platform,'label':result.get('label',label)}

    def login(self,platform,label=None):
        from account_labels import valid_label
        adapter=self._adapter(platform)
        label=label or self._next(platform)
        if not valid_label(label):raise ValueError('账号编号须为大写字母，如 A、I、AA。')
        # Native login is interactive in the caller's TTY. Never run it in an
        # HTTP worker or claim success simply because a terminal was opened.
        return adapter.login(label)

    def run(self,platform,label,args):
        from account_labels import valid_label
        if platform!='agy' or not valid_label(label):raise ValueError('请使用 run agy --label 编号 -- 参数。')
        return self._adapter(platform).run(label,args)
