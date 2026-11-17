import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from quota_store import QuotaStore
from dashboard import QuotaHTTPServer


class FakeAccounts:
    def __init__(self, platform, entries=None):
        self.platform=platform
        self.entries=dict(entries or {})
        self.calls=[]

    def registry(self):
        return dict(self.entries)

    def add(self,label,credential):
        if label in self.entries:raise ValueError('此编号已有账号。')
        if credential.get('identity') in self.entries.values():raise ValueError('此身份已存在。')
        self.entries[label]=credential['identity']
        self.calls.append(('add',label))
        return {'platform':self.platform,'label':label}

    def remove(self,label,expected_identity):
        if self.entries.get(label)!=expected_identity:raise ValueError('账号已变更。')
        del self.entries[label]
        self.calls.append(('remove',label))
        return {'platform':self.platform,'label':label}


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        from accounts import AccountManager
        self.codex=FakeAccounts('codex',{'I':'private-identity-one'})
        self.agy=FakeAccounts('agy',{'A':'private-google-identity'})
        self.manager=AccountManager(adapters={'codex':self.codex,'agy':self.agy})

    def test_public_metadata_never_exposes_identities_and_suggests_dynamic_labels(self):
        state=self.manager.public_state()
        self.assertNotIn('private-',json.dumps(state))
        self.assertEqual(state['next_labels']['agy'],'B')
        self.assertTrue(all(row['remove_token'] for row in state['accounts']))
        self.assertIn('codex-switch accounts login codex',state['login_commands']['codex'])

    def test_identity_change_rejects_stale_confirmation(self):
        row=self.manager.public_state()['accounts'][0]
        self.codex.entries['I']='new-private-identity'
        with self.assertRaises(ValueError):
            self.manager.remove('codex','I',row['remove_token'],True)
        self.assertIn('I',self.codex.entries)

    def test_remove_confirmation_cannot_be_replayed_after_same_identity_readd(self):
        row=self.manager.public_state()['accounts'][0]
        self.manager.remove('codex','I',row['remove_token'],True)
        self.manager.add('codex','I',{'identity':'private-identity-one'})
        with self.assertRaises(ValueError):
            self.manager.remove('codex','I',row['remove_token'],True)
        self.assertIn('I',self.codex.entries)

    def test_unconfirmed_and_forged_removals_leave_account_intact(self):
        row=self.manager.public_state()['accounts'][0]
        for token,confirmed in [(row['remove_token'],False),('forged',True)]:
            with self.assertRaises(ValueError):self.manager.remove('codex','I',token,confirmed)
        self.assertEqual(self.codex.calls,[])

    def test_import_auto_allocates_and_rejects_invalid_platform_label_and_credential(self):
        result=self.manager.add('codex','',{'identity':'another-private-identity'})
        self.assertEqual(result['label'],'A')
        for platform,label,value in [('bad','A',{}),('codex','../A',{}),('codex','A',[])]:
            with self.assertRaises(ValueError):self.manager.add(platform,label,value)

    def test_deletion_rejects_queued_quota_and_credit_jobs(self):
        for field in ('refreshing','busy'):
            class Store:
                lock=threading.RLock()
                rows={'I':{field:True}}
            self.manager.store=Store() if field=='refreshing' else None
            self.manager.credits=Store() if field=='busy' else None
            row=self.manager.public_state()['accounts'][0]
            with self.assertRaises(ValueError):
                self.manager.remove('codex','I',row['remove_token'],True)
        self.assertEqual(self.codex.calls,[])


class AccountManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        from accounts import AccountManager
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.adapter=FakeAccounts('codex',{'AA':'private-http-identity'})
        self.manager=AccountManager(adapters={'codex':self.adapter,'agy':FakeAccounts('agy')})
        self.store=QuotaStore(Path(self.tmp.name)/'cache.json',lambda:{},lambda *_:None)
        self.server=QuotaHTTPServer(('127.0.0.1',0),self.store,accounts=self.manager)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.store.close()

    def request(self,path,body=None,headers=None):
        return self.client.open(urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','X-Quota-Request':'local-dashboard',**(headers or {})}),timeout=3)

    def test_import_larger_than_settings_limit_and_metadata_are_safe(self):
        body={'platform':'codex','label':'AB','credential':{'identity':'secret-http-'+('x'*4096)}}
        with self.request('/api/accounts/add',body) as response:
            self.assertEqual(response.status,201)
            self.assertNotIn('secret-http',response.read().decode())
        with self.request('/api/accounts') as response:
            self.assertNotIn('private-http',response.read().decode())

    def test_cross_origin_and_extra_fields_cannot_remove(self):
        with self.request('/api/accounts') as response:row=json.load(response)['accounts'][0]
        body={'platform':'codex','label':'AA','remove_token':row['remove_token'],'confirmed':True}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/accounts/remove',body,{'Origin':'https://elsewhere.example'})
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/accounts/remove',{**body,'unexpected':True})
        self.assertIn('AA',self.adapter.entries)
        with self.request('/api/accounts/remove',body) as response:self.assertEqual(response.status,200)
        self.assertNotIn('AA',self.adapter.entries)

    def test_status_tolerates_account_removed_between_registry_reads(self):
        self.store.registry=lambda:{'AA':'synthetic-identity'}
        class Credits:
            lock=threading.RLock()
            def public_state(self):return {}
            def signal_state(self):return {}
        self.server.credits=Credits()
        with self.request('/api/status') as response:
            self.assertEqual(response.status,200)
            self.assertTrue(json.load(response)['accounts'][-1]['reset_credits']['stale'])


class AccountCLITests(unittest.TestCase):
    def test_run_splits_native_flags_after_separator(self):
        import account_cli
        with patch('accounts.AccountManager') as cls:
            cls.return_value.run.return_value=0
            self.assertEqual(account_cli.main(['run','agy','--label','AA','--','--help']),0)
            cls.return_value.run.assert_called_once_with('agy','AA',['--help'])


class RealAccountHTTPTests(unittest.TestCase):
    def test_both_adapters_import_remove_and_restart_without_touching_default_history(self):
        import base64
        from accounts import AccountManager
        from codex_accounts import CodexAccounts
        from agy_accounts import AgyAccounts
        from agy_quota import AgyProfiles
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'base';base.mkdir(mode=0o700)
            user=root/'user';user.mkdir(mode=0o700);proc=root/'proc';proc.mkdir()
            (base/'auth.json').write_text('keep default');(base/'history.jsonl').write_text('keep history')
            codex=CodexAccounts(base,root/'profiles',proc=proc)
            agy=AgyAccounts(AgyProfiles(root=root/'agy',home=user,proc=proc))
            manager=AccountManager(adapters={'codex':codex,'agy':agy})
            store=QuotaStore(root/'cache.json',codex.registry,lambda *_:None,pool_base=base)
            server=QuotaHTTPServer(('127.0.0.1',0),store,accounts=manager)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(path,body=None):
                with client.open(urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json','X-Quota-Request':'local-dashboard'}),timeout=3) as response:
                    return json.load(response)
            try:
                claims=base64.urlsafe_b64encode(json.dumps({'sub':'synthetic-AA'}).encode()).decode().rstrip('=')
                auth={'auth_mode':'chatgpt','tokens':{'id_token':'header.'+claims+'.signature',
                    'access_token':'synthetic-access','refresh_token':'synthetic-refresh','account_id':'synthetic-AA'}}
                google={'auth_method':'consumer','id_token_email':'aa@example.test',
                    'token':{'access_token':'synthetic-google','refresh_token':'synthetic-google-refresh'}}
                for platform,credential in [('codex',auth),('agy',google)]:
                    result=request('/api/accounts/add',{'platform':platform,'label':'AA','credential':credential})
                    self.assertEqual(result['label'],'AA')
                    self.assertNotIn('synthetic',json.dumps(result))
                self.assertTrue((base/'auth_account_AA.json').exists())
                self.assertTrue(agy.profiles.path('AA').exists())
                manager=AccountManager(adapters={'codex':codex,'agy':agy});server.accounts=manager
                state=request('/api/accounts')
                self.assertEqual(len(state['accounts']),2)
                self.assertNotIn('synthetic',json.dumps(state))
                for row in state['accounts']:
                    request('/api/accounts/remove',{'platform':row['platform'],'label':'AA',
                        'remove_token':row['remove_token'],'confirmed':True})
                self.assertEqual(request('/api/accounts')['accounts'],[])
                self.assertFalse((base/'auth_account_AA.json').exists())
                self.assertFalse((root/'profiles/AA/auth.json').exists())
                self.assertFalse(agy.profiles.path('AA').exists())
                self.assertEqual((base/'auth.json').read_text(),'keep default')
                self.assertEqual((base/'history.jsonl').read_text(),'keep history')
            finally:
                server.shutdown();server.server_close();thread.join(2);store.close()


if __name__=='__main__':unittest.main()
