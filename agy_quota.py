"""Read existing local AGY identities and Google's official quota metadata only."""
from datetime import datetime
from contextlib import contextmanager,nullcontext
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from quota_network import network_environment
from account_labels import valid_label, sort_key
from agy_runtime.files import directory, read_json, private_dir

AGY_ROOT=Path(os.environ.get('AGY_SWITCH_ROOT',str(Path.home()/'.local/share/agy-switch')))
PROD='https://cloudcode-pa.googleapis.com/v1internal:'
DAILY='https://daily-cloudcode-pa.googleapis.com/v1internal:'
PROFILE='https://www.googleapis.com/oauth2/v2/userinfo'
MAX_BYTES=256*1024


class AgyError(ValueError):pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


class ExplicitProxyHandler(urllib.request.ProxyHandler):
    """The selected route must not consult process-wide NO_PROXY exceptions."""
    def proxy_open(self,req,proxy,kind):
        try:
            value=urllib.parse.urlsplit(proxy)
            if value.scheme not in ('http','https') or not value.hostname or value.path not in ('','/') or value.query or value.fragment:raise ValueError()
            host=value.hostname
            if ':' in host:host='['+host+']'
            if value.port:host+=':'+str(value.port)
            if value.username is not None:
                credentials=urllib.parse.unquote(value.username)+':'+urllib.parse.unquote(value.password or '')
                req.add_header('Proxy-authorization','Basic '+base64.b64encode(credentials.encode()).decode('ascii'))
            req.set_proxy(host,value.scheme)
        except ValueError:raise AgyError('所选 AGY 查询代理无效。') from None
        return None


def timestamp(value):
    if not isinstance(value,str):return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:return None
        stamp=parsed.timestamp()
        return stamp if math.isfinite(stamp) and stamp>0 else None
    except (ValueError,OverflowError,OSError):return None


def text(value,limit=200):
    return ''.join(c for c in value[:limit] if ord(c)>=32) if isinstance(value,str) else ''


def normalize_summary(data):
    try:
        groups=data.get('groups')
        if groups is None and isinstance(data.get('buckets'),list):groups=[{'displayName':'AGY','buckets':data['buckets']}]
        if not isinstance(groups,list) or not 1<=len(groups)<=64:raise ValueError()
        result=[]
        for group in groups:
            name=text(group['displayName']);buckets=group['buckets']
            if not name.strip() or not isinstance(buckets,list) or not 1<=len(buckets)<=64:raise ValueError()
            normalized=[];ids=set()
            for bucket in buckets:
                bid=bucket['bucketId'];fraction=bucket.get('remainingFraction');amount=bucket.get('remainingAmount')
                if not isinstance(bid,str) or not bid or len(bid)>200 or bid in ids:raise ValueError()
                ids.add(bid)
                if fraction is not None and (type(fraction) not in (int,float) or not math.isfinite(fraction) or not 0<=fraction<=1):raise ValueError()
                if amount is not None and not (type(amount) is int and 0<=amount<=2**53-1 or isinstance(amount,str) and amount.isascii() and amount.isdigit() and len(amount)<=20):raise ValueError()
                if 'disabled' in bucket and type(bucket['disabled']) is not bool:raise ValueError()
                normalized.append({'id':text(bid),'display_name':text(bucket.get('displayName')) or text(bid),
                    'window':text(bucket.get('window'),100),'remaining':round(fraction*100,4) if fraction is not None else None,
                    'remaining_amount':str(amount) if amount is not None else None,'disabled':bucket.get('disabled') is True,
                    'reset_at':timestamp(bucket.get('resetTime'))})
            result.append({'display_name':name,'buckets':normalized})
        if len(json.dumps(result).encode())>64*1024:raise ValueError()
        return result
    except (KeyError,TypeError,ValueError,AttributeError):
        raise AgyError('AGY 官方额度数据暂时无法确认。') from None


def validate_credential(data):
    try:
        if not isinstance(data,dict) or len(json.dumps(data,allow_nan=False).encode())>MAX_BYTES:raise ValueError()
        email=data['id_token_email'];token=data['token']
        if data.get('auth_method')!='consumer' or not isinstance(email,str) or '@' not in email or len(email)>320 or any(ord(c)<32 for c in email):raise ValueError()
        email=email.strip().casefold()
        if any(c.isspace() for c in email) or email.startswith('@') or email.endswith('@') or email.count('@')!=1:raise ValueError()
        if not isinstance(token,dict) or not any(isinstance(token.get(k),str) and token[k] and len(token[k])<=32768 and all(ord(c)>32 for c in token[k]) for k in ('access_token','refresh_token')):raise ValueError()
        for key in ('access_token','refresh_token'):
            if key in token and (not isinstance(token[key],str) or len(token[key])>32768 or any(ord(c)<=32 for c in token[key])):raise ValueError()
        return {'email':email,'identity':hashlib.sha256(email.encode()).hexdigest(),'token':token}
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
        raise AgyError('AGY 登录文件格式无效，请选择官方客户端的个人登录文件。') from None


class AgyProfiles:
    def __init__(self,root=None,home=None,proc=Path('/proc'),binary=None):
        self.root=Path(root or os.environ.get('AGY_SWITCH_ROOT') or AGY_ROOT).absolute()
        self.home=Path(home or os.environ.get('AGY_HOME') or Path.home()).absolute()
        self.proc=Path(proc);self.binary=str(binary or shutil.which('agy') or self.home/'.local/bin/agy')

    def gemini(self,label):
        if not valid_label(label):raise AgyError('AGY 账号标签无效，请使用大写英文字母。')
        return self.home/'.gemini' if label=='A' else self.root/'accounts'/label/'gemini'

    def path(self,label):
        return self.gemini(label)/'antigravity-cli/antigravity-oauth-token'

    @contextmanager
    def account_lock(self,label):
        if not valid_label(label):raise AgyError('AGY 账号标签无效，请使用大写英文字母。')
        private_dir(self.root);private_dir(self.root/'runtime')
        with directory(self.root/'runtime') as parent:
            try:fd=os.open(label+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
            except OSError:raise AgyError('账号锁不可用，已停止操作。') from None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise AgyError('账号锁必须为当前用户的私有普通文件。')
            try:fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
            except BlockingIOError:raise AgyError('此账号正在更改或登录，请稍后再试。') from None
            yield fd
        finally:os.close(fd)

    def removed(self):
        try:
            data=read_json(self.root/'runtime/removed-accounts.json')
            labels=data['removed']
            if not isinstance(labels,list) or any(not valid_label(label) for label in labels):raise ValueError()
            return set(labels)
        except FileNotFoundError:return set()
        except (ValueError,KeyError,TypeError,OSError):
            raise AgyError('AGY 账号登记暂不可读，已停止自动发现。') from None

    def labels(self):
        labels={'A'}
        try:
            with directory(self.root/'accounts') as fd:
                for name in os.listdir(fd):
                    if valid_label(name):labels.add(name)
        except FileNotFoundError:pass
        except (ValueError,OSError):return ['A'] if 'A' not in self.removed() else []
        return sorted(labels-self.removed(),key=sort_key)

    def read(self,label):
        try:return validate_credential(read_json(self.path(label),MAX_BYTES))
        except (OSError,ValueError,KeyError,TypeError,UnicodeError):
            raise AgyError('AGY 本地登录暂不可读，请检查该账号登录状态。') from None

    def registry(self):
        result={}
        for label in self.labels():
            try:
                row=self.read(label)
                if any(other['identity']==row['identity'] for other in result.values()):continue
                result[label]={key:row[key] for key in ('email','identity')}
            except AgyError:pass
        return result

    def active(self):
        counts={};paths={label:self.path(label) for label in self.labels()};identities={}
        for label,p in paths.items():
            try:s=p.stat();identities[(s.st_dev,s.st_ino)]=label
            except OSError:pass
        try:entries=self.proc.iterdir()
        except OSError:return counts
        for proc in entries:
            if not proc.name.isdigit():continue
            try:
                if proc.stat().st_uid!=os.getuid() or (proc/'exe').resolve().name!='agy':continue
                args=(proc/'cmdline').read_bytes().split(b'\0')
                if any(arg in (b'models',b'help',b'--help',b'update',b'--version') for arg in args[1:]):continue
                p=proc/'root'/str(self.path('A')).lstrip('/')
                s=p.stat();label=identities.get((s.st_dev,s.st_ino))
                if label:counts[label]=counts.get(label,0)+1
            except (OSError,RuntimeError):continue
        return counts


def opener_for(mode):
    env=network_environment(dict(os.environ),mode)
    proxies={}
    for scheme in ('http','https'):
        proxy=env.get(scheme.upper()+'_PROXY') or env.get(scheme+'_proxy')
        if proxy:proxies[scheme]=proxy
    if mode=='clash' and not proxies.get('https'):raise AgyError('Clash 查询代理暂不可用。')
    return urllib.request.build_opener(ExplicitProxyHandler(proxies),NoRedirect())


def google_request(url,payload,token,mode):
    permitted={PROFILE,*[base+action for base in (PROD,DAILY) for action in ('loadCodeAssist','retrieveUserQuotaSummary')]}
    if url not in permitted:raise AgyError('不支持的 AGY 元数据端点。')
    headers={'Authorization':'Bearer '+token,'User-Agent':'antigravity/hub/2.23.0 windows/amd64'}
    raw=None
    if url!=PROFILE:
        headers['Content-Type']='application/json';raw=json.dumps(payload).encode()
    request=urllib.request.Request(url,data=raw,headers=headers,method='GET' if raw is None else 'POST')
    try:
        with opener_for(mode).open(request,timeout=15) as response:
            content=response.read(MAX_BYTES+1)
        if len(content)>MAX_BYTES:raise ValueError()
        data=json.loads(content)
        if not isinstance(data,dict):raise ValueError()
        return data
    except urllib.error.HTTPError as error:
        code=error.code;error.close()
        if code in (401,403):message='AGY 额度查询授权不足或已失效，请核对该账号授权。'
        elif code==429:message='AGY 查询过于频繁，稍后再试。'
        elif 300<=code<400:message='AGY 查询出现重定向，已停止转发登录资料。'
        else:message='AGY 官方额度服务暂不可用。'
        raise AgyError(message) from None
    except (OSError,urllib.error.URLError,TimeoutError):raise AgyError('AGY 额度查询网络暂不可用。') from None
    except (ValueError,UnicodeError):raise AgyError('AGY 官方返回了无法确认的数据。') from None


class AgyQuotaClient:
    def __init__(self,profiles,request=google_request,clock=time.time):
        self.profiles=profiles;self.request=request;self.clock=clock
        self.auth={};self.projects={};self.lock=threading.Lock()

    def access(self,row,mode):
        token=row['token'];identity=row['identity'];refresh=token.get('refresh_token')
        signature=hashlib.sha256(json.dumps(token,sort_keys=True).encode()).hexdigest()
        with self.lock:cached=self.auth.get(identity)
        if cached and cached['signature']==signature and cached['expires']>self.clock()+60:return cached['access']
        if cached and cached['signature']==signature:
            refresh=cached.get('refresh') or refresh
        access=token.get('access_token');expires=timestamp(token.get('expiry')) or 0
        if not isinstance(access,str) or not access or expires<=self.clock()+60:
            if not isinstance(refresh,str) or not refresh:raise AgyError('AGY 登录需要在原账号中更新。')
            try:
                # Same verified installed-app client as the local RT importer.
                # Access/refresh data remain in this process; original login files are read-only.
                from agy_runtime.refresh import installed_client,exchange_rt
                binary=getattr(self.profiles,'binary',shutil.which('agy') or str(Path.home()/'.local/bin/agy'))
                fresh,email=exchange_rt(refresh,installed_client(binary),opener=opener_for(mode))
                if email.casefold()!=row['email']:raise AgyError('AGY 在线身份与本地账号不一致，已停止查询。')
                access=fresh['access_token'];expires=timestamp(fresh['expiry']) or 0
                refresh=fresh.get('refresh_token') or refresh
            except AgyError:raise
            except Exception:raise AgyError('AGY 登录刷新暂未成功，请核对账号授权或网络。') from None
        else:
            profile=self.request(PROFILE,None,access,mode)
            if not isinstance(profile.get('email'),str) or profile['email'].strip().casefold()!=row['email']:
                raise AgyError('AGY 在线身份与本地账号不一致，已停止查询。')
        with self.lock:
            self.auth[identity]={'signature':signature,'access':access,'expires':expires,'refresh':refresh}
        return access

    def query(self,label,mode):
        lock=getattr(self.profiles,'account_lock',None)
        with lock(label) if lock else nullcontext():return self._query(label,mode)

    def _query(self,label,mode):
        row=self.profiles.read(label);access=self.access(row,mode)
        key=(row['identity'],mode)
        with self.lock:project=self.projects.get(key)
        if not project or self.clock()-project['at']>=3600:
            loaded=self.request(PROD+'loadCodeAssist',{'metadata':{'ideType':'ANTIGRAVITY','ideVersion':'2.23.0','ideName':'antigravity'}},access,mode)
            pid=loaded.get('cloudaicompanionProject')
            if not isinstance(pid,str) or not pid.strip() or loaded.get('ineligibleTiers'):
                raise AgyError('AGY 原账号的额度项目尚未就绪，请先核对原生登录。')
            tier=loaded.get('paidTier') or loaded.get('currentTier') or {}
            tier=tier.get('id') if isinstance(tier,dict) else tier
            if not isinstance(tier,str):tier='Free'
            plan='Pro' if tier.startswith('g1-pro-') else 'Ultra' if tier.startswith('g1-ultra-') else 'Free' if tier=='free-tier' else text(tier) or 'Free'
            project={'id':pid.strip(),'plan_type':plan,'at':self.clock()}
            with self.lock:self.projects[key]=project
        base=DAILY if project['plan_type'] in ('Pro','Ultra') else PROD
        data=self.request(base+'retrieveUserQuotaSummary',{'project':project['id']},access,mode)
        groups=normalize_summary(data)
        return {'identity':row['identity'],'email':row['email'],'plan_type':project['plan_type'],'groups':groups}
