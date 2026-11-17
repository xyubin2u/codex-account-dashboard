"""Synthetic account mutations; never use the native HOME or request OAuth."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agy_quota import AgyProfiles, AgyError,AgyQuotaClient
from agy_store import AgyStore


def credential(email):
    return {'auth_method':'consumer','id_token_email':email,
            'token':{'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expiry':'2030-01-01T00:00:00Z'}}


class AgyAccountsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_accounts'),'dynamic AGY account adapter is missing')
        from agy_accounts import AgyAccounts
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.root=self.home/'profiles';self.proc=self.home/'proc';self.proc.mkdir()
        self.profiles=AgyProfiles(self.root,self.home,proc=self.proc,binary=self.home/'.local/bin/agy')
        self.accounts=AgyAccounts(self.profiles)

    def native(self,email='native@example.test'):
        path=self.profiles.path('A');path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        path.write_text(json.dumps(credential(email)));path.chmod(0o600);return path

    def test_dynamic_add_registry_and_cold_store_keep_i_and_aa(self):
        for label in ('I','AA'):self.assertEqual(self.accounts.add(label,credential(label.lower()+'@example.test')),{'platform':'agy','label':label})
        cold=AgyProfiles(self.root,self.home,proc=self.proc)
        self.assertEqual(set(cold.registry()),{'I','AA'})
        self.assertEqual(self.accounts.labels(),['I','AA'])
        self.assertEqual(self.accounts.registry()['AA'],hashlib.sha256(b'aa@example.test').hexdigest())
        self.assertNotIn('synthetic',json.dumps(self.accounts.registry()))
        store=AgyStore(self.home/'cache.json',cold.registry,lambda *args:None,interval=0)
        try:self.assertEqual([r['label'] for r in store.public_state()['accounts'] if r['registered']],['I','AA'])
        finally:store.close()

    def test_duplicate_identity_rejects_without_overwriting(self):
        self.accounts.add('B',credential('person@example.test'))
        before=self.profiles.path('B').read_bytes()
        with self.assertRaises(ValueError):self.accounts.add('I',credential(' PERSON@example.test '))
        with self.assertRaises(ValueError):self.accounts.add('B',credential('different@example.test'))
        self.assertEqual(self.profiles.path('B').read_bytes(),before)
        self.assertFalse(self.profiles.path('I').exists())

    def test_gmail_alias_duplicate_is_rejected(self):
        self.accounts.add('B',credential('first.last@gmail.com'))
        with self.assertRaises(ValueError):self.accounts.add('C',credential('firstlast+tag@googlemail.com'))

    def test_label_and_credential_validation_do_not_create_paths(self):
        for label in ('../A','aa','AUTO','A/B','Ａ',''):
            with self.subTest(label=label),self.assertRaises(ValueError):self.accounts.add(label,credential('x@example.test'))
        for data in ({}, {'auth_method':'consumer','id_token_email':'x@example.test','token':{}}, credential('bad\n@example.test')):
            with self.assertRaises(ValueError):self.accounts.add('I',data)
        self.assertFalse(self.profiles.path('I').exists())

    def test_remove_only_credentials_preserves_history_and_config(self):
        self.accounts.add('AA',credential('aa@example.test'));path=self.profiles.path('AA')
        history=path.parent/'conversations';history.mkdir();(history/'keep.db').write_text('history')
        config=path.parents[1]/'settings.json';config.write_text('configuration')
        identity=self.accounts.registry()['AA']
        with self.assertRaises(ValueError):self.accounts.remove('AA','0'*64)
        self.assertTrue(path.exists())
        self.assertEqual(self.accounts.remove('AA',identity),{'platform':'agy','label':'AA'})
        self.assertFalse(path.exists());self.assertEqual((history/'keep.db').read_text(),'history')
        self.assertEqual(config.read_text(),'configuration');self.assertNotIn('AA',self.accounts.registry())

    def test_native_a_remove_is_persistent_tombstone_and_matching_readd_only(self):
        path=self.native();before=path.read_bytes();identity=self.accounts.registry()['A']
        self.accounts.remove('A',identity)
        self.assertEqual(path.read_bytes(),before)
        self.assertNotIn('A',AgyProfiles(self.root,self.home,proc=self.proc).registry())
        with self.assertRaises(ValueError):self.accounts.add('A',credential('other@example.test'))
        self.assertEqual(path.read_bytes(),before)
        self.accounts.add('A',credential('native@example.test'))
        self.assertIn('A',AgyProfiles(self.root,self.home,proc=self.proc).registry())
        self.assertEqual(path.read_bytes(),before)

    def test_symlink_ancestor_and_login_file_are_rejected(self):
        outside=self.home/'outside';outside.mkdir()
        self.root.mkdir();(self.root/'accounts').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.accounts.add('I',credential('i@example.test'))
        self.assertEqual(list(outside.iterdir()),[])
        (self.root/'accounts').unlink();self.accounts.add('B',credential('b@example.test'))
        path=self.profiles.path('B');saved=path.read_bytes();path.unlink();target=outside/'token';target.write_bytes(saved);target.chmod(0o600);path.symlink_to(target)
        self.assertNotIn('B',self.accounts.registry())
        with self.assertRaises(ValueError):self.accounts.remove('B',hashlib.sha256(b'b@example.test').hexdigest())
        self.assertEqual(target.read_bytes(),saved)

    def test_unrelated_protected_system_process_does_not_block_import(self):
        proc=self.profiles.proc/'517';proc.mkdir()
        (proc/'status').write_text('Name:\tsystemd\n')
        original=os.readlink
        def readlink(path,*args,**kwargs):
            if Path(path)==proc/'exe':raise PermissionError('fixture protected helper')
            return original(path,*args,**kwargs)
        with patch('os.readlink',readlink):
            self.assertEqual(self.accounts.add('I',credential('fresh@example.test'))['label'],'I')

    def test_global_and_account_locks_reject_busy_mutation(self):
        self.accounts.add('B',credential('b@example.test'));identity=self.accounts.registry()['B']
        for name in ('accounts-mutation.lock','B.lock'):
            with self.subTest(lock=name):
                fd=os.open(self.root/'runtime'/name,os.O_CREAT|os.O_RDWR,0o600)
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.assertRaises(ValueError):self.accounts.remove('B',identity)
                finally:os.close(fd)
                self.assertTrue(self.profiles.path('B').exists())

    def test_native_models_process_blocks_removal_by_inode(self):
        token=self.native();pid=self.proc/'123';pid.mkdir();binary=self.home/'.local/bin/agy';binary.parent.mkdir(parents=True);binary.touch()
        (pid/'exe').symlink_to(binary);(pid/'cmdline').write_bytes(b'agy\0models\0')
        view=pid/'root'/str(token).lstrip('/');view.parent.mkdir(parents=True);os.link(token,view)
        self.assertEqual(self.profiles.active(),{})
        with self.assertRaises(ValueError):self.accounts.remove('A',self.accounts.registry()['A'])
        self.assertTrue(token.exists())

    def test_login_uses_isolated_namespace_and_validates_without_overwriting(self):
        seen=[]
        def call(command,**kwargs):
            seen.append((command,kwargs));path=self.profiles.path('AA');path.write_text(json.dumps(credential('aa@example.test')));path.chmod(0o600);return 0
        with patch('agy_accounts.subprocess.call',side_effect=call):self.assertEqual(self.accounts.login('AA'),0)
        command,options=seen[0]
        self.assertEqual(command[0],'/usr/bin/unshare');self.assertIn('--mount',command)
        self.assertEqual(command[-3:],['acp','--login','--disable-telemetry'])
        self.assertTrue(any('agy_runtime/namespace_exec.py' in arg for arg in command))
        self.assertIn('AA',self.accounts.registry());self.assertNotIn('stdin',options)
        with patch('agy_accounts.subprocess.call') as process:
            with self.assertRaises(ValueError):self.accounts.login('AA')
            process.assert_not_called()
        with self.assertRaises(ValueError):self.accounts.login('A')

    def test_failed_login_keeps_official_result_unregistered(self):
        def call(command,**kwargs):
            path=self.profiles.path('I');path.write_text(json.dumps(credential('i@example.test')));path.chmod(0o600)
            return 1
        with patch('agy_accounts.subprocess.call',side_effect=call):self.assertEqual(self.accounts.login('I'),1)
        self.assertTrue(self.profiles.path('I').exists())
        self.assertNotIn('I',AgyProfiles(self.root,self.home,proc=self.proc).registry())

    def test_import_writes_private_files_and_directories(self):
        self.accounts.add('AA',credential('aa@example.test'));path=self.profiles.path('AA')
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        for directory in (path.parent,path.parents[1],self.root/'accounts/AA',self.root/'accounts',self.root/'runtime'):
            self.assertEqual(directory.stat().st_mode&0o777,0o700)

    def test_unrelated_process_without_exe_does_not_block_mutation(self):
        (self.proc/'345').mkdir()
        self.assertEqual(self.accounts.add('I',credential('i@example.test')),{'platform':'agy','label':'I'})

    def test_dynamic_active_account_uses_native_mount_inode(self):
        self.accounts.add('AA',credential('aa@example.test'))
        token=self.profiles.path('AA');pid=self.proc/'123';pid.mkdir()
        binary=self.home/'.local/bin/agy';binary.parent.mkdir(parents=True);binary.touch()
        (pid/'exe').symlink_to(binary);(pid/'cmdline').write_bytes(b'agy\0--continue\0')
        view=pid/'root'/str(self.profiles.path('A')).lstrip('/');view.parent.mkdir(parents=True);os.link(token,view)
        self.assertEqual(self.profiles.active(),{'AA':1})
        with self.assertRaises(ValueError):self.accounts.remove('AA',self.accounts.registry()['AA'])

    def test_store_refresh_and_remove_dynamic_account_discards_inflight_result(self):
        import threading
        self.accounts.add('I',credential('i@example.test'));identity=self.accounts.registry()['I'];gate=threading.Event()
        def query(*args):
            gate.wait(2)
            return {'identity':identity,'plan_type':'Free','groups':[]}
        store=AgyStore(self.home/'cache.json',self.profiles.registry,query,interval=0)
        try:
            self.assertEqual(store.refresh('I')['accepted'],['I'])
            self.accounts.remove('I',identity);gate.set();store.wait()
            self.assertNotIn('I',[r['label'] for r in store.public_state()['accounts']])
            self.assertEqual(store.refresh('I')['accepted'],[])
        finally:gate.set();store.close()

    def test_shared_history_is_attached_for_new_profile_without_global_migration(self):
        shared=self.root/'shared/history';shared.mkdir(parents=True)
        (self.root/'shared/history-manifest.json').write_text('{}');(shared/'conversations').mkdir()
        self.accounts.add('I',credential('i@example.test'))
        self.assertEqual((self.profiles.path('I').parent/'conversations').resolve(),(shared/'conversations').resolve())

    def test_dynamic_run_is_isolated_and_holds_shared_account_lock(self):
        self.accounts.add('AA',credential('aa@example.test'));seen=[]
        def call(command,**kwargs):
            seen.append((command,kwargs));fd=os.open(self.root/'runtime/AA.lock',os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.accounts.remove('AA',self.accounts.registry()['AA'])
            finally:os.close(fd)
            return 0
        with patch.dict(os.environ,{'GEMINI_API_KEY':'synthetic-key','GOOGLE_API_KEY':'synthetic-key'}),patch('agy_accounts.subprocess.call',side_effect=call):
            self.assertEqual(self.accounts.run('AA',['--continue']),0)
        command,options=seen[0]
        self.assertEqual(command[0],'/usr/bin/unshare')
        self.assertEqual(command[-3:],[self.profiles.binary,'--dangerously-skip-permissions','--continue'])
        self.assertNotIn('GEMINI_API_KEY',options['env']);self.assertNotIn('GOOGLE_API_KEY',options['env'])
        self.assertEqual(options['env']['HOME'],str(self.home))
        self.assertTrue(options['pass_fds'])

    def test_native_run_preserves_identity_and_respects_safe_flag(self):
        path=self.native();before=path.read_bytes()
        with patch('agy_accounts.subprocess.call',return_value=0) as call:
            self.assertEqual(self.accounts.run('A',['--safe','--continue']),0)
        self.assertEqual(call.call_args.args[0],[self.profiles.binary,'--continue'])
        self.assertEqual(path.read_bytes(),before)
        self.accounts.remove('A',self.accounts.registry()['A'])
        with patch('agy_accounts.subprocess.call') as call:
            with self.assertRaises(ValueError):self.accounts.run('A',[])
            with self.assertRaises(ValueError):self.accounts.run('I',[])
            call.assert_not_called()

    def test_run_syncs_dynamic_recent_pointer_after_official_atomic_replacement(self):
        shared=self.root/'shared/history';(shared/'conversations').mkdir(parents=True)
        (self.root/'shared/history-manifest.json').write_text('{}')
        (shared/'conversations/thread.db').write_bytes(b'synthetic')
        self.accounts.add('AA',credential('aa@example.test'))
        pointer=self.profiles.path('AA').parent/'cache/last_conversations.json'
        def call(*args,**kwargs):
            pointer.parent.mkdir(parents=True,exist_ok=True)
            temp=pointer.with_suffix('.new');temp.write_text(json.dumps({'/synthetic':'thread'}));temp.chmod(0o600);temp.replace(pointer)
            return 0
        with patch('agy_accounts.subprocess.call',side_effect=call):self.assertEqual(self.accounts.run('AA',[]),0)
        self.assertEqual(json.loads((shared/'cache/last_conversations.json').read_text()),{'/synthetic':'thread'})
        self.assertFalse(pointer.is_symlink())

    def test_quota_query_holds_shared_account_lock_through_all_requests(self):
        self.accounts.add('AA',credential('aa@example.test'));identity=self.accounts.registry()['AA'];calls=[]
        def request(url,*args):
            calls.append(url)
            with self.assertRaises(ValueError):self.accounts.remove('AA',identity)
            self.assertTrue(self.profiles.path('AA').exists())
            if url.endswith('/userinfo'):return {'email':'aa@example.test'}
            if url.endswith(':loadCodeAssist'):return {'cloudaicompanionProject':'synthetic','currentTier':{'id':'free-tier'}}
            return {'groups':[{'displayName':'Synthetic','buckets':[{'bucketId':'synthetic','remainingFraction':1}]}]}
        client=AgyQuotaClient(self.profiles,request=request,clock=lambda:1800000000)
        self.assertEqual(client.query('AA','direct')['identity'],identity)
        self.assertEqual(len(calls),3)
        self.accounts.remove('AA',identity)
        self.assertFalse(self.profiles.path('AA').exists())

    def test_quota_query_rejects_active_mutation_before_any_request(self):
        self.accounts.add('I',credential('i@example.test'))
        with self.accounts._mutation('I'),patch('agy_quota.google_request') as request:
            with self.assertRaises(ValueError):AgyQuotaClient(self.profiles,request=request).query('I','direct')
            request.assert_not_called()


class AgyRuntimeTests(unittest.TestCase):
    def test_nonregular_credential_is_rejected_without_waiting_for_a_writer(self):
        import multiprocessing
        from agy_runtime.files import read_json
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'token';os.mkfifo(path,0o600)
            def read():
                try:read_json(path)
                except ValueError:return
                raise AssertionError('nonregular login file accepted')
            process=multiprocessing.get_context('fork').Process(target=read)
            process.start();process.join(.5)
            stalled=process.is_alive()
            if stalled:process.terminate();process.join()
            self.assertFalse(stalled,'a nonregular credential must not wait for data')
            self.assertEqual(process.exitcode,0)

    def test_unknown_installed_client_is_refused_locally(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_runtime.refresh'))
        from agy_runtime.refresh import installed_client
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/'agy';binary.write_bytes(b'synthetic unknown client')
            with self.assertRaises(ValueError):installed_client(binary)

    def test_refresh_never_follows_redirects(self):
        self.assertIsNotNone(importlib.util.find_spec('agy_runtime.refresh'))
        from agy_runtime.refresh import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://invalid.test/'))
