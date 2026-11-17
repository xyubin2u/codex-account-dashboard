"""Passive quota observations and local, workload-dependent capacity estimates."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import threading
import time
import uuid
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from account_labels import valid_label,sort_key
from usage_store import AGG, JOIN

SINCE=datetime(2026,11,9,tzinfo=timezone.utc).timestamp()-9*3600
WINDOWS=('five_hour','weekly')
MAX_GAP=7200
SETTLE=30


def number(value):
    return type(value) in (float,int) and math.isfinite(value)


class QuotaCapacity:
    def __init__(self,usage,registry,credit_path,clock=time.time,run_id=None):
        self.usage,self.registry,self.credit_path=usage,registry,Path(credit_path)
        self.clock,self.run_id=clock,run_id or uuid.uuid4().hex
        self.error=None;self.lock=threading.Lock()
        with usage._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS quota_capacity_samples(
                id INTEGER PRIMARY KEY, account TEXT NOT NULL, identity TEXT NOT NULL,
                at REAL NOT NULL, started REAL NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(account,identity,at));
                CREATE INDEX IF NOT EXISTS capacity_account_time
                ON quota_capacity_samples(account,identity,at);
                CREATE TABLE IF NOT EXISTS quota_capacity_markers(
                    account TEXT,identity TEXT,at REAL,PRIMARY KEY(account,identity,at));''')

    def record(self,label,identity,snapshot,at,started):
        if not valid_label(label) or not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{64}',identity):
            raise ValueError('额度观察身份无效。')
        if not number(at) or not number(started) or not 0<started<=at:
            raise ValueError('额度观察时间无效。')
        clean={}
        for name in WINDOWS:
            window=snapshot.get(name,{})
            if not isinstance(window,dict):raise ValueError('额度窗口无效。')
            remaining,reset=window.get('remaining'),window.get('reset_at')
            if remaining is not None and (not number(remaining) or not 0<=remaining<=100):
                raise ValueError('额度比例无效。')
            if reset is not None and (not number(reset) or reset<=0):raise ValueError('额度重置时间无效。')
            clean[name]={'remaining':remaining,'reset_at':reset,'applicable':window.get('applicable') is not False}
        with self.usage._connect() as db:
            db.execute('INSERT OR IGNORE INTO quota_capacity_samples(account,identity,at,started,run_id,payload) VALUES(?,?,?,?,?,?)',
                (label,identity,at,started,self.run_id,json.dumps(clean)))
            markers,_=self._credit_markers(label,identity)
            db.executemany('INSERT OR IGNORE INTO quota_capacity_markers VALUES(?,?,?)',[(label,identity,stamp) for stamp in markers])
        self.error=None

    def safe_record(self,*args):
        try:self.record(*args)
        except Exception:self.error='额度观察暂未保存；原额度查询继续工作。'

    def _credit_markers(self,label,identity):
        try:
            ledger=json.loads(self.credit_path.read_text())
            row=ledger.get('identities',{}).get(identity) or ledger['accounts'].get(label,{})
            if row.get('identity')!=identity:return [],False
            return sorted(op['at'] for op in row.get('redeems',{}).values() if number(op.get('at'))),False
        except FileNotFoundError:return [],False
        except (OSError,ValueError,KeyError,TypeError,AttributeError):return [],True

    def _markers(self,label,identity):
        current,error=self._credit_markers(label,identity)
        with self.usage._connect() as db:
            saved=[r[0] for r in db.execute('SELECT at FROM quota_capacity_markers WHERE account=? AND identity=?',(label,identity))]
        return sorted(set(current+saved)),error

    @staticmethod
    def _usable(sample,name):
        w=sample['payload'][name]
        return w['applicable'] and w['remaining'] is not None and w['reset_at'] is not None and w['reset_at']>sample['at']

    def _segments(self,samples,name,markers):
        segments=[]
        for sample in samples:
            if not self._usable(sample,name):
                segments.append([sample]);continue
            prev=segments[-1][-1] if segments else None
            w=sample['payload'][name]
            split=not prev or not self._usable(prev,name)
            if prev and not split:
                old=prev['payload'][name]
                split=(prev['run_id']!=sample['run_id'] or sample['at']-prev['at']>MAX_GAP or
                    abs(old['reset_at']-w['reset_at'])>2 or w['remaining']>old['remaining']+.01 or
                    old['reset_at']<=sample['at'] or any(prev['at']<stamp<=sample['at'] for stamp in markers))
            if split:segments.append([sample])
            else:segments[-1].append(sample)
        return segments

    @staticmethod
    def _aggregate(db,label,identity,start,end):
        where=" WHERE e.platform='gpt' AND e.provider='openai' AND e.at>=? AND e.at<? "
        params=(start,end)
        if label is not None:
            where+=' AND a.account=? AND a.identity=? AND a.conflict=0 '
            params+=(label,identity)
        result=dict(db.execute('SELECT '+AGG+JOIN+where,params).fetchone())
        result['models']=[dict(r) for r in db.execute('SELECT e.model, '+AGG+JOIN+where+' GROUP BY e.model',params)]
        return result

    def _measure(self,db,label,identity,segment,name,scan,now,marker_error):
        first,last=segment[0],segment[-1]
        w=last['payload'][name]
        status='observing' if self._usable(last,name) else ('no_limit' if not w['applicable'] else 'unknown')
        # Only log entries between actual observed endpoints are counted.
        observed=self._aggregate(db,label,identity,first['at'],last['at'])
        dropped=max(0,round((first['payload'][name]['remaining'] or 0)-(w['remaining'] or 0),3)) if status=='observing' else None
        unattributed=db.execute('''SELECT COUNT(*) FROM events e LEFT JOIN usage_accounts a ON e.id=a.event_id
            WHERE e.platform='gpt' AND e.provider='openai' AND e.at>=? AND e.at<? AND (a.account IS NULL OR a.conflict!=0)''',
            (first['at'],last['at'])).fetchone()[0]
        conflict=db.execute('''SELECT COUNT(*) FROM events e JOIN usage_accounts a ON e.id=a.event_id
            WHERE e.platform='gpt' AND e.provider='openai' AND e.at>=? AND e.at<? AND a.account=? AND a.identity!=?''',
            (first['at'],last['at'],label,identity)).fetchone()[0]
        boundary=db.execute('''SELECT COUNT(*) FROM events e JOIN usage_accounts a ON e.id=a.event_id
            WHERE e.platform='gpt' AND e.provider='openai' AND a.account=? AND a.identity=? AND a.conflict=0
            AND ((e.at>=? AND e.at<=?) OR (e.at>=? AND e.at<=?))''',
            (label,identity,first['started'],first['at'],last['started'],last['at'])).fetchone()[0]
        reasons=[]
        if status!='observing':reasons.append(status)
        if not dropped:reasons.append('need_change')
        elif dropped<20:reasons.append('small_change')
        if observed['requests']<5:reasons.append('few_requests')
        if last['at']-first['at']<60:reasons.append('short_span')
        if now-last['at']<SETTLE:reasons.append('settling')
        if not scan.get('updated_at') or scan['updated_at']<last['at'] or scan.get('error') or scan.get('running'):
            reasons.append('index_behind')
        if unattributed:reasons.append('unattributed')
        if conflict:reasons.append('identity_conflict')
        if boundary:reasons.append('boundary')
        if observed['unpriced_requests']:reasons.append('unpriced')
        if marker_error:reasons.append('credit_history_unavailable')
        estimate=None
        if not reasons:
            estimate={'total_tokens':observed['total_tokens']*100/dropped,
                'api_usd':observed['estimated_cost_usd']*100/dropped,'basis':'local_observed_mix'}
        return dict(status=status,observed=observed,from_at=first['at'],until_at=last['at'],
            reset_at=w['reset_at'],remaining=w['remaining'],quota_drop=dropped,
            snapshot_count=len(segment),unattributed_requests=unattributed,
            boundary_requests=boundary,reasons=reasons,estimate=estimate)

    def snapshot(self,timezone='Asia/Tokyo'):
        try:since=datetime(2026,11,9,tzinfo=ZoneInfo(timezone)).timestamp()
        except (ZoneInfoNotFoundError,ValueError,TypeError):raise ValueError('时区无效。') from None
        now=self.clock();scan=self.usage.signal();identities=self.registry();accounts=[]
        with self.usage._connect() as db:
            db.execute('BEGIN')
            for label in sorted(identities,key=sort_key):
                identity=identities.get(label)
                if not identity:continue
                samples=[dict(r) for r in db.execute('SELECT at,started,run_id,payload FROM quota_capacity_samples WHERE account=? AND identity=? AND at>=? AND at<=? ORDER BY at',
                    (label,identity,max(since,now-90*86400),now))]
                for s in samples:s['payload']=json.loads(s['payload'])
                markers,marker_error=self._markers(label,identity)
                windows={}
                for name in WINDOWS:
                    segments=self._segments(samples,name,markers)
                    if not segments:
                        windows[name]=dict(status='waiting',observed={'requests':0,'total_tokens':0,'estimated_cost_usd':None},
                            estimate=None,reasons=['waiting'],history_samples=0,history=[],history_range=None)
                        continue
                    # Summaries are bounded; the raw observations remain available.
                    def settled(seg):
                        safe=[s for s in seg if s['at']<=now-SETTLE]
                        return safe if len(safe)>=2 and self._usable(seg[-1],name) else seg
                    measured=[self._measure(db,label,identity,settled(seg),name,scan,now,marker_error) for seg in segments[-12:]]
                    row=measured[-1];qualified=[m['estimate'] for m in measured if m['estimate']]
                    if now-row['until_at']>MAX_GAP or row.get('reset_at') and row['reset_at']<=now:
                        row['reasons'].append('stale');row['estimate']=None
                        qualified=[m['estimate'] for m in measured[:-1] if m['estimate']]
                    row['history_samples']=len(qualified)
                    row['history_range']=dict(total_tokens={'median':statistics.median(q['total_tokens'] for q in qualified),
                        'min':min(q['total_tokens'] for q in qualified),'max':max(q['total_tokens'] for q in qualified)},
                        api_usd={'median':statistics.median(q['api_usd'] for q in qualified),
                        'min':min(q['api_usd'] for q in qualified),'max':max(q['api_usd'] for q in qualified)}) if qualified else None
                    row['history']=[{k:m[k] for k in ('from_at','until_at','quota_drop','estimate','reasons')} for m in measured[:-1][-5:]]
                    windows[name]=row
                accounts.append(dict(label=label,windows=windows,since_usage=self._aggregate(db,label,identity,since,now)))
            since_total=self._aggregate(db,None,None,since,now)
        return dict(since='2026-11-09',timezone=timezone,scope='local_observed',external_use='user_confirmed_local_only',since_total=since_total,
            accounts=accounts,scan=scan,collection_error=self.error,server_time=now,
            note='本机样本粗估，非官方固定Token额度；五小时和周不可相加。API等价金额非订阅扣款。')
