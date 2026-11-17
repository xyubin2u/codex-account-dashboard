"""Read public quota fields through the existing isolated Codex account homes."""
import base64
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
import unicodedata

from account_labels import valid_label,codex_labels
from core import ACCOUNTS, SwitchError, account_path, check_environment, load_auth
from auto_child import account_lock

ERRORS = {
    'network': '额度接口连接失败，请稍后重试。',
    'timeout': '额度查询超时，请稍后重试。',
    'auth': '此账户登录已失效，需要重新授权。',
    'not_registered': '此账户尚未登记，或本地登录凭据无效。',
    'cancelled': '本次查询已停止。',
    'internal': '本次查询未完成，请稍后重试。',
}


class QuotaError(Exception):
    def __init__(self, kind):
        self.kind = kind if kind in ERRORS else 'internal'
        super().__init__(ERRORS[self.kind])


def registry(base):
    accounts = {}
    for label in codex_labels(base):
        try:
            _, identity = load_auth(account_path(base, label))
            accounts[label] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        except SwitchError:
            continue
    return accounts


def account_display(base, label, expected_identity):
    """Only the display fields requested by the owner, bound to this row's identity."""
    empty={'display_name':None,'email':None}
    if expected_identity is None:
        return empty
    try:
        data,identity=load_auth(account_path(Path(base),label))
        if hashlib.sha256(json.dumps(identity).encode()).hexdigest()!=expected_identity:
            return empty
        raw=data['tokens']['id_token'].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        profile=claims.get('https://api.openai.com/profile') or {}
        if not isinstance(profile,dict):profile={}
        def clean(value,limit):
            if not isinstance(value,str):return None
            value=''.join(c for c in value if not unicodedata.category(c).startswith('C'))
            return ' '.join(value.split())[:limit] or None
        email=clean(claims.get('email') or profile.get('email'),254)
        name=clean(claims.get('name') or claims.get('preferred_username') or profile.get('name'),96)
        return {'display_name':name or email,'email':email}
    except (SwitchError,ValueError,IndexError,TypeError,AttributeError):
        return empty


def account_subscription(base,label,expected_identity):
    """A dated subscription record from the matching login, never JWT exp."""
    empty={'plan_type':None,'active_until':None,'checked_at':None,'source':None}
    if expected_identity is None:return empty
    try:
        data,identity=load_auth(account_path(Path(base),label))
        if hashlib.sha256(json.dumps(identity).encode()).hexdigest()!=expected_identity:
            return empty
        raw=data['tokens']['id_token'].split('.')[1]
        claims=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        details=claims.get('https://api.openai.com/auth')
        if not isinstance(details,dict):return empty
        def stamp(value):
            if not isinstance(value,str) or not value:return None
            try:
                date=datetime.fromisoformat(value.replace('Z','+00:00'))
                if date.tzinfo is None:return None
                result=date.timestamp()
                return result if math.isfinite(result) and result>0 else None
            except (ValueError,OverflowError,OSError):return None
        plan=details.get('chatgpt_plan_type')
        if not isinstance(plan,str) or not plan.replace('_','').isalnum():plan=None
        return {'plan_type':plan.lower()[:32] if plan else None,
                'active_until':stamp(details.get('chatgpt_subscription_active_until')),
                'checked_at':stamp(details.get('chatgpt_subscription_last_checked')),
                'source':'login_profile'}
    except (SwitchError,ValueError,IndexError,TypeError,AttributeError):
        return empty


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_limits(result):
    groups = result.get('rateLimitsByLimitId') or {}
    limits = groups.get('codex') or result.get('rateLimits') or next(iter(groups.values()), {})
    windows = {'five_hour': {'remaining': None, 'reset_at': None},
               'weekly': {'remaining': None, 'reset_at': None}}
    for bucket in (limits.get('primary'), limits.get('secondary')):
        if not isinstance(bucket, dict):
            continue
        minutes = bucket.get('windowDurationMins', bucket.get('window_duration_mins'))
        name = {300: 'five_hour', 10080: 'weekly'}.get(minutes)
        if not name:
            continue
        used = bucket.get('usedPercent', bucket.get('used_percent'))
        reset = bucket.get('resetsAt', bucket.get('resets_at'))
        if _number(used):
            windows[name]['remaining'] = round(max(0, min(100, 100-used)), 2)
        if _number(reset) and reset > 0:
            windows[name]['reset_at'] = reset/1000 if reset > 100000000000 else reset
    # A complete one-window response explicitly declares the other slot empty.
    # Keep an omitted/malformed slot unknown; do not infer this from account A
    # or from a missing percentage. Native primary can itself be the weekly limit.
    if all(key in limits for key in ('primary','secondary')):
        buckets=[limits['primary'],limits['secondary']]
        if sum(bucket is None for bucket in buckets)==1:
            active=next(bucket for bucket in buckets if bucket is not None)
            if isinstance(active,dict):
                minutes=active.get('windowDurationMins',active.get('window_duration_mins'))
                used=active.get('usedPercent',active.get('used_percent'))
                configured={300:'five_hour',10080:'weekly'}.get(minutes)
                if configured and _number(used) and 0<=used<=100:
                    other='weekly' if configured=='five_hour' else 'five_hour'
                    windows[other]['applicable']=False
    return windows


def _error_kind(message):
    value = str(message).lower()
    if any(token in value for token in ('token_revoked', 'invalid_grant', 'unauthorized', '401', 'refresh token has already', 'authentication token')):
        return 'auth'
    return 'network'


class NativeQuotaProbe:
    def __init__(self, base, profiles, binary, env=None, timeout=25, attempts=2, stop=None, command_prefix=None):
        self.base, self.profiles = Path(base), Path(profiles)
        self.binary = binary
        self.env = dict(os.environ if env is None else env)
        check_environment(self.env)
        self.timeout, self.attempts = timeout, attempts
        self.stop = stop or threading.Event()
        self.command_prefix = list(command_prefix or [])

    def __call__(self, label):
        if not valid_label(label):
            raise ValueError('Invalid quota account')
        if label not in registry(self.base):
            raise QuotaError('not_registered')
        # Existing flock preparation and token-refresh synchronization are shared
        # with the CLI. No model turn is created by account/rateLimits/read.
        with account_lock(self.base, self.profiles, label) as home:
            env = self.env.copy()
            env.update(CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(self.base),
                       CODEX_MULTI_BASE=str(self.base), CODEX_MULTI_PROFILES=str(self.profiles))
            env.pop('CODEX_THREAD_ID', None)
            for attempt in range(self.attempts):
                if self.stop.is_set():
                    raise QuotaError('cancelled')
                try:
                    return normalize_limits(self._request(env))
                except QuotaError as error:
                    if error.kind not in ('network', 'timeout') or attempt+1 == self.attempts:
                        raise
                    if self.stop.wait(1):
                        raise QuotaError('cancelled')

    def _request(self, env):
        child = subprocess.Popen(self.command_prefix + [self.binary, '--no-daemon', '-c', 'cli_auth_credentials_store="file"',
                                  'app-server', '--listen', 'stdio://'],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        buffer = b''
        deadline = time.monotonic()+self.timeout
        def send(value):
            try:
                child.stdin.write((json.dumps(value)+'\n').encode())
                child.stdin.flush()
            except (BrokenPipeError, OSError):
                raise QuotaError('network') from None
        try:
            send({'id':1, 'method':'initialize', 'params':{
                'clientInfo':{'name':'codex-switch-quota-dashboard','version':'1.0.0'},
                'capabilities':{'experimentalApi':True}}})
            while True:
                if self.stop.is_set():
                    raise QuotaError('cancelled')
                if time.monotonic() >= deadline:
                    raise QuotaError('timeout')
                if not selector.select(min(.5, max(.001, deadline-time.monotonic()))):
                    continue
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    raise QuotaError('network')
                buffer += chunk
                if len(buffer) > 2*1024*1024:
                    raise QuotaError('internal')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if event.get('id') not in (1, 2):
                        continue
                    if event.get('error'):
                        raise QuotaError(_error_kind(event['error'].get('message', '')))
                    if event['id'] == 1:
                        send({'method':'initialized','params':{}})
                        send({'id':2,'method':'account/rateLimits/read','params':None})
                    else:
                        result = event.get('result')
                        if not isinstance(result, dict):
                            raise QuotaError('internal')
                        return result
        finally:
            selector.close()
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:pass
                child.wait()
            child.stdin.close()
            child.stdout.close()
