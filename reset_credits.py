"""Account-isolated Codex reset-credit transport; never prints upstream payloads."""
from datetime import datetime
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from auto_child import account_lock
from account_labels import valid_label
from core import ACCOUNTS, load_auth
from quota_data import registry

URL = 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits'
MESSAGES = {
    'auth': '券查询未获授权，请先刷新此账户额度，再重试查询券。',
    'network': '重置券接口连接失败，请稍后重试。',
    'timeout': '重置券请求超时。',
    'unsupported': '此账户的重置券接口暂不可用。',
    'limited': '重置券接口请求过于频繁，请稍后重试。',
    'rejected': '上游未接受此次用券，请重新查询券和额度。',
    'invalid': '重置券接口返回了无法确认的数据。',
    'identity': '账户已变化，请重新查询后操作。',
    'cancelled': '查询路线已改变，请重新查询。',
    'internal': '重置券操作未完成，请稍后重试。',
}


class CreditError(Exception):
    def __init__(self, kind):
        self.kind = kind if kind in MESSAGES else 'internal'
        super().__init__(MESSAGES[self.kind])


def _expiry(value):
    try:
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return None
            return stamp.timestamp()
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return value / 1000 if value > 1e12 else value
    except (ValueError, OverflowError, OSError):
        pass
    return None


def parse_credits(body):
    count = None
    if isinstance(body, dict):
        raw_count = body.get('available_count', body.get('availableCount'))
        if raw_count is not None:
            if type(raw_count) is not int or raw_count < 0:
                raise CreditError('invalid')
            count = raw_count
        listed = next((body[k] for k in ('credits','rate_limit_reset_credits','items','data') if k in body), None)
        if listed is not None and not isinstance(listed,list):
            raise CreditError('invalid')
        if listed is None and count is None:
            raise CreditError('invalid')
    elif isinstance(body, list):
        listed = body
    else:
        raise CreditError('invalid')
    available, expires = 0, []
    for row in listed or []:
        if not isinstance(row,dict):
            raise CreditError('invalid')
        if row.get('reset_type',row.get('resetType','codex_rate_limits')) != 'codex_rate_limits':
            continue
        if row.get('status','available') != 'available':
            continue
        raw = row.get('expires_at',row.get('expiresAt'))
        expiry = _expiry(raw)
        if expiry is not None and expiry <= time.time():
            continue
        available += 1
        if expiry is not None:
            expires.append(expiry)
    # A detailed list is authoritative; counts alone cannot supply expiry dates.
    return {'available_count':available if listed is not None else count,
            'expires_at':sorted(expires)}


def parse_consumed(body):
    if not isinstance(body,dict):
        raise CreditError('invalid')
    code = body.get('code')
    windows = body.get('windows_reset',body.get('windowsReset',0))
    if type(windows) is not int or windows < 0:
        raise CreditError('invalid')
    # Official backend outcomes, including idempotent completion and a request
    # that did not apply a reset. None of these require another confirmation.
    # Keep the code through the worker boundary and in the durable ledger.
    if code in ('ok','success') and windows > 0:
        code = 'reset'  # Compatibility with the earlier adapter response.
    if code not in ('reset','already_redeemed','nothing_to_reset','no_credit'):
        raise CreditError('invalid')
    return {'code':code,'windows_reset':windows}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_credit(action, tokens, request_id=None):
    """Exactly one upstream request. Redirects and automatic POST retries are disabled."""
    headers = {'Authorization':'Bearer '+tokens['access_token'],
        'ChatGPT-Account-Id':tokens['account_id'], 'Accept':'application/json',
        'OpenAI-Beta':'codex-1', 'OAI-Language':'zh-CN', 'Originator':'codex_cli_rs'}
    data = None
    if action == 'consume':
        data = json.dumps({'redeem_request_id':request_id}).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(URL+('/consume' if action=='consume' else ''),
        headers=headers,data=data,method='POST' if action=='consume' else 'GET')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:
            raw = response.read(512*1024+1)
            if len(raw)>512*1024:
                raise CreditError('invalid')
            body = json.loads(raw)
    except urllib.error.HTTPError as exc:
        exc.close()
        kind = {401:'auth',403:'auth',404:'unsupported',429:'limited',400:'rejected',409:'rejected'}.get(exc.code,'network')
        raise CreditError(kind) from None
    except (TimeoutError, socket.timeout):
        raise CreditError('timeout') from None
    except (ValueError,UnicodeError):
        raise CreditError('invalid') from None
    except (urllib.error.URLError,OSError):
        raise CreditError('network') from None
    return parse_credits(body) if action=='query' else parse_consumed(body)


class RoutedCreditClient:
    def __init__(self, base, profiles, quota_router):
        self.base, self.profiles, self.quota_router = Path(base), Path(profiles), quota_router

    def capture(self):
        route = self.quota_router.capture()
        def operate(label, action, request_id, identity):
            if not valid_label(label) or action not in ('query','consume'):
                raise CreditError('internal')
            if route.stop.is_set():
                raise CreditError('cancelled')
            with account_lock(self.base,self.profiles,label) as home:
                if registry(self.base).get(label) != identity:
                    raise CreditError('identity')
                data, _ = load_auth(home/'auth.json')
                # Same route and private DNS mount as native quota queries. Tokens
                # remain in memory and travel through stdin, never argv or env.
                payload = {'action':action,'request_id':request_id,
                    'tokens':{k:data['tokens'][k] for k in ('access_token','account_id')}}
                try:
                    result = subprocess.run(route.command_prefix+[sys.executable,str(Path(__file__).resolve()),'--worker'],
                        input=json.dumps(payload),text=True,capture_output=True,env=route.env,timeout=24)
                    answer = json.loads(result.stdout) if result.returncode==0 else {}
                except subprocess.TimeoutExpired:
                    raise CreditError('timeout') from None
                except (OSError,ValueError):
                    raise CreditError('network') from None
                if not answer.get('ok'):
                    raise CreditError(answer.get('error','network'))
                if action=='query':
                    clean=parse_credits({'available_count':answer['result']['available_count']})
                    clean['expires_at']=[v for v in answer['result']['expires_at'] if type(v) in (int,float) and math.isfinite(v)]
                    return clean
                return parse_consumed(answer['result'])
        return operate


def worker():
    try:
        payload = json.load(sys.stdin)
        if payload['action'] not in ('query','consume'):
            raise CreditError('internal')
        result = request_credit(payload['action'],payload['tokens'],payload.get('request_id'))
        answer = {'ok':True,'result':result}
    except CreditError as exc:
        answer = {'ok':False,'error':exc.kind}
    except Exception:
        answer = {'ok':False,'error':'internal'}
    print(json.dumps(answer))


if __name__=='__main__' and sys.argv[1:]==['--worker']:
    worker()
