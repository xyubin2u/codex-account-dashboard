"""Read-only refresh helper adapted from the user's verified AGY login tool.

The installed official CLI must match the known installed-app client. No AGY
binary, client secret or account credential is shipped in this package.
"""
from datetime import datetime,timezone
import hashlib
import json
import math
import mmap
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID='1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com'
# Public installed-app client, verified against the official CLI. Its value is
# read from that installed executable, never from account files or web services.
CLIENT_SECRET_HASH='1d2f041093fd95aa8995a038c711d50a7960da09a505381c09a745d6ad0ecc60'
TOKEN_URL='https://oauth2.googleapis.com/token'
PROFILE_URL='https://www.googleapis.com/oauth2/v2/userinfo'
MAX_RESPONSE=256*1024


class ImportError(ValueError):pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def installed_client(binary):
    try:
        path=Path(binary)
        if not path.is_file() or path.stat().st_size>512*1024*1024:raise ValueError()
        with path.open('rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
            if data.find(CLIENT_ID.encode())<0:raise ValueError()
            for match in re.finditer(rb'GOCSPX-[A-Za-z0-9_-]{28}',data):
                value=match.group()
                if hashlib.sha256(value).hexdigest()==CLIENT_SECRET_HASH:
                    return CLIENT_ID,value.decode('ascii')
    except (OSError,ValueError):pass
    raise ImportError('当前官方AGY版本的个人登录参数未匹配，已停止；不会尝试其他客户端或修改原账号。')


def _request(opener,request):
    try:
        with opener.open(request,timeout=20) as response:
            raw=response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE:raise ValueError()
        data=json.loads(raw)
        if not isinstance(data,dict):raise ValueError()
        return data
    except urllib.error.HTTPError as error:
        code=error.code;kind=None
        try:
            body=json.loads(error.read(MAX_RESPONSE));kind=body.get('error') if isinstance(body,dict) else None
        except (ValueError,OSError):pass
        finally:error.close()
        if code==429:message='Google请求过于频繁，请稍后再试；未自动重试。'
        elif kind=='invalid_grant':message='Google未接受此RT：可能已失效、被撤销或与标准AGY授权不匹配。'
        elif kind in ('invalid_client','unauthorized_client'):message='此RT与当前标准AGY客户端不匹配，已停止。'
        elif code in (401,403):message='Google拒绝此次凭据验证，未保存登录。'
        elif 300<=code<400:message='Google请求发生重定向，已阻止转发凭据。'
        else:message='Google暂未完成验证，请稍后再试；未自动重试。'
        raise ImportError(message) from None
    except (OSError,urllib.error.URLError,TimeoutError):
        raise ImportError('无法连接Google验证接口，请检查当前网络；未改动原账号。') from None
    except (ValueError,UnicodeError):
        raise ImportError('Google返回了无法确认的数据，未保存登录。') from None


def exchange_rt(refresh_token,client,opener=None):
    opener=opener or urllib.request.build_opener(NoRedirect())
    client_id,client_secret=client
    body=urllib.parse.urlencode({'client_id':client_id,'client_secret':client_secret,
        'refresh_token':refresh_token,'grant_type':'refresh_token'}).encode()
    data=_request(opener,urllib.request.Request(TOKEN_URL,data=body,headers={
        'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},method='POST'))
    access=data.get('access_token');expires=data.get('expires_in');kind=data.get('token_type','Bearer')
    if not isinstance(access,str) or not access or any(ord(c)<=32 for c in access) or len(access)>32768 or \
            type(expires) not in (int,float) or not math.isfinite(expires) or not 0<expires<=7*86400 or \
            not isinstance(kind,str) or kind.lower()!='bearer':
        raise ImportError('Google返回了不完整的访问令牌，未保存登录。')
    profile=_request(opener,urllib.request.Request(PROFILE_URL,headers={
        'Authorization':'Bearer '+access,'Accept':'application/json'},method='GET'))
    email=profile.get('email');identity=profile.get('id')
    if not isinstance(identity,str) or not identity or len(identity)>256 or \
            not isinstance(email,str) or len(email)>320 or '@' not in email or \
            any(ord(c)<=32 for c in email) or profile.get('verified_email') is not True:
        raise ImportError('Google未返回可核实的账号邮箱，未保存登录。')
    rotated=data.get('refresh_token')
    if rotated is not None and not isinstance(rotated,str):
        raise ImportError('Google返回了无法确认的刷新令牌，未保存登录。')
    token={'access_token':access,'token_type':'Bearer','refresh_token':rotated or refresh_token,
        'expiry':datetime.fromtimestamp(time.time()+expires,timezone.utc).isoformat().replace('+00:00','Z'),
        'expires_in':int(expires)}
    if isinstance(data.get('id_token'),str) and data['id_token']:token['id_token']=data['id_token']
    return token,email.strip().casefold()

