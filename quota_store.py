"""Account-scoped, single-flight quota cache with honest stale-data state."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from account_labels import LEGACY_LABELS,valid_label,sort_key
from quota_data import QuotaError, ERRORS, account_display, account_subscription
from auto_pool import read_pool, set_member
from auto_threshold import read_all_thresholds, set_threshold, set_thresholds
from subscription_dates import SubscriptionDates


class QuotaStore:
    def __init__(self, path, registry, probe, cooldown=5, pool_base=None, activity=None, clock=None):
        self.path = Path(path)
        self.subscription_dates=SubscriptionDates(self.path.with_name('subscription-dates.json'))
        self.pool_base = Path(pool_base) if pool_base is not None else self.path.parent
        self.registry, self.probe = registry, probe
        self.activity=activity or (lambda:{})
        self.clock=clock or time.time
        self.started_at=self.clock()
        self.active_counts={};self.activity_checked_at=None;self.activity_error=None
        self.next_activity_scan=0;self.newly_active=set();self.defer_until={}
        self.profile_cache={};self.revision=0
        self.instance_id=uuid.uuid4().hex
        self.cooldown = cooldown
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='quota')
        self.rows = {}
        self.interval = 15
        self.network_mode = "clash"
        self.route_generation = 0
        self.closed = False
        self.snapshot_observer = None
        try:
            saved = json.loads(self.path.read_text())
            self.rows = saved.get('accounts', {})
            if saved.get('network_mode') in ('clash','direct'):
                self.network_mode = saved['network_mode']
            interval=saved.get('interval')
            if type(interval) is int and interval in (0,15,60,300,3600):
                self.interval=15 if saved.get('refresh_policy_version',0)<2 and interval in (60,300) else interval
        except (OSError, ValueError, AttributeError):
            pass
        for row in self.rows.values():
            row['refreshing'] = False
            row.pop('refresh_again',None)
            if row.get('snapshot') is not None:
                row.setdefault('snapshot_mode','clash')
        if hasattr(self.probe, 'set_mode'):
            self.probe.set_mode(self.network_mode)
        with self.lock:
            self._sync()

    def _sync(self):
        identities = self.registry()
        labels=sorted(set(LEGACY_LABELS)|{label for label in identities if valid_label(label)},key=sort_key)
        for label in list(self.rows):
            if label not in labels:
                self.rows.pop(label);self.profile_cache.pop(label,None);self.defer_until.pop(label,None)
        for label in labels:
            identity = identities.get(label)
            previous = self.rows.get(label, {})
            if previous.get('identity') != identity or label not in self.rows:
                self.rows[label] = {'identity':identity,'snapshot':None,'fetched_at':None,
                                    'attempted_at':None,'completed_at':None,'error':None,'refreshing':False}
                self.profile_cache.pop(label,None);self.defer_until.pop(label,None)
                self.revision+=1
            if 'subscription_edit_token' not in self.rows[label]:
                self.rows[label]['subscription_edit_token']=uuid.uuid4().hex
        return identities

    def effective_interval(self,label):
        if self.interval==15:return 15 if self.active_counts.get(label,0)>0 else 3600
        return self.interval

    def _due_at(self,label):
        if not self.interval:return None
        if label in self.defer_until:return self.defer_until[label]
        row=self.rows[label]
        stamps=[row.get(key) for key in ('attempted_at','completed_at','fetched_at')]
        stamps=[value for value in stamps if type(value) in (int,float) and value>0]
        return max(stamps)+self.effective_interval(label) if stamps else self.started_at

    @property
    def next_refresh_at(self):
        due=[self._due_at(label) for label,row in self.rows.items() if row['identity'] and not row['refreshing']]
        return min((value for value in due if value is not None),default=None)

    def _scan_activity(self,now):
        try:
            counts=self.activity()
            if not isinstance(counts,dict):raise ValueError()
            error=None
        except Exception:
            counts={};error='暂时无法识别使用中账号；智能模式暂按每小时更新，可手动刷新。'
        with self.lock:
            self._sync()
            counts={label:count for label,count in counts.items() if valid_label(label) and
                    type(count) is int and count>0 and self.rows.get(label,{}).get('identity')}
            new=set(counts)-set(self.active_counts)
            self.newly_active.update(new)
            for label in new:
                self.defer_until.pop(label,None);self.profile_cache.pop(label,None)
            if counts!=self.active_counts or error!=self.activity_error or self.activity_checked_at is None:
                self.revision+=1
            self.active_counts=counts;self.activity_error=error;self.activity_checked_at=now

    def activity_state(self):
        with self.lock:
            return {'revision':self.revision,'instance_id':self.instance_id,'server_time':self.clock(),'active_accounts':sorted(self.active_counts),
                    'active_counts':dict(self.active_counts),'activity_checked_at':self.activity_checked_at,
                    'activity_error':self.activity_error,'busy':any(row['refreshing'] for row in self.rows.values())}

    def _profile(self,label,row,now):
        cached=self.profile_cache.get(label)
        interval=self.effective_interval(label)
        if cached is None or cached['identity']!=row['identity'] or interval and now-cached['at']>=interval:
            cached={'identity':row['identity'],'at':now,
                    'display':account_display(self.pool_base,label,row['identity']) if row['identity'] else {'display_name':None,'email':None},
                    'subscription':account_subscription(self.pool_base,label,row['identity']) if row['identity'] else
                        {'plan_type':None,'active_until':None,'checked_at':None,'source':None}}
            self.profile_cache[label]=cached
        return cached

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.quota-cache-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'version':1,'refresh_policy_version':2,'accounts':self.rows,'interval':self.interval,'network_mode':self.network_mode}, stream)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    def refresh(self, target='all', force=False, invalidate_profiles=True):
        if target != 'all' and not valid_label(target):
            raise ValueError('账户只能是 A–H 或 all。')
        accepted, busy, skipped = [], [], []
        with self.lock:
            if self.closed:
                return {'accepted':[],'busy':[],'skipped':list(self.rows) if target=='all' else [target]}
            self._sync()
            labels = tuple(self.rows) if target == 'all' else [target]
            for label in labels:
                row = self.rows.get(label)
                if invalidate_profiles:self.profile_cache.pop(label,None)
                if not row or not row['identity']:
                    skipped.append(label)
                elif row['refreshing']:
                    if force:row['refresh_again']=True
                    busy.append(label)
                elif not force and row['attempted_at'] and self.clock()-row['attempted_at'] < self.cooldown:
                    skipped.append(label)
                else:
                    row['refreshing'] = True
                    row['attempted_at'] = self.clock()
                    row['attempted_mode'] = self.network_mode
                    accepted.append(label)
                    self.defer_until.pop(label,None);self.revision+=1
                    probe = self.probe.capture() if hasattr(self.probe, 'capture') else self.probe
                    self.pool.submit(self._fetch, label, row['identity'], self.route_generation, self.network_mode, probe)
            return {'accepted':accepted,'busy':busy,'skipped':skipped}

    def _fetch(self, label, identity, generation, mode, probe):
        with self.lock:
            if generation != self.route_generation:
                return
            started=self.clock()
        snapshot = None
        error = None
        try:
            snapshot = probe(label)
        except QuotaError as exc:
            error = {'kind':exc.kind,'message':ERRORS[exc.kind]}
        except Exception:
            error = {'kind':'internal','message':ERRORS['internal']}
        with self.lock:
            self._sync()
            row = self.rows.get(label)
            if row is None or row['identity'] != identity or generation != self.route_generation:
                return  # A re-login cannot receive the previous user's result.
            row['refreshing'] = False
            row['error'] = error
            row['completed_at']=self.clock();self.revision+=1
            self.profile_cache.pop(label,None)
            if snapshot is not None and error is None:
                row['snapshot'] = snapshot
                row['fetched_at'] = self.clock()
                row['snapshot_mode'] = mode
            self._save()
            if snapshot is not None and error is None and self.snapshot_observer:
                try:self.snapshot_observer(label,identity,copy.deepcopy(snapshot),row['fetched_at'],started)
                except Exception:pass  # Passive measurement cannot fail the original quota query.
            if row.pop('refresh_again',False) and not self.closed:
                self.refresh(label,force=True)

    def public_state(self):
        with self.lock:
            self._sync()
            enabled_pool = read_pool(self.pool_base)
            threshold_settings = read_all_thresholds(self.pool_base)
            thresholds = threshold_settings['five_hour']
            weekly_thresholds = threshold_settings['weekly']
            now = self.clock()
            accounts = []
            for label in self.rows:
                row = self.rows[label]
                snapshot = row['snapshot']
                profile=self._profile(label,row,now)
                subscription=copy.deepcopy(profile['subscription'])
                try:
                    manual=self.subscription_dates.get(label,row['identity'])
                    if manual:
                        subscription.update(source='manual',login_active_until=subscription.get('active_until'),
                            active_until=None,manual_date=manual['date'],manual_updated_at=manual['updated_at'])
                except ValueError as exc:
                    subscription['manual_date_error']=str(exc)
                cadence=self.effective_interval(label)
                reset_elapsed = bool(snapshot and any(w.get('reset_at') and w['reset_at'] <= now for w in snapshot.values()))
                accounts.append({'label':label,'registered':bool(row['identity']),
                    **profile['display'],'subscription':subscription,'subscription_edit_token':row['subscription_edit_token'],
                    'profile_fetched_at':profile['at'],'active':self.active_counts.get(label,0)>0,
                    'active_processes':self.active_counts.get(label,0),'refresh_interval':cadence,
                    'next_refresh_at':self._due_at(label),
                    'manual_only':label not in enabled_pool,'auto_enabled':label in enabled_pool,'auto_threshold':thresholds.get(label,0),'auto_weekly_threshold':weekly_thresholds.get(label,0),'snapshot':copy.deepcopy(snapshot),
                    'fetched_at':row['fetched_at'],'attempted_at':row['attempted_at'],
                    'error':copy.deepcopy(row['error']),'refreshing':row['refreshing'],
                    'snapshot_mode':row.get('snapshot_mode'),'attempted_mode':row.get('attempted_mode'),
                    'stale':bool(snapshot and (row.get('snapshot_mode') != self.network_mode or row['error'] or reset_elapsed or now-row['fetched_at']>max(60,cadence or 3600)) )})
            return {'app':'codex-quota-dashboard','accounts':accounts,'server_time':now,
                    'interval':self.interval,'network_mode':self.network_mode,'next_refresh_at':self.next_refresh_at,
                    **self.activity_state()}

    def set_subscription_date(self,label,value,edit_token):
        with self.lock:
            if not valid_label(label) or self.closed:raise ValueError('账户无效或服务正在停止。')
            identities=self._sync()
            if not identities.get(label):raise ValueError('账户尚未登记。')
            row=self.rows[label]
            if type(edit_token) is not str or edit_token!=row['subscription_edit_token']:
                raise ValueError('账户资料已变化，请关闭后重新打开日期编辑。')
            result=self.subscription_dates.set(label,row['identity'],value,now=self.clock())
            row['subscription_edit_token']=uuid.uuid4().hex;self.revision+=1
            self._save()
            return {'source':'manual' if result else 'login_profile','date':result['date'] if result else None}

    def set_auto_member(self, label, enabled):
        with self.lock:
            identities = self._sync()
            if not valid_label(label) or not identities.get(label):
                raise ValueError('账户未登记。')
            selected = set_member(self.pool_base,label,enabled)
            self.revision+=1
            return label in selected

    def set_auto_threshold(self, label, threshold, window='five_hour'):
        with self.lock:
            if not valid_label(label) or not self._sync().get(label):
                raise ValueError('账户未登记。')
            result=set_threshold(self.pool_base,label,threshold,window=window);self.revision+=1
            return result

    def set_auto_thresholds(self, labels, five_hour=None, weekly=None):
        with self.lock:
            identities=self._sync()
            if not isinstance(labels,list) or any(not valid_label(label) or not identities.get(label) for label in labels):
                raise ValueError('账户未登记或无效。')
            result=set_thresholds(self.pool_base,labels,five_hour=five_hour,weekly=weekly);self.revision+=1
            return result

    def set_interval(self, interval):
        if type(interval) is not int or interval not in (0,15,60,300,3600):
            raise ValueError('请选择智能更新、每小时或手动更新。')
        with self.lock:
            self.interval = interval
            self.profile_cache.clear();self.revision+=1
            if interval==15:self.newly_active.update(self.active_counts)
            self._save()

    def set_network_mode(self, mode):
        if not isinstance(mode, str) or mode not in ('clash','direct'):
            raise ValueError('查询路线只能是 Clash 或 WireGuard／系统网络。')
        with self.lock:
            if self.closed:
                raise ValueError('看板正在停止。')
            if mode != self.network_mode:
                if hasattr(self.probe, 'set_mode'):
                    self.probe.set_mode(mode)
                self.route_generation += 1
                self.network_mode = mode
                self.revision+=1
                for row in self.rows.values():
                    row['refreshing'] = False
                    row['attempted_at'] = None
                    row['error'] = None
                self._save()
            return self.refresh('all')

    def tick(self):
        now=self.clock()
        with self.lock:
            if self.closed:return
            scan=now>=self.next_activity_scan
            if scan:self.next_activity_scan=now+3
        if scan:self._scan_activity(now)
        with self.lock:
            self._sync();new=self.newly_active;self.newly_active=set()
            if not self.interval or self.closed:return
            for label,row in self.rows.items():
                if not row['identity'] or row['refreshing']:continue
                immediate=self.interval==15 and label in new
                if immediate or now>=self._due_at(label):
                    self.refresh(label,force=immediate,invalidate_profiles=False)

    def defer_initial_refresh(self):
        now=self.clock();self._scan_activity(now)
        with self.lock:
            self.newly_active.clear();self.next_activity_scan=now+3
            for label,row in self.rows.items():
                if row['identity'] and not row.get('attempted_at') and self.interval:
                    self.defer_until[label]=now+self.effective_interval(label)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
