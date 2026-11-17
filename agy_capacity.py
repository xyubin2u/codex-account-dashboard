"""Per-bucket AGY observations backed by actual local token usage."""
from datetime import datetime
import json
from pathlib import Path
import re
import statistics
import time
import uuid
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from quota_capacity import QuotaCapacity,MAX_GAP,SETTLE,number
from usage_store import AGG,JOIN
from account_labels import valid_label

# Relationships established by the native catalog and the official quota IDs.
BUCKETS={'gemini-weekly':('gemini','Gemini · 周窗口'),
         'gemini-5h':('gemini','Gemini · 五小时窗口'),
         '3p-weekly':('3p','Claude / GPT · 周窗口'),
         '3p-5h':('3p','Claude / GPT · 五小时窗口'),
         'gemini-argon-wtu-weekly':('argon','Gemini Argon · 周窗口')}


class AgyQuotaCapacity:
    _usable=staticmethod(QuotaCapacity._usable)

    def _segments(self,samples,name,markers):
        # A tier or explicit bucket change can alter which models share a pool.
        scopes=[];batch=[];previous=None
        for sample in samples:
            window=sample['payload'][name]
            scope=(window.get('family'),window.get('include_argon'))
            if batch and scope!=previous:
                scopes.extend(QuotaCapacity._segments(self,batch,name,markers));batch=[]
            batch.append(sample);previous=scope
        if batch:scopes.extend(QuotaCapacity._segments(self,batch,name,markers))
        return scopes

    def __init__(self,usage,registry,clock=time.time,run_id=None,current_state=None):
        self.usage=usage;self.registry=registry;self.clock=clock;self.run_id=run_id or uuid.uuid4().hex;self.error=None
        self.current_state=current_state
        with usage._connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS agy_quota_capacity_samples(
                account TEXT NOT NULL,identity TEXT NOT NULL,at REAL NOT NULL,started REAL NOT NULL,
                run_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(account,identity,at));
                CREATE INDEX IF NOT EXISTS agy_capacity_account_at ON agy_quota_capacity_samples(account,identity,at);''')

    def record(self,label,identity,snapshot,at,started):
        if not valid_label(label) or not isinstance(identity,str) or not re.fullmatch('[0-9a-f]{64}',identity):raise ValueError('AGY 观察身份无效。')
        if not number(at) or not number(started) or not 0<started<=at:raise ValueError('AGY 观察时间无效。')
        payload={};groups=snapshot.get('groups')
        if not isinstance(groups,list):raise ValueError('AGY 额度分组无效。')
        explicit_argon=any(b.get('id')=='gemini-argon-wtu-weekly' for g in groups for b in g.get('buckets',[]))
        for group in groups:
            for bucket in group.get('buckets',[]):
                bid=bucket.get('id');remaining=bucket.get('remaining');reset=bucket.get('reset_at')
                if not isinstance(bid,str) or not bid or len(bid)>200:raise ValueError('AGY 额度标识无效。')
                if remaining is not None and (not number(remaining) or not 0<=remaining<=100):raise ValueError('AGY 额度比例无效。')
                if reset is not None and (not number(reset) or reset<=0):raise ValueError('AGY 重置时间无效。')
                family,title=BUCKETS.get(bid,(None,group.get('display_name','AGY')+' · '+bucket.get('display_name',bid)))
                payload[bid]={'remaining':remaining,'reset_at':reset,'applicable':bucket.get('disabled') is not True,
                    'family':family,'title':title[:250],
                    'include_argon':family=='gemini' and snapshot.get('plan_type')=='Ultra' and not explicit_argon}
        if not payload:raise ValueError('AGY 未返回额度窗口。')
        with self.usage._connect() as db:
            db.execute('INSERT OR IGNORE INTO agy_quota_capacity_samples VALUES(?,?,?,?,?,?)',
                (label,identity,at,started,self.run_id,json.dumps(payload,allow_nan=False)))
        self.error=None

    def safe_record(self,*args):
        try:self.record(*args)
        except Exception:self.error='AGY 额度观察暂未保存；原查询继续工作。'

    @staticmethod
    def _group_scope(window):
        family=window.get('family')
        if not family:return ' AND 0 ',[]
        families=[family]
        if window.get('include_argon'):families.append('argon')
        return ' AND e.quota_group IN ('+','.join('?' for _ in families)+') ',families

    def _aggregate(self,db,label,identity,start,end,window=None):
        where=" WHERE e.platform='agy' AND e.at>=? AND e.at<? ";params=[start,end]
        if label is not None:
            where+=' AND a.account=? AND a.identity=? AND a.conflict=0 ';params.extend([label,identity])
        if window:
            clause,values=self._group_scope(window);where+=clause;params.extend(values)
        out=dict(db.execute('SELECT '+AGG+JOIN+where,params).fetchone())
        out['models']=[dict(r) for r in db.execute('SELECT e.model, '+AGG+JOIN+where+' GROUP BY e.model',params)]
        return out

    def _measure(self,db,label,identity,segment,name,scan,now):
        first,last=segment[0],segment[-1];w=last['payload'][name]
        status='observing' if self._usable(last,name) else 'no_limit' if not w['applicable'] else 'unknown'
        observed=self._aggregate(db,label,identity,first['at'],last['at'],w)
        dropped=max(0,round((first['payload'][name]['remaining'] or 0)-(w['remaining'] or 0),3)) if status=='observing' else None
        scope,scope_params=self._group_scope(w)
        query=" FROM events e LEFT JOIN usage_accounts a ON e.id=a.event_id WHERE e.platform='agy' AND e.at>=? AND e.at<? "
        bounds=[first['at'],last['at']]
        unattributed=db.execute('SELECT COUNT(*)'+query+scope+' AND (a.account IS NULL OR a.conflict!=0)',bounds+scope_params).fetchone()[0]
        unknown_group=db.execute('SELECT COUNT(*)'+query+' AND e.quota_group IS NULL',bounds).fetchone()[0]
        conflict=db.execute('SELECT COUNT(*)'+query+scope+' AND a.account=? AND a.identity!=?',bounds+scope_params+[label,identity]).fetchone()[0]
        boundary=db.execute("SELECT COUNT(*) FROM events e JOIN usage_accounts a ON a.event_id=e.id WHERE e.platform='agy' "+scope+
            ' AND a.account=? AND a.identity=? AND a.conflict=0 AND ((COALESCE(e.completed_at,e.at)>=? AND e.at<=?) OR (COALESCE(e.completed_at,e.at)>=? AND e.at<=?))',
            scope_params+[label,identity,first['started'],first['at'],last['started'],last['at']]).fetchone()[0]
        incomplete=db.execute('SELECT COUNT(*)'+query+scope+
            ' AND (e.completed_at IS NULL OR e.completed_at<? OR e.completed_at>?)',
            bounds+scope_params+[first['at'],now-SETTLE]).fetchone()[0]
        reasons=[]
        if status!='observing':reasons.append(status)
        if not w.get('family'):reasons.append('unknown_group')
        if not dropped:reasons.append('need_change')
        elif dropped<20:reasons.append('small_change')
        if observed['requests']<5:reasons.append('few_requests')
        if last['at']-first['at']<60:reasons.append('short_span')
        if now-last['at']<SETTLE:reasons.append('settling')
        if not scan.get('updated_at') or scan['updated_at']<last['at'] or scan.get('error') or scan.get('running'):reasons.append('index_behind')
        if unattributed:reasons.append('unattributed')
        if unknown_group:reasons.append('unknown_group')
        if conflict:reasons.append('identity_conflict')
        if boundary:reasons.append('boundary')
        if incomplete:reasons.append('incomplete_usage')
        indexer=getattr(self.usage,'external_indexer',None)
        if indexer:
            signal=indexer.signal()
            if signal.get('error') or signal.get('pending_sources') or signal.get('running'):reasons.append('index_behind')
        estimate=None
        if not reasons:
            estimate={'total_tokens':observed['total_tokens']*100/dropped,
                'api_usd':observed['estimated_cost_usd']*100/dropped if not observed['unpriced_requests'] and observed['estimated_cost_usd'] is not None else None,
                'basis':'local_observed_mix'}
        if observed['unpriced_requests']:reasons.append('unpriced')
        return dict(status=status,title=w.get('title',name),observed=observed,from_at=first['at'],until_at=last['at'],
            reset_at=w['reset_at'],remaining=w['remaining'],quota_drop=dropped,snapshot_count=len(segment),
            unattributed_requests=unattributed,boundary_requests=boundary,reasons=reasons,estimate=estimate)

    def snapshot(self,timezone='Asia/Tokyo'):
        try:since=datetime(2026,11,9,tzinfo=ZoneInfo(timezone)).timestamp()
        except (ZoneInfoNotFoundError,ValueError,TypeError):raise ValueError('时区无效。') from None
        now=self.clock();scan=self.usage.signal();identities=self.registry();accounts=[]
        current={}
        if self.current_state:
            try:current={row['label']:row.get('snapshot') for row in self.current_state().get('accounts',[])}
            except Exception:pass
        with self.usage._connect() as db:
            db.execute('BEGIN')
            for label,registered in identities.items():
                identity=registered['identity'] if isinstance(registered,dict) else registered
                samples=[dict(r) for r in db.execute('SELECT at,started,run_id,payload FROM agy_quota_capacity_samples WHERE account=? AND identity=? AND at>=? AND at<=? ORDER BY at',
                    (label,identity,max(since,now-90*86400),now))]
                for sample in samples:sample['payload']=json.loads(sample['payload'])
                windows={}
                if not samples:
                    for group in (current.get(label) or {}).get('groups',[]):
                        for bucket in group.get('buckets',[]):
                            bid=bucket['id'];status='no_limit' if bucket.get('disabled') else 'waiting'
                            windows[bid]={'title':BUCKETS.get(bid,(None,group['display_name']))[1],'status':status,
                                'observed':{'requests':0,'total_tokens':0,'estimated_cost_usd':None},'estimate':None,
                                'reasons':[status],'history_samples':0,'history':[],'history_range':None}
                # The current official set determines which quota buckets exist now.
                names=list(samples[-1]['payload']) if samples else []
                for name in names:
                    valid=[]
                    for sample in samples:
                        if name not in sample['payload']:
                            valid.append({**sample,'payload':{name:{'remaining':None,'reset_at':None,'applicable':True}}})
                        else:valid.append(sample)
                    segments=self._segments(valid,name,[])
                    def settled(segment):
                        safe=[s for s in segment if s['at']<=now-SETTLE]
                        return safe if len(safe)>=2 and self._usable(segment[-1],name) else segment
                    measured=[self._measure(db,label,identity,settled(s),name,scan,now) for s in segments[-12:]]
                    row=measured[-1]
                    if now-row['until_at']>MAX_GAP or row.get('reset_at') and row['reset_at']<=now:
                        row['reasons'].append('stale');row['estimate']=None
                    qualified=[m['estimate'] for m in measured if m['estimate']]
                    row['history_samples']=len(qualified);row['history']=[]
                    row['history_range']=None
                    if qualified:
                        amounts=[q['api_usd'] for q in qualified if q['api_usd'] is not None]
                        def stats(values):return {'min':min(values),'max':max(values),'median':statistics.median(values)}
                        row['history_range']={'total_tokens':stats([q['total_tokens'] for q in qualified]),
                            'api_usd':stats(amounts) if amounts else {'min':None,'max':None,'median':None}}
                    windows[name]=row
                accounts.append({'label':label,'platform':'agy','display_name':'AGY '+label,'windows':windows,
                    'since_usage':self._aggregate(db,label,identity,since,now)})
            total=self._aggregate(db,None,None,since,now)
        return {'since':'2026-11-09','timezone':timezone,'platform':'agy','scope':'local_observed','external_use':'local_scope_only',
            'since_total':total,'accounts':accounts,'scan':scan,'collection_error':self.error,'server_time':now,
            'note':'按本机已观察且可归属的 AGY 调用估算各模型组；金额按对应 API 模型的 Google / Anthropic 官方参考价，缓存按供应商规则。其他设备消耗无法计入，非订阅扣款；未知价格保留未定价。'}


class PlatformCapacity:
    def __init__(self,gpt,agy,usage):self.gpt=gpt;self.agy=agy;self.usage=usage

    def snapshot(self,timezone='Asia/Tokyo',platform='gpt'):
        if platform not in ('gpt','agy','all'):raise ValueError('平台筛选无效。')
        if platform=='agy':return self.agy.snapshot(timezone)
        data=self.gpt.snapshot(timezone)
        for account in data['accounts']:
            account['platform']='gpt';account['display_name']='GPT '+account['label']
        data['platform']=platform
        if platform=='all':
            other=self.agy.snapshot(timezone)
            data['accounts']+=other['accounts']
            with self.usage._connect() as db:
                since=datetime(2026,11,9,tzinfo=ZoneInfo(timezone)).timestamp()
                data['since_total']=dict(db.execute('SELECT '+AGG+JOIN+" WHERE e.at>=? AND e.at<? AND (e.platform='agy' OR (e.platform='gpt' AND e.provider='openai'))",(since,data['server_time'])).fetchone())
            data['collection_error']='；'.join(v for v in [data['collection_error'],other['collection_error']] if v) or None
            data['note']='累计用量合并 GPT 和 AGY；GPT 按 OpenAI 官方 / 手动价，AGY 按对应 API 模型的 Google / Anthropic 官方参考价。各平台、账号、模型组额度分别测算，不能把窗口容量相加，金额非订阅扣款。'
        return data
