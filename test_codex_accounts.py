"""Synthetic dynamic account and durable identity-bound credit regression tests."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid


def auth(who):
    payload=base64.urlsafe_b64encode(json.dumps({'sub':who}).encode()).decode().rstrip('=')
    return {'auth_mode':'chatgpt','tokens':{'id_token':'x.'+payload+'.x','access_token':'synthetic-access',
        'refresh_token':'synthetic-refresh','account_id':who}}


class DynamicLabelsTests(unittest.TestCase):
    def test_labels_are_ascii_bounded_and_commands_are_reserved(self):
        from account_labels import valid_label,sort_key,next_label
        for label in ('A','H','I','Z','AA','ZZ','AAA'):self.assertTrue(valid_label(label),label)
        for label in ('','a','Ａ','../A','A/AA','A1','S','SAVE','LOGIN','STATUS','BUZZ','AUTO','QUOTA','WEB','ACCOUNTS','REMOVE','A'*9,None):
            self.assertFalse(valid_label(label),label)
        self.assertEqual(sorted(('AA','I','B','Z'),key=sort_key),['B','I','Z','AA'])
        self.assertEqual(next_label(set('ABCDEFGHIJKLMNOPQR')|set('TUVWXYZ')),'AA')

    def test_discovery_and_paths_accept_more_than_eight_accounts(self):
        from account_labels import codex_labels
        from core import account_path
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            for label in ('I','AA','SAVE','a'):(base/f'auth_account_{label}.json').write_text('{}')
            self.assertEqual(codex_labels(base),tuple('ABCDEFGH')+('I','AA'))
            self.assertEqual(account_path(base,'AA'),base/'auth_account_AA.json')

    def test_new_accounts_require_explicit_pool_membership_and_dynamic_thresholds(self):
        from auto_pool import read_pool,set_member
        from auto_threshold import read_all_thresholds,set_thresholds
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);(base/'auth_account_AA.json').write_text('{}')
            self.assertNotIn('AA',read_pool(base))
            set_member(base,'AA',True)
            self.assertIn('AA',read_pool(base))
            set_thresholds(base,['I','AA'],five_hour=25,weekly=30)
            self.assertEqual(read_all_thresholds(base)['five_hour']['AA'],25)
            self.assertEqual(read_all_thresholds(base)['weekly']['I'],30)


class CodexAccountsTests(unittest.TestCase):
    def setUp(self):
        from codex_accounts import CodexAccounts
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=self.root/'base';self.profiles=self.root/'profiles'
        self.base.mkdir(mode=0o700);self.profiles.mkdir(mode=0o700)
        self.proc=self.root/'proc';self.proc.mkdir()
        self.accounts=CodexAccounts(self.base,self.profiles,proc=self.proc)

    def test_add_i_aa_and_remove_only_managed_credentials(self):
        (self.base/'auth.json').write_text('default keep');(self.base/'sessions').mkdir()
        (self.base/'sessions'/'keep').write_text('session keep')
        self.assertEqual(self.accounts.add('I',auth('i')),{'label':'I','platform':'codex'})
        self.accounts.add('AA',auth('aa'))
        self.assertEqual(self.accounts.labels(),('I','AA'))
        identity=hashlib.sha256(json.dumps(('i','i')).encode()).hexdigest()
        self.assertEqual(self.accounts.registry()['I'],identity)
        self.assertEqual((self.base/'auth_account_I.json').stat().st_mode&0o777,0o600)
        self.assertEqual((self.profiles/'I'/'auth.json').stat().st_mode&0o777,0o600)
        (self.profiles/'I'/'history.jsonl').write_text('profile history keep')
        self.accounts.remove('I',identity)
        self.assertFalse((self.base/'auth_account_I.json').exists())
        self.assertFalse((self.profiles/'I'/'auth.json').exists())
        self.assertEqual((self.base/'auth.json').read_text(),'default keep')
        self.assertEqual((self.base/'sessions'/'keep').read_text(),'session keep')
        self.assertEqual((self.profiles/'I'/'history.jsonl').read_text(),'profile history keep')
        self.assertTrue((self.profiles/'.I.lock').exists())

    def test_add_refuses_duplicate_identity_overwrite_invalid_credentials_and_paths(self):
        self.accounts.add('I',auth('i'))
        for label,value in [('AA',auth('i')),('I',auth('another')),('../bad',auth('bad')),('AA',{'tokens':{}})]:
            with self.subTest(label=label),self.assertRaises(ValueError):self.accounts.add(label,value)
        self.assertEqual(self.accounts.labels(),('I',))

    def test_remove_refuses_changed_identity_and_held_account_or_registration_lock(self):
        self.accounts.add('I',auth('i'));identity=self.accounts.registry()['I']
        with self.assertRaises(ValueError):self.accounts.remove('I','wrong')
        for path in (self.profiles/'.I.lock',self.base/'.account-register.lock'):
            with path.open('r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.accounts.remove('I',identity)
        self.assertTrue((self.base/'auth_account_I.json').exists())

    def test_unreadable_unrelated_system_process_does_not_block_account_import(self):
        entry=self.proc/'517';entry.mkdir()
        (entry/'status').write_text(f'Name:\tsystemd\nUid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n')
        (entry/'exe').symlink_to('/usr/lib/systemd/systemd')
        original=Path.read_bytes
        def read(path,*args,**kwargs):
            if path==entry/'environ':raise PermissionError('fixture unreadable environment')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_bytes',read):
            self.assertEqual(self.accounts.add('I',auth('synthetic-new'))['label'],'I')

    def test_remove_detects_same_profile_app_server_and_login(self):
        self.accounts.add('AA',auth('aa'));identity=self.accounts.registry()['AA']
        for command in ('app-server','login'):
            entry=self.proc/'10';entry.mkdir(exist_ok=True)
            (entry/'status').write_text(f'Uid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n')
            (entry/'cmdline').write_bytes(b'codex\0'+command.encode()+b'\0')
            (entry/'environ').write_bytes(b'CODEX_HOME='+os.fsencode(self.profiles/'AA')+b'\0')
            if not (entry/'exe').is_symlink():(entry/'exe').symlink_to('/fake/codex')
            with self.subTest(command=command),self.assertRaises(ValueError):self.accounts.remove('AA',identity)

    def test_replaced_native_executable_still_blocks_removal(self):
        self.accounts.add('AA',auth('synthetic-old-process'));identity=self.accounts.registry()['AA']
        entry=self.proc/'123';entry.mkdir()
        (entry/'status').write_text(f'Name:\tcodex\nUid:\t{os.getuid()}\t0\t0\t0\n')
        (entry/'exe').symlink_to('/fake/codex (deleted)')
        (entry/'environ').write_bytes(b'CODEX_HOME='+os.fsencode(self.profiles/'AA')+b'\0')
        with self.assertRaises(ValueError):self.accounts.remove('AA',identity)

    def test_mutation_rejects_symlink_and_permissive_credential_paths(self):
        outside=self.root/'outside';outside.mkdir()
        (self.profiles/'I').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.accounts.add('I',auth('i'))
        (self.profiles/'I').unlink();self.accounts.add('I',auth('i'))
        identity=self.accounts.registry()['I'];(self.base/'auth_account_I.json').chmod(0o644)
        with self.assertRaises(ValueError):self.accounts.remove('I',identity)
        self.assertTrue((self.base/'auth_account_I.json').exists())

    def test_remove_refuses_mismatched_profile_without_deleting_either_credential(self):
        from core import atomic_write
        self.accounts.add('I',auth('i'));identity=self.accounts.registry()['I']
        atomic_write(self.profiles/'I'/'auth.json',auth('different'))
        with self.assertRaises(ValueError):self.accounts.remove('I',identity)
        self.assertTrue((self.base/'auth_account_I.json').exists())
        self.assertTrue((self.profiles/'I'/'auth.json').exists())

    def test_failed_login_returns_native_exit_and_saves_no_account(self):
        result=type('Result',(),{'returncode':23})()
        with patch('codex_accounts.shutil.which',return_value='/fake/codex'),patch('codex_accounts.subprocess.run',return_value=result):
            self.assertEqual(self.accounts.login('AA'),23)
        self.assertFalse((self.base/'auth_account_AA.json').exists())
        self.assertFalse((self.profiles/'AA'/'auth.json').exists())

    def test_official_login_is_isolated_and_rechecks_slot_before_commit(self):
        (self.base/'auth.json').write_text('keep')
        def run(command,env):
            self.assertIn('login',command);self.assertNotEqual(env['CODEX_HOME'],str(self.base))
            home=Path(env['CODEX_HOME']);(home/'auth.json').write_text(json.dumps(auth('login-i')))
            (home/'auth.json').chmod(0o600)
            return type('Result',(),{'returncode':0})()
        with patch('codex_accounts.shutil.which',return_value='/fake/codex'),patch('codex_accounts.subprocess.run',side_effect=run):
            self.assertEqual(self.accounts.login('I'),0)
        self.assertEqual((self.base/'auth.json').read_text(),'keep')


class CreditIdentityArchiveTests(unittest.TestCase):
    def test_reused_label_keeps_previous_unknowns_separate_from_new_identity(self):
        from reset_credit_store import ResetCreditStore
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credits.json';identities={'AA':'identity-one'}
            request=str(uuid.uuid4())
            store=ResetCreditStore(path,lambda:dict(identities),lambda:'direct',lambda:None,lambda label:None)
            try:
                with store.lock:
                    store.rows['AA']['redeems'][request]={'status':'unknown','at':100}
                    store._save()
                identities['AA']='identity-two'
                self.assertIsNone(store.public_state()['AA']['pending_request_id'])
                self.assertEqual(store.identities['identity-one']['redeems'][request]['status'],'unknown')
                identities['I']='identity-one'
                self.assertEqual(store.public_state()['I']['pending_request_id'],request)
                with self.assertRaises(ValueError):store.consume('I',True,'unused',str(uuid.uuid4()))
            finally:store.close()
            reopened=ResetCreditStore(path,lambda:dict(identities),lambda:'direct',lambda:None,lambda label:None)
            try:
                self.assertIsNone(reopened.public_state()['AA']['pending_request_id'])
                self.assertEqual(reopened.public_state()['I']['pending_request_id'],request)
            finally:reopened.close()

    def test_removed_or_relabelled_identity_preserves_unknown_request_after_restart(self):
        from reset_credit_store import ResetCreditStore
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credits.json';identities={'A':'identity-one'}
            request=str(uuid.uuid4())
            path.write_text(json.dumps({'version':1,'accounts':{'A':{'identity':'identity-one','snapshot':None,
                'fetched_at':None,'mode':None,'busy':None,'error':None,'revision':None,'attempted_at':None,
                'redeems':{request:{'status':'unknown','at':100}},'last_reset':None}}}))
            store=ResetCreditStore(path,lambda:dict(identities),lambda:'direct',lambda:None,lambda label:None)
            identities.clear();store.public_state();store.close()
            identities['AA']='identity-one'
            other=ResetCreditStore(path,lambda:dict(identities),lambda:'direct',lambda:None,lambda label:None)
            try:
                self.assertEqual(other.public_state()['AA']['pending_request_id'],request)
                with self.assertRaises(ValueError):other.consume('AA',True,'unused',str(uuid.uuid4()))
                self.assertEqual(other.rows['AA']['redeems'][request]['status'],'unknown')
            finally:other.close()


class DynamicFlowTests(unittest.TestCase):
    def test_historical_available_accounts_are_platform_scoped_and_ignore_current_filters(self):
        from usage_store import UsageStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=UsageStore(root/'usage.sqlite3',root,clock=lambda:2000000000)
            try:
                with store._connect() as db:
                    for n,(platform,label,conflict,provider) in enumerate((
                        ('gpt','AA',0,'openai'),('gpt','AA',0,'openai'),('gpt','I',0,'openai'),
                        ('gpt','ZZ',1,'openai'),('gpt','Z',0,'external'),('agy','AA',0,'google'))):
                        ident=f'{n:064x}'
                        db.execute('''INSERT INTO events(id,at,platform,model,provider,session,source,
                            input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,
                            output_tokens,reasoning_output_tokens,total_tokens)
                            VALUES(?,1800000000,?,'synthetic-model',?,'synthetic-session','record',100,0,0,100,10,0,110)''',
                            (ident,platform,provider))
                        db.execute('INSERT INTO usage_accounts VALUES(?,?,?,?)',(ident,label,'a'*64,conflict))
                expected=[{'platform':'gpt','account':'I'},{'platform':'gpt','account':'AA'}]
                selected=store.snapshot('today',model='absent',account='unknown')
                self.assertEqual(selected['summary']['requests'],0)
                self.assertEqual(selected['available_accounts'],expected)
                self.assertEqual(store.snapshot('all',platform='agy')['available_accounts'],[{'platform':'agy','account':'AA'}])
                self.assertEqual(store.snapshot('all',platform='all')['available_accounts'],[{'platform':'agy','account':'AA'},*expected])
            finally:store.close()

    def test_removed_dynamic_accounts_ignore_stale_activity_and_refresh_results(self):
        from quota_store import QuotaStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identities={'AA':'a'*64}
            store=QuotaStore(root/'quota.json',lambda:dict(identities),lambda label:None,
                activity=lambda:{'AA':1},pool_base=root)
            try:
                identities.clear()
                self.assertEqual(store.refresh('AA')['skipped'],['AA'])
                store._scan_activity(100)
                self.assertEqual(store.active_counts,{})
                store._fetch('AA','a'*64,store.route_generation,store.network_mode,lambda label:None)
                with self.assertRaises(ValueError):store.set_subscription_date('AA','2027-01-01','old-token')
            finally:store.close()

    def test_quota_store_and_capacity_publish_dynamic_registry_and_reject_identity_reuse(self):
        from quota_store import QuotaStore
        from quota_capacity import QuotaCapacity
        from usage_store import UsageStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identities={'I':'a'*64,'AA':'b'*64}
            store=QuotaStore(root/'quota.json',lambda:dict(identities),lambda label:None,pool_base=root)
            usage=UsageStore(root/'usage.sqlite3',root,clock=lambda:2000000100)
            try:
                dynamic=[r for r in store.public_state()['accounts'] if r['registered']]
                self.assertEqual([r['label'] for r in dynamic],['I','AA'])
                self.assertTrue(all(not r['auto_enabled'] for r in dynamic))
                store.set_auto_member('AA',True);store.set_auto_threshold('AA',25)
                store.set_auto_thresholds(['I','AA'],weekly=30)
                capacity=QuotaCapacity(usage,lambda:dict(identities),root/'credits.json',clock=lambda:2000000100)
                snapshot={'five_hour':{'remaining':50,'reset_at':2000010000},'weekly':{'remaining':80,'reset_at':2000020000}}
                capacity.record('AA','b'*64,snapshot,2000000000,1999999999)
                self.assertEqual([r['label'] for r in capacity.snapshot()['accounts']],['I','AA'])
                identities['AA']='c'*64
                self.assertIsNone(next(r for r in store.public_state()['accounts'] if r['label']=='AA')['snapshot'])
            finally:store.close();usage.close()

    def test_removed_dynamic_label_remains_a_valid_historical_usage_filter(self):
        from usage_store import UsageStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=UsageStore(root/'usage.sqlite3',root)
            try:
                store.attribute_accounts([{'id':'1'*64,'account':'AA','identity':'2'*64}])
                self.assertEqual(store.snapshot('all',account='AA')['summary']['requests'],0)
                with self.assertRaises(ValueError):store.snapshot('all',account='../AA')
            finally:store.close()

    def test_dynamic_live_writer_keeps_same_attribution_guards(self):
        from usage_accounts import UsageAccountObserver
        from usage_store import UsageStore
        from core import atomic_write
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'base';profiles=root/'profiles';proc=root/'proc'
            (base/'sessions').mkdir(parents=True);(profiles/'AA').mkdir(parents=True);proc.mkdir()
            atomic_write(base/'auth_account_AA.json',auth('AA'));atomic_write(profiles/'AA'/'auth.json',auth('AA'))
            path=base/'sessions/rollout.jsonl'
            path.write_text(json.dumps({'type':'session_meta','timestamp':'2026-11-09T00:00:00Z',
                'payload':{'id':'synthetic-thread','model_provider':'openai'}})+'\n')
            binary=root/'bin/codex';binary.parent.mkdir();binary.write_text('synthetic binary')
            folder=proc/'101';folder.mkdir();(folder/'exe').symlink_to(binary)
            (folder/'status').write_text(f'Uid:\t{os.getuid()}\t0\t0\t0\n')
            (folder/'stat').write_text('101 (codex) S '+'0 '*18+'123 0\n')
            (folder/'cmdline').write_bytes(os.fsencode(binary)+b'\0--no-daemon\0')
            (folder/'environ').write_bytes(b'CODEX_HOME='+os.fsencode(profiles/'AA')+b'\0')
            (folder/'fd').mkdir();(folder/'fdinfo').mkdir();(folder/'fd/42').symlink_to(path)
            (folder/'fdinfo/42').write_text('flags:\t0100002\n')
            store=UsageStore(root/'usage.sqlite3',base,clock=lambda:1794182406)
            now=[1794182400]
            observer=UsageAccountObserver(store,base,profiles,proc=proc,clock=lambda:now[0],monotonic=lambda:now[0])
            try:
                observer.poll()
                with path.open('a') as stream:
                    stream.write(json.dumps({'type':'turn_context','timestamp':'2026-11-09T00:00:01Z',
                        'payload':{'model':'synthetic-model'}})+'\n')
                    stream.write(json.dumps({'type':'token_usage_record','timestamp':'2026-11-09T00:00:02Z',
                        'payload':{'thread_id':'synthetic-thread','response_id':'synthetic-response',
                        'usage':{'input_tokens':100,'cached_input_tokens':0,'cache_write_input_tokens':0,
                            'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':110}}})+'\n')
                now[0]+=1;observer.poll();store.scan()
                self.assertEqual(store.snapshot('all')['rows'][0]['account'],'AA')
                self.assertEqual(store.snapshot('all')['rows'][0]['account_source'],'process')
            finally:observer.close();store.close()

    def test_dynamic_auto_probe_threshold_and_manual_launch(self):
        import subprocess
        import sys
        from core import atomic_write
        from auto_pool import set_member
        from auto_threshold import set_threshold
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=root/'base';base.mkdir();binary=root/'bin/codex';binary.parent.mkdir()
            (base/'sessions').mkdir();(base/'config.toml').write_text('model="synthetic-model"\n')
            atomic_write(base/'auth.json',auth('default'))
            for label in ('B','I','AA'):atomic_write(base/f'auth_account_{label}.json',auth(label))
            records=root/'records.jsonl'
            binary.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if '--help' in args:print('--no-daemon');sys.exit(0)
label=Path(os.environ['CODEX_HOME']).name
def record(kind):
 with open(os.environ['FAKE_RECORDS'],'a') as stream:stream.write(json.dumps(dict(kind=kind,label=label,args=args))+'\\n')
if 'app-server' in args:
 for line in sys.stdin:
  message=json.loads(line)
  if message.get('method')=='initialize':print(json.dumps({'id':message['id'],'result':{}}),flush=True)
  elif message.get('method')=='account/rateLimits/read':
   record('probe');used=95 if label=='AA' and os.environ.get('FAKE_LOW')=='1' else 10
   print(json.dumps({'id':message['id'],'result':{'rateLimits':{'primary':{'usedPercent':used,'windowDurationMins':300,'resetsAt':2000010000},'secondary':{'usedPercent':20,'windowDurationMins':10080,'resetsAt':2000020000}}}}),flush=True)
else:record('run');print('synthetic response')
''')
            binary.chmod(0o700)
            env={'PATH':str(binary.parent)+os.pathsep+os.environ['PATH'],'HOME':str(root),'PYTHONDONTWRITEBYTECODE':'1',
                'CODEX_MULTI_BASE':str(base),'CODEX_MULTI_PROFILES':str(root/'profiles'),'FAKE_RECORDS':str(records)}
            def run(*args,**extra):
                result=subprocess.run([sys.executable,str(Path(__file__).with_name('core.py')),*args],
                    env={**env,**extra},cwd=root,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                entries=[json.loads(line) for line in records.read_text().splitlines()]
                records.unlink();return entries
            original=(base/'auth.json').read_bytes()
            for label in ('I','AA'):
                entries=run(label.lower(),'synthetic-session')
                self.assertEqual([r['label'] for r in entries], [label])
                self.assertIn('synthetic-session',entries[0]['args'])
            self.assertEqual({r['label'] for r in run('quota','AA')},{'AA'})
            set_member(base,'AA',True);set_threshold(base,'AA',10)
            entries=run('auto','--new','--prefer','AA')
            self.assertEqual([r['label'] for r in entries if r['kind']=='run'],['AA'])
            entries=run('auto','--new','--prefer','AA',FAKE_LOW='1')
            self.assertEqual([r['label'] for r in entries if r['kind']=='run'],['B'])
            self.assertEqual((base/'auth.json').read_bytes(),original)

    def test_vendor_dynamic_threshold_patch_exists_and_rejects_unexpected_input(self):
        import subprocess
        script=Path(__file__).with_name('patch_vendor_dynamic_accounts.py')
        self.assertTrue(script.exists(),'第14步动态账号补丁尚未创建')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cx';path.write_text('unexpected input')
            result=subprocess.run(['python3',str(script),str(path)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(path.read_text(),'unexpected input')
