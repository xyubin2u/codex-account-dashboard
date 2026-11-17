#!/usr/bin/env python3
"""Loopback-only HTTP interface to the existing Codex subscription accounts."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import urllib.parse
import urllib.request

from core import check_environment
from account_labels import valid_label
from quota_data import registry
from quota_network import RoutedQuotaProbe, configure_proxy
from quota_store import QuotaStore
from activity import CodexActivity
from reset_credits import RoutedCreditClient
from reset_credit_store import ResetCreditStore
from usage_store import UsageStore
from usage_accounts import UsageAccountObserver
from quota_capacity import QuotaCapacity
from agy_quota import AgyProfiles, AgyQuotaClient
from agy_store import AgyStore
from agy_capacity import AgyQuotaCapacity, PlatformCapacity
from agy_usage_indexer import AgyUsageIndexer

HERE = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html','text/html; charset=utf-8'),
          '/style.css': ('style.css','text/css; charset=utf-8'),
          '/compact.css': ('compact.css','text/css; charset=utf-8'),
          '/navigation.css': ('navigation.css','text/css; charset=utf-8'),
          '/app.js': ('app.js','text/javascript; charset=utf-8'),
          '/credits.js': ('credits.js','text/javascript; charset=utf-8'),
          '/agy.js': ('agy.js','text/javascript; charset=utf-8'),
          '/agy.css': ('agy.css','text/css; charset=utf-8'),
          '/accounts.js': ('accounts.js','text/javascript; charset=utf-8'),
          '/accounts.css': ('accounts.css','text/css; charset=utf-8'),
          '/usage': ('usage.html','text/html; charset=utf-8'),
          '/usage/': ('usage.html','text/html; charset=utf-8'),
          '/usage.js': ('usage.js','text/javascript; charset=utf-8'),
          '/usage.css': ('usage.css','text/css; charset=utf-8'),
          '/capacity': ('capacity.html','text/html; charset=utf-8'),
          '/capacity/': ('capacity.html','text/html; charset=utf-8'),
          '/capacity.js': ('capacity.js','text/javascript; charset=utf-8'),
          '/capacity.css': ('capacity.css','text/css; charset=utf-8'),
          '/favicon.svg': ('favicon.svg','image/svg+xml'),
          '/favicon.png': ('favicon.png','image/png'),
          '/favicon.ico': ('favicon.ico','image/x-icon')}


class QuotaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store, credits=None, usage=None, capacity=None, agy=None, accounts=None):
        self.store = store
        self.credits = credits
        self.usage = usage
        self.capacity = capacity
        self.agy = agy
        self.accounts = accounts
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'CodexQuota/1.0'
    def log_message(self, *_):
        pass

    def _trusted(self, mutation=False):
        hosts = {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        host = self.headers.get('Host','')
        if host not in hosts:
            return False
        if mutation:
            if self.headers.get('X-Quota-Request') != 'local-dashboard':
                return False
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+host:
                return False
        return True

    def _respond(self, code, body, content_type='application/json; charset=utf-8'):
        data = json.dumps(body,ensure_ascii=False,allow_nan=False).encode() if isinstance(body,dict) else body
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):pass

    def do_GET(self):
        if not self._trusted():
            return self._respond(403,{'error':'仅允许本机访问。'})
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/health':
            return self._respond(200,{'app':'codex-quota-dashboard','version':1})
        if path == '/api/accounts':
            if self.server.accounts is None:return self._respond(503,{'error':'账号管理暂不可用。'})
            try:return self._respond(200,self.server.accounts.public_state())
            except (ValueError,OSError):return self._respond(503,{'error':'账号列表暂不可读，请核对本地文件。'})
        if path == '/api/agy/status':
            if self.server.agy is None:
                return self._respond(503,{'error':'AGY 额度服务暂不可用。'})
            return self._respond(200,self.server.agy.public_state())
        if path == '/api/activity':
            signal_state=self.server.store.activity_state()
            if self.server.credits is not None:
                signal_state.update(self.server.credits.signal_state())
            return self._respond(200,signal_state)
        if path == '/api/status':
            # Match the account mutation lock order. A dynamic label may also
            # disappear in another process between the two registry reads.
            with self.server.store.lock:
                state=self.server.store.public_state()
                if self.server.credits is not None:
                    with self.server.credits.lock:
                        credits=self.server.credits.public_state()
                        for row in state['accounts']:
                            row['reset_credits']=credits.get(row['label'],{'stale':True,'busy':None})
                        state.update(self.server.credits.signal_state())
            return self._respond(200,state)
        if path == '/api/capacity':
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,keep_blank_values=True)
            if set(query)-{'tz','platform'} or any(len(v)!=1 for v in query.values()):
                return self._respond(400,{'error':'额度测算不接受额外参数。'})
            if self.server.capacity is None:
                return self._respond(503,{'error':'额度测算服务暂不可用。'})
            try:
                options={'timezone':query.get('tz',['Asia/Tokyo'])[0]}
                if 'platform' in query:options['platform']=query['platform'][0]
                return self._respond(200,self.server.capacity.snapshot(**options))
            except ValueError:return self._respond(400,{'error':'时区无效。'})
            except Exception:return self._respond(503,{'error':'额度测算暂时无法读取，原额度功能不受影响。'})
        if path == '/api/usage':
            if self.server.usage is None:
                return self._respond(503,{'error':'用量统计服务未启动。'})
            try:
                query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,keep_blank_values=True)
                if set(query)-{'range','model','tz','page','limit','account','start','end','platform'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError()
                get=lambda key,default:query.get(key,[default])[0]
                result=self.server.usage.snapshot(range_name=get('range','7d'),model=get('model','all'),
                    timezone=get('tz','UTC'),page=int(get('page','1')),limit=int(get('limit','30')),
                    account=get('account','all'),start_date=get('start',None),end_date=get('end',None),platform=get('platform','gpt'))
                return self._respond(200,result)
            except ValueError:
                return self._respond(400,{'error':'用量筛选无效，请检查时间、模型、时区或分页。'})
        if path in ASSETS:
            filename,content_type=ASSETS[path]
            return self._respond(200,(HERE/'web'/filename).read_bytes(),content_type)
        return self._respond(404,{'error':'页面不存在。'})

    def do_POST(self):
        if not self._trusted(mutation=True):
            return self._respond(403,{'error':'仅允许本页发起刷新。'})
        try:
            path = urllib.parse.urlsplit(self.path).path
            length = int(self.headers.get('Content-Length','0'))
            maximum=256*1024 if path=='/api/accounts/add' else 1024
            if not 0 < length <= maximum or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                raise ValueError('请求格式不正确。')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise ValueError('请求格式不正确。')
            path = urllib.parse.urlsplit(self.path).path
            if path in ('/api/accounts/add','/api/accounts/remove'):
                if self.server.accounts is None:return self._respond(503,{'error':'账号管理暂不可用。'})
                try:
                    if path.endswith('/add'):
                        if set(body)!={'platform','label','credential'}:raise ValueError('新增账号请求格式无效。')
                        result=self.server.accounts.add(body['platform'],body['label'],body['credential'])
                        return self._respond(201,result)
                    if set(body)!={'platform','label','remove_token','confirmed'}:raise ValueError('移除账号请求格式无效。')
                    result=self.server.accounts.remove(body['platform'],body['label'],body['remove_token'],body['confirmed'])
                    return self._respond(200,result)
                except ValueError as error:return self._respond(409,{'error':str(error)})
                except OSError:return self._respond(409,{'error':'本地账号文件操作失败，请核对目录权限。'})
            if path == '/api/agy/refresh':
                if self.server.agy is None:
                    return self._respond(503,{'error':'AGY 额度服务暂不可用。'})
                if set(body)!={'account'} or not isinstance(body['account'],str):
                    raise ValueError('AGY 刷新请求无效。')
                return self._respond(202,self.server.agy.refresh(body['account']))
            if path in ('/api/usage/scan','/api/usage/pricing'):
                if self.server.usage is None:
                    return self._respond(503,{'error':'用量统计服务未启动。'})
                if path.endswith('/scan'):
                    if body:
                        raise ValueError('同步请求不接受额外参数。')
                    return self._respond(202,self.server.usage.request_scan())
                if set(body)=={'model','clear'} and body['clear'] is True:
                    self.server.usage.set_pricing(body['model'],None)
                elif set(body)=={'model','input','cached_input','cache_write','output'}:
                    self.server.usage.set_pricing(body['model'],{k:v for k,v in body.items() if k!='model'})
                else:
                    raise ValueError('单价请求格式无效。')
                return self._respond(200,{'ok':True})
            if path == '/api/subscription-date':
                if set(body)!={'account','date','edit_token'}:
                    return self._respond(400,{'error':'日期请求格式无效。'})
                try:
                    result=self.server.store.set_subscription_date(body['account'],body['date'],body['edit_token'])
                    return self._respond(200,{'ok':True,**result})
                except ValueError as exc:
                    return self._respond(409,{'error':str(exc)})
            if path in ('/api/credits/query','/api/credits/consume','/api/credits/resolve'):
                if self.server.credits is None:
                    return self._respond(503,{'error':'重置券服务未启动。'})
                try:
                    if path.endswith('/query') and set(body)=={'account'}:
                        result=self.server.credits.query(body['account'])
                    elif path.endswith('/consume') and set(body)=={'account','confirmed','revision','request_id'}:
                        result=self.server.credits.consume(body['account'],body['confirmed'],body['revision'],body['request_id'])
                    elif path.endswith('/resolve') and set(body)=={'account','confirmed','request_id'}:
                        result=self.server.credits.resolve_unknown(body['account'],body['confirmed'],body['request_id'])
                    else:
                        raise ValueError('重置券请求格式无效。')
                    return self._respond(202,result)
                except ValueError as exc:
                    return self._respond(409,{'error':str(exc)})
            if path == '/api/refresh':
                result = self.server.store.refresh(body.get('account','all'))
                if self.server.agy is not None and body.get('account','all')=='all':
                    result['agy']=self.server.agy.refresh('all')
                return self._respond(202,result)
            if path == '/api/auto-pool':
                if set(body) != {'account','enabled'} or not valid_label(body['account']) or type(body['enabled']) is not bool:
                    raise ValueError('账户或开关无效。')
                enabled = self.server.store.set_auto_member(body['account'],body['enabled'])
                return self._respond(200,{'ok':True,'enabled':enabled})
            if path == '/api/auto-threshold':
                if set(body) not in ({'account','threshold'},{'account','threshold','window'}):
                    raise ValueError('账户或阈值无效。')
                threshold=self.server.store.set_auto_threshold(body['account'],body['threshold'],body.get('window','five_hour'))
                return self._respond(200,{'ok':True,'threshold':threshold})
            if path == '/api/auto-threshold-batch':
                if 'accounts' not in body or not set(body).issubset({'accounts','five_hour','weekly'}):
                    raise ValueError('账户或阈值无效。')
                self.server.store.set_auto_thresholds(body['accounts'],body.get('five_hour'),body.get('weekly'))
                return self._respond(200,{'ok':True})
            if path == '/api/settings':
                if set(body) == {'network_mode'}:
                    result = self.server.store.set_network_mode(body['network_mode'])
                    if self.server.agy is not None:
                        self.server.agy.configure(self.server.store.interval,self.server.store.network_mode)
                        self.server.agy.refresh('all')
                    return self._respond(200,{'ok':True,**result})
                if set(body) == {'interval'}:
                    self.server.store.set_interval(body['interval'])
                    if self.server.agy is not None:
                        self.server.agy.configure(self.server.store.interval,self.server.store.network_mode)
                    return self._respond(200,{'ok':True})
                raise ValueError('未知设置。')
            return self._respond(404,{'error':'页面不存在。'})
        except (ValueError, TypeError):
            return self._respond(400,{'error':'请求无效；请检查账户、刷新间隔或查询路线。'})


def main():
    parser=argparse.ArgumentParser(description='Codex A–H 本机额度看板')
    parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--no-initial-refresh',action='store_true')
    args=parser.parse_args()
    env=os.environ.copy()
    check_environment(env)
    binary=env.get('CODEX_QUOTA_REAL_CODEX') or shutil.which('codex',path=env.get('PATH'))
    if not binary:
        raise SystemExit('找不到 Codex CLI。')
    base=Path(env.get('CODEX_MULTI_BASE',str(Path.home()/'.codex'))).resolve()
    profiles=Path(env.get('CODEX_MULTI_PROFILES',str(HERE/'accounts'))).resolve()
    state=Path(env.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'codex-switch/quota-dashboard.json'
    stop=threading.Event()
    probe=RoutedQuotaProbe(base,profiles,binary,env=env)
    activity=CodexActivity(base,profiles)
    store=QuotaStore(state,activity.registered,probe,pool_base=base,activity=activity)
    agy_profiles=AgyProfiles()
    agy_client=AgyQuotaClient(agy_profiles)
    agy=AgyStore(state.with_name('agy-quota.json'),agy_profiles.registry,agy_client.query,
        activity=agy_profiles.active,interval=store.interval,mode=store.network_mode)
    credits=ResetCreditStore(state.with_name('reset-credits.json'),activity.registered,
        lambda:store.network_mode,RoutedCreditClient(base,profiles,probe).capture,
        lambda label:store.refresh(label,force=True))
    usage=UsageStore(state.with_name('usage.sqlite3'),base)
    usage.external_indexer=AgyUsageIndexer(usage,profiles=agy_profiles)
    usage.account_observer=UsageAccountObserver(usage,base,profiles)
    try:
        codex_capacity=QuotaCapacity(usage,activity.registered,state.with_name('reset-credits.json'))
        agy_capacity=AgyQuotaCapacity(usage,agy_profiles.registry,current_state=agy.public_state)
        capacity=PlatformCapacity(codex_capacity,agy_capacity,usage)
        store.snapshot_observer=codex_capacity.safe_record
        agy.snapshot_observer=agy_capacity.safe_record
    except Exception:
        capacity=None
    try:
        from accounts import AccountManager
        accounts=AccountManager(base,profiles,store=store,credits=credits,agy_store=agy)
        server=QuotaHTTPServer(('127.0.0.1',args.port),store,credits,usage=usage,capacity=capacity,agy=agy,accounts=accounts)
    except OSError as error:
        usage.close();credits.close();store.close();agy.close()
        client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with client.open(f'http://127.0.0.1:{args.port}/api/health',timeout=2) as response:
                if json.load(response).get('app')=='codex-quota-dashboard':
                    print(f'额度看板已在运行：http://localhost:{args.port}')
                    return 0
        except Exception:
            pass
        raise SystemExit(f'看板端口不可用：{error}') from None
    def terminate(_signum,_frame):
        stop.set();probe.close()
    for signum in (signal.SIGINT,signal.SIGTERM):signal.signal(signum,terminate)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    usage.account_observer.start()
    usage.external_indexer.start()
    print(f'Codex 额度看板：http://localhost:{server.server_port}',flush=True)
    try:
        if args.no_initial_refresh:
            store.defer_initial_refresh();agy.defer_initial_refresh()
        else:
            store.tick();agy.tick()
        while not stop.wait(1):
            store.tick();agy.configure(store.interval,store.network_mode);agy.tick()
    finally:
        stop.set();probe.close();server.shutdown();server.server_close();usage.close();credits.close();store.close();agy.close();worker.join(timeout=2)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
