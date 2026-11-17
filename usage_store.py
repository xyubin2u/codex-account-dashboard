"""Local incremental usage index, independent of account/quota operations."""
from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from usage_parser import UsageParser, clean
from usage_pricing import OFFICIAL_PRICES
from agy_pricing import OFFICIAL_AGY_PRICES
from account_labels import valid_label,sort_key
import re

TOKEN_COLUMNS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                 'uncached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
EVENT_COLUMNS = ('id', 'at', 'model', 'provider', 'session', 'source', *TOKEN_COLUMNS,
                 'duration_ms', 'speed_tps')
PRICE_FIELDS = ('input', 'cached_input', 'cache_write', 'output')
def _rate(field):
    return (f"(CASE WHEN e.platform='agy' THEN "+
        f'(CASE WHEN ar.context_threshold IS NOT NULL AND e.input_tokens>ar.context_threshold THEN ar.long_{field} ELSE ap.{field} END) '+
        f'ELSE (CASE WHEN r.context_threshold IS NOT NULL AND e.input_tokens>r.context_threshold THEN r.long_{field} ELSE p.{field} END) END)')


_COST_PARTS = [('uncached_input_tokens','input'), ('cached_input_tokens','cached_input'),
               ('cache_write_input_tokens','cache_write'), ('output_tokens','output')]
COST = ('CASE WHEN (p.model IS NULL AND ap.model IS NULL) OR '+
        ' OR '.join(f'(e.{tokens}>0 AND {_rate(field)} IS NULL)' for tokens,field in _COST_PARTS)+
        ' THEN NULL ELSE ('+
        ' + '.join(f'e.{tokens}*COALESCE({_rate(field)},0)' for tokens,field in _COST_PARTS)+
        ')/1000000.0 END')
AGG = ('COUNT(*) AS requests, COUNT(DISTINCT e.session) AS sessions, '+
       'COALESCE(SUM(a.conflict=0),0) AS attributed_requests, COUNT(*)-COALESCE(SUM(a.conflict=0),0) AS unattributed_requests, '+
       ', '.join(f'COALESCE(SUM(e.{col}),0) AS {col}' for col in TOKEN_COLUMNS)+
       ', SUM('+COST+') AS estimated_cost_usd, '+
       'COALESCE(SUM(('+COST+') IS NULL),0) AS unpriced_requests, '+
       'COUNT(e.speed_tps) AS speed_samples, '+
       'SUM(CASE WHEN e.speed_tps IS NOT NULL THEN e.output_tokens END) / '+
       'SUM(CASE WHEN e.speed_tps IS NOT NULL THEN e.duration_ms/1000.0 END) AS speed_tps')
JOIN = (" FROM events e LEFT JOIN prices p ON p.model=e.model AND e.platform='gpt' LEFT JOIN price_rules r ON r.model=e.model AND e.platform='gpt' "+
        "LEFT JOIN agy_prices ap ON ap.model=e.model AND e.platform='agy' LEFT JOIN agy_price_rules ar ON ar.model=e.model AND e.platform='agy' "+
        "LEFT JOIN usage_accounts a ON a.event_id=e.id AND (e.provider='openai' OR e.platform='agy') ")
ACCOUNT = 'CASE WHEN a.conflict=0 THEN a.account ELSE NULL END'
MAX_LINE = 8*1024*1024
PARSER_VERSION = 3


class UsageStore:
    def __init__(self, path, base, clock=time.time):
        self.path, self.base, self.clock = Path(path), Path(base).resolve(), clock
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('用量索引路径不可为符号链接。')
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._mutex, self._progress_lock = threading.Lock(), threading.Lock()
        self._cache_lock = threading.Lock()
        self._revision, self._cache = 0, {}
        self._thread = None
        self.account_observer = None
        self.external_indexer = None
        self._stop = threading.Event()
        self._progress = dict(running=False, files_total=0, files_done=0, indexed_files=0,
                              scanned_bytes=0, updated_at=None, error=None, skipped_records=0)
        with self._connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, device INTEGER, inode INTEGER,
                  size INTEGER DEFAULT 0, mtime INTEGER DEFAULT 0, offset INTEGER DEFAULT 0,
                  state TEXT NOT NULL DEFAULT '{}', skipped INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                  id TEXT PRIMARY KEY, at REAL NOT NULL, model TEXT NOT NULL, provider TEXT,
                  session TEXT, source TEXT, input_tokens INTEGER, cached_input_tokens INTEGER,
                  cache_write_input_tokens INTEGER, uncached_input_tokens INTEGER,
                  output_tokens INTEGER, reasoning_output_tokens INTEGER, total_tokens INTEGER,
                  duration_ms REAL, speed_tps REAL);
                CREATE INDEX IF NOT EXISTS usage_at ON events(at);
                CREATE INDEX IF NOT EXISTS usage_model_at ON events(model,at);
                CREATE TABLE IF NOT EXISTS origins (
                  source_id INTEGER, event_id TEXT, PRIMARY KEY(source_id,event_id));
                CREATE INDEX IF NOT EXISTS origin_event ON origins(event_id);
                CREATE TABLE IF NOT EXISTS prices (
                  model TEXT PRIMARY KEY, input REAL, cached_input REAL, cache_write REAL, output REAL);
                CREATE TABLE IF NOT EXISTS price_rules (
                  model TEXT PRIMARY KEY, url TEXT, checked_at TEXT, context_threshold INTEGER,
                  long_input REAL, long_cached_input REAL, long_cache_write REAL, long_output REAL,
                  cache_write_ttl TEXT);
                CREATE TABLE IF NOT EXISTS agy_prices (
                  model TEXT PRIMARY KEY, input REAL, cached_input REAL, cache_write REAL, output REAL);
                CREATE TABLE IF NOT EXISTS agy_price_rules (
                  model TEXT PRIMARY KEY, url TEXT, checked_at TEXT, context_threshold INTEGER,
                  long_input REAL, long_cached_input REAL, long_cache_write REAL, long_output REAL,
                  cache_write_ttl TEXT,kind TEXT,source_model TEXT,pricing_basis TEXT,mapping_note TEXT,
                  valid_until TEXT);
                CREATE TABLE IF NOT EXISTS usage_accounts (
                  event_id TEXT PRIMARY KEY, account TEXT, identity TEXT, conflict INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);
            ''')
            if 'skipped' not in {row[1] for row in db.execute('PRAGMA table_info(sources)')}:
                db.execute('ALTER TABLE sources ADD COLUMN skipped INTEGER DEFAULT 0')
            if 'platform' not in {row[1] for row in db.execute('PRAGMA table_info(events)')}:
                db.execute("ALTER TABLE events ADD COLUMN platform TEXT NOT NULL DEFAULT 'gpt'")
            if 'quota_group' not in {row[1] for row in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN quota_group TEXT')
            if 'completed_at' not in {row[1] for row in db.execute('PRAGMA table_info(events)')}:
                db.execute('ALTER TABLE events ADD COLUMN completed_at REAL')
            db.execute('CREATE INDEX IF NOT EXISTS usage_platform_at ON events(platform,at)')
            version = db.execute("SELECT value FROM meta WHERE key='parser_version'").fetchone()
            if version is None or version[0] != str(PARSER_VERSION):
                db.executescript("DELETE FROM origins; DELETE FROM events WHERE platform='gpt'; DELETE FROM sources;")
                db.execute("DELETE FROM meta WHERE key='scan'")
                db.execute("INSERT OR REPLACE INTO meta VALUES('parser_version',?)", (str(PARSER_VERSION),))
            saved = db.execute("SELECT value FROM meta WHERE key='scan'").fetchone()
            if saved:
                self._progress.update(json.loads(saved[0]))
            self._progress['running'] = False
            for model, reference in OFFICIAL_PRICES.items():
                exists = db.execute('SELECT 1 FROM prices WHERE model=?', (model,)).fetchone()
                official = db.execute('SELECT 1 FROM price_rules WHERE model=?', (model,)).fetchone()
                if not exists or official:
                    self._put_official(db, model, reference)
            for model, reference in OFFICIAL_AGY_PRICES.items():
                db.execute('INSERT OR REPLACE INTO agy_prices VALUES(?,?,?,?,?)',
                    (model,*(reference['rates'][key] for key in PRICE_FIELDS)))
                long=reference['long_context'] or {}
                db.execute('INSERT OR REPLACE INTO agy_price_rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (model,reference['url'],reference['checked_at'],reference['context_threshold'],
                     *(long.get(key) for key in PRICE_FIELDS),reference['cache_write_ttl'],
                     reference['kind'],reference['source_model'],reference['pricing_basis'],reference['mapping_note'],reference['valid_until']))
        for file in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')):
            if file.exists():
                os.chmod(file, 0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def signal(self):
        with self._progress_lock:
            return dict(self._progress)

    def platform_signal(self,platform='gpt'):
        value=self.signal()
        if platform!='gpt' and self.external_indexer:
            extra=self.external_indexer.signal()
            value['error']='；'.join(e for e in [value.get('error'),extra.get('error')] if e) or None
            value['running']=value['running'] or bool(extra.get('running'))
            if extra.get('pending_sources') and not value['running'] and not value['error']:
                value['error']='AGY 仍有部分记录待同步，下一次同步会继续。'
            value['agy']=extra
        return value

    def _update(self, **values):
        with self._progress_lock:
            self._progress.update(values)

    def _invalidate(self):
        with self._cache_lock:
            self._revision += 1
            self._cache.clear()

    def request_scan(self):
        if not self._stop.is_set() and self._mutex.acquire(blocking=False):
            self._update(running=True, error=None)
            self._thread = threading.Thread(target=self._run_scan, daemon=True, name='codex-usage-index')
            self._thread.start()
        return self.signal()

    def scan(self):
        if not self._stop.is_set() and self._mutex.acquire(blocking=False):
            self._update(running=True, error=None)
            self._run_scan()
        return self.signal()

    def wait(self, timeout=5):
        if self._thread:
            self._thread.join(timeout)

    def close(self):
        if self.external_indexer:
            self.external_indexer.close()
        if self.account_observer:
            self.account_observer.close()
        self._stop.set()
        self.wait(5)

    def _files(self):
        result, seen = [], set()
        for folder in ('sessions', 'archived_sessions'):
            root = self.base/folder
            if not root.is_dir():
                continue
            for path in root.rglob('*.jsonl'):
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    path.resolve().relative_to(root.resolve())
                    st = path.stat()
                    key = (st.st_dev, st.st_ino)
                    if key not in seen:
                        result.append((path, st))
                        seen.add(key)
                except (OSError, ValueError):
                    continue
        return sorted(result, key=lambda item:item[1].st_mtime_ns, reverse=True)

    def _run_scan(self):
        try:
            files = self._files()
            with self._connect() as db:
                known = {row['path']:dict(row) for row in db.execute(
                    'SELECT path,size,mtime,offset,device,inode FROM sources')}
            self._update(files_total=len(files), files_done=0, scanned_bytes=0)
            for index, (path, st) in enumerate(files):
                if self._stop.is_set():
                    break
                try:
                    previous = known.get(str(path))
                    if not (previous and previous['size'] == st.st_size and
                            previous['mtime'] == st.st_mtime_ns and previous['offset'] >= st.st_size and
                            previous['device'] == st.st_dev and previous['inode'] == st.st_ino):
                        self._scan_file(path, st)
                except (OSError, ValueError):
                    self._update(error='部分日志暂时无法读取，已保留其他记录；可再次同步。')
                self._update(files_done=index+1)
            if self.external_indexer and not self._stop.is_set():
                until=time.monotonic()+10
                while not self._stop.is_set():
                    progress=self.external_indexer.scan()
                    if not progress.get('pending_sources') or progress.get('error') or time.monotonic()>=until:break
            with self._connect() as db:
                count = db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
                skipped = db.execute('SELECT COALESCE(SUM(skipped),0) FROM sources').fetchone()[0]
                self._update(indexed_files=count, skipped_records=skipped, updated_at=self.clock(), running=False)
                db.execute("INSERT OR REPLACE INTO meta VALUES('scan',?)", (json.dumps(self.signal()),))
        except Exception:
            self._update(error='本地用量索引暂时失败，已保留上次结果，请重试同步。', running=False)
        finally:
            self._update(running=False)
            self._mutex.release()

    def _scan_file(self, path, st):
        with self._connect() as db:
            row = db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
            if (row and row['size'] == st.st_size and row['mtime'] == st.st_mtime_ns and
                    row['offset'] >= st.st_size and row['device'] == st.st_dev and row['inode'] == st.st_ino):
                return
            if row is None:
                source_id = db.execute('INSERT INTO sources(path) VALUES(?)', (str(path),)).lastrowid
                offset, state = 0, {}
            else:
                source_id, offset = row['id'], row['offset']
                state = json.loads(row['state'])
                changed = (row['device'] != st.st_dev or row['inode'] != st.st_ino or
                           st.st_size < row['size'] or (st.st_size == row['size'] and
                                                        row['mtime'] != st.st_mtime_ns))
                if not changed and offset and state.get('_tail'):
                    with path.open('rb') as check:
                        changed = self._tail(check, offset) != state['_tail']
                if changed:
                    db.execute('DELETE FROM origins WHERE source_id=?', (source_id,))
                    db.execute("DELETE FROM events WHERE platform='gpt' AND NOT EXISTS (SELECT 1 FROM origins WHERE event_id=events.id)")
                    offset, state = 0, {}
            parser = UsageParser(state)
            with path.open('rb') as stream:
                stream.seek(offset)
                lines = 0
                while stream.tell() < st.st_size and not self._stop.is_set():
                    start = stream.tell()
                    raw = stream.readline(MAX_LINE)
                    if not raw:
                        break
                    if len(raw) == MAX_LINE and not raw.endswith(b'\n'):
                        while raw and not raw.endswith(b'\n'):
                            raw = stream.readline(MAX_LINE)
                        parser.state['skipped'] += 1
                    else:
                        if not raw.endswith(b'\n'):
                            try:
                                json.loads(raw)
                            except (ValueError, UnicodeDecodeError):
                                stream.seek(start)
                                break
                        event = parser.feed(raw)
                        if event is not None:
                            db.execute('INSERT OR IGNORE INTO events('+','.join(EVENT_COLUMNS)+') VALUES ('+
                                       ','.join('?' for _ in EVENT_COLUMNS)+')', tuple(event[k] for k in EVENT_COLUMNS))
                            db.execute('INSERT OR IGNORE INTO origins VALUES(?,?)', (source_id, event['id']))
                    offset = stream.tell()
                    lines += 1
                    with self._progress_lock:
                        self._progress['scanned_bytes'] += offset-start
                    if lines % 2000 == 0:
                        parser.state['_tail'] = self._tail(stream, offset)
                        self._checkpoint(db, source_id, st, offset, parser.state)
                        db.commit()
                        self._invalidate()
                parser.state['_tail'] = self._tail(stream, offset)
                self._checkpoint(db, source_id, st, offset, parser.state)
        self._invalidate()

    @staticmethod
    def _tail(stream, offset):
        previous = stream.tell()
        start = max(0, offset-512)
        stream.seek(start)
        result = hashlib.sha256(stream.read(offset-start)).hexdigest()
        stream.seek(previous)
        return result

    @staticmethod
    def _checkpoint(db, source_id, st, offset, state):
        db.execute('UPDATE sources SET device=?,inode=?,size=?,mtime=?,offset=?,state=?,skipped=? WHERE id=?',
                   (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, offset, json.dumps(state), state.get('skipped',0), source_id))

    def set_pricing(self, model, values):
        if not isinstance(model, str) or clean(model) != model or not 0 < len(model) <= 160 or model == 'all':
            raise ValueError('模型名称无效。')
        if values is not None and (not isinstance(values, dict) or set(values) != set(PRICE_FIELDS) or
            any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1000000 for v in values.values())):
            raise ValueError('单价须为0至1000000之间的有限数字。')
        with self._connect() as db:
            if values is None:
                if model in OFFICIAL_PRICES:
                    self._put_official(db, model, OFFICIAL_PRICES[model])
                else:
                    db.execute('DELETE FROM prices WHERE model=?', (model,))
                    db.execute('DELETE FROM price_rules WHERE model=?', (model,))
            else:
                db.execute('INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?)',
                           (model, *(values[key] for key in PRICE_FIELDS)))
                db.execute('DELETE FROM price_rules WHERE model=?', (model,))
        self._invalidate()

    def attribute_accounts(self,claims):
        with self._connect() as db:
            before=db.total_changes
            for claim in claims:
                if (set(claim)!={'id','account','identity'} or
                    not valid_label(claim['account']) or
                    not all(isinstance(claim[key],str) and re.fullmatch('[0-9a-f]{64}',claim[key])
                            for key in ('id','identity'))):
                    raise ValueError('账号归属证据格式无效。')
                db.execute('''INSERT INTO usage_accounts(event_id,account,identity) VALUES(?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET conflict=1
                    WHERE usage_accounts.conflict=0 AND
                    (usage_accounts.account!=excluded.account OR usage_accounts.identity!=excluded.identity)''',
                    (claim['id'],claim['account'],claim['identity']))
            changed=db.total_changes>before
        if changed:
            self._invalidate()

    def attribution_state(self,platform='gpt'):
        unavailable=dict(enabled=False,started_at=None,checked_at=None,error=None)
        gpt=self.account_observer.signal() if self.account_observer else unavailable
        agy=self.external_indexer.signal() if self.external_indexer else unavailable
        if agy.get('attribution_error'):
            agy={**agy,'error':agy['attribution_error']}
        if platform=='gpt':return gpt
        if platform=='agy':return agy
        return dict(enabled=gpt.get('enabled') or agy.get('enabled'),
            started_at=max((r.get('started_at') or 0 for r in (gpt,agy))) or None,
            checked_at=min((r.get('checked_at') or 0 for r in (gpt,agy))) or None,
            error='；'.join(r['error'] for r in (gpt,agy) if r.get('error')) or None)

    @staticmethod
    def _put_official(db, model, reference):
        db.execute('INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?)',
                   (model, *(reference['rates'][key] for key in PRICE_FIELDS)))
        long = reference['long_context'] or {}
        db.execute('INSERT OR REPLACE INTO price_rules VALUES(?,?,?,?,?,?,?,?,?)',
                   (model, reference['url'], reference['checked_at'], reference['context_threshold'],
                    *(long.get(key) for key in PRICE_FIELDS), reference['cache_write_ttl']))

    @staticmethod
    def _pricing_sources(db, prices):
        rules = {row['model']:dict(row) for row in db.execute('SELECT * FROM price_rules')}
        result = {}
        for model in prices:
            rule = rules.get(model)
            official = OFFICIAL_PRICES.get(model)
            result[model] = dict(kind='openai_official' if rule else 'manual',
                url=rule['url'] if rule else None, checked_at=rule['checked_at'] if rule else None,
                tier='standard' if rule else None,
                context_threshold=rule['context_threshold'] if rule else None,
                long_context={key:rule['long_'+key] for key in PRICE_FIELDS}
                    if rule and rule['context_threshold'] else None,
                cache_write_ttl=rule['cache_write_ttl'] if rule else None,
                official_available=official is not None,
                official_url=official['url'] if official else None,
                official_checked_at=official['checked_at'] if official else None)
        return result

    @staticmethod
    def _summary(row):
        result = dict(row)
        total = result['input_tokens']
        result['cache_hit_rate'] = round(100*result['cached_input_tokens']/total, 3) if total else None
        return result

    def snapshot(self, range_name='7d', model='all', timezone='UTC', page=1, limit=30, account='all',start_date=None,end_date=None,platform='gpt'):
        if platform not in ('gpt','agy','all'):
            raise ValueError('平台筛选无效。')
        if range_name not in ('today', 'yesterday', '7d', '30d', '365d', 'all','custom'):
            raise ValueError('时间范围无效。')
        if type(page) is not int or not 1 <= page <= 1000000 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('分页参数无效。')
        if not isinstance(model, str) or len(model) > 160:
            raise ValueError('模型筛选无效。')
        if account not in ('all','unknown') and not valid_label(account):
            raise ValueError('账号筛选无效。')
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError('时区无效。') from None
        now = datetime.fromtimestamp(self.clock(), zone)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = (midnight+timedelta(days=1)).timestamp()
        days = {'today':1, 'yesterday':1, '7d':7, '30d':30, '365d':365}.get(range_name)
        start = (midnight-timedelta(days=days-1)).timestamp() if days else 0
        if range_name=='yesterday':
            last=midnight-timedelta(days=1)
            start,end=last.timestamp(),midnight.timestamp()
            start_date=end_date=last.strftime('%Y-%m-%d')
        if range_name=='custom':
            if any(type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) for value in (start_date,end_date)):
                raise ValueError('请选择完整起止日期。')
            try:
                first=datetime.strptime(start_date,'%Y-%m-%d').replace(tzinfo=zone)
                last=datetime.strptime(end_date,'%Y-%m-%d').replace(tzinfo=zone)
            except ValueError:raise ValueError('日期无效。') from None
            if start_date<'2026-11-09' or first>last or last>midnight:
                raise ValueError('可选日期为2026-11-09至今天，结束不能早于开始。')
            start,end=first.timestamp(),(last+timedelta(days=1)).timestamp()
            days=(last.date()-first.date()).days+1
        elif range_name!='yesterday' and (start_date is not None or end_date is not None):
            raise ValueError('自选日期须使用custom范围。')
        with self._cache_lock:
            revision = self._revision
            cache_key = (range_name,model,timezone,page,limit,account,midnight.timestamp(),revision,start_date,end_date,platform)
            cached = self._cache.get(cache_key)
        if cached is not None:
            return {**cached, 'scan':self.platform_signal(platform), 'attribution':self.attribution_state(platform)}
        where, params = ' WHERE e.at>=? AND e.at<? ', [start, end]
        if platform!='all':
            where+=' AND e.platform=? '
            params.append(platform)
        if model != 'all':
            where += ' AND e.model=? '
            params.append(model)
        account_where,account_params=where,list(params)
        if account != 'all':
            where += ' AND COALESCE(('+ACCOUNT+"),'unknown')=? "
            params.append(account)
        with self._connect() as db:
            db.execute('BEGIN')
            summary = self._summary(db.execute('SELECT '+AGG+JOIN+where, params).fetchone())
            models = [self._summary(row) for row in db.execute('SELECT e.model, '+AGG+JOIN+where+
                        ' GROUP BY e.model ORDER BY total_tokens DESC', params)]
            available = [row[0] for row in db.execute('SELECT DISTINCT model FROM events'+(' WHERE platform=?' if platform!='all' else '')+' ORDER BY model',
                (platform,) if platform!='all' else ())]
            available_accounts = sorted((dict(row) for row in db.execute(
                'SELECT DISTINCT e.platform, '+ACCOUNT+' AS account'+JOIN+
                ' WHERE ('+ACCOUNT+') IS NOT NULL'+(' AND e.platform=?' if platform!='all' else ''),
                (platform,) if platform!='all' else ()) if valid_label(row['account'])),
                key=lambda row:(row['platform'],sort_key(row['account'])))
            accounts=[self._summary(row) for row in db.execute('SELECT e.platform, '+ACCOUNT+' AS account, '+AGG+JOIN+
                        account_where+' GROUP BY e.platform, '+ACCOUNT+' ORDER BY e.platform,account',account_params)]
            price_context = ("CASE WHEN ("+COST+") IS NULL THEN 'unpriced' WHEN e.platform='agy' THEN ar.pricing_basis WHEN r.model IS NULL THEN 'manual' "+
                             "WHEN r.context_threshold IS NOT NULL AND e.input_tokens>r.context_threshold THEN 'long' ELSE 'short' END")
            rows = [dict(row) for row in db.execute('SELECT e.*, '+ACCOUNT+" AS account, CASE WHEN a.conflict=1 THEN 'conflict' "+
                        "WHEN a.account IS NOT NULL THEN 'process' ELSE 'unknown' END AS account_source, "+COST+' AS estimated_cost_usd, '+price_context+
                        ' AS price_context, ar.source_model AS price_model, ar.kind AS price_source, ar.mapping_note AS price_mapping_note, '+
                        'ar.url AS price_url, ar.checked_at AS price_checked_at'+JOIN+where+
                        ' ORDER BY e.at DESC,e.id LIMIT ? OFFSET ?', [*params, limit, (page-1)*limit])]
            for row in rows:row['app']='AGY' if row['platform']=='agy' else 'Codex'
            for row in accounts:row['app']='AGY' if row['platform']=='agy' else 'Codex'
            local_format = '%Y-%m-%d %H:00' if range_name in ('today','yesterday') else '%Y-%m-%d'
            db.create_function('bucket', 1, lambda ts:datetime.fromtimestamp(ts, zone).strftime(local_format))
            groups = {row['label']:dict(row) for row in db.execute(
                'SELECT bucket(e.at) AS label,COUNT(*) AS requests,SUM(e.total_tokens) AS total_tokens,'+
                'SUM('+COST+') AS estimated_cost_usd'+JOIN+where+' GROUP BY label', params)}
            timeline = []
            if range_name == 'all':
                labels = sorted(groups)
            elif range_name in ('today','yesterday'):
                anchor=last if range_name=='yesterday' else midnight
                labels = [(anchor+timedelta(hours=n)).strftime(local_format) for n in range(24)]
            else:
                anchor=last if range_name=='custom' else midnight
                labels = [(anchor-timedelta(days=days-1-n)).strftime(local_format) for n in range(days)]
            for label in labels:
                value = groups.get(label, dict(label=label,requests=0,total_tokens=0,estimated_cost_usd=None))
                value['at'] = datetime.strptime(label, local_format).replace(tzinfo=zone).timestamp()
                timeline.append(value)
            heat_where, heat_params = ' WHERE e.at>=? AND e.at<? ', [(midnight-timedelta(days=364)).timestamp(), end]
            if platform!='all':
                heat_where+=' AND e.platform=? '
                heat_params.append(platform)
            if range_name=='custom':heat_params[0]=max(heat_params[0],start)
            if model != 'all':
                heat_where += ' AND e.model=? '
                heat_params.append(model)
            if account != 'all':
                heat_where += ' AND COALESCE(('+ACCOUNT+"),'unknown')=? "
                heat_params.append(account)
            db.create_function('day', 1, lambda ts:datetime.fromtimestamp(ts, zone).strftime('%Y-%m-%d'))
            heat = {row['date']:dict(row) for row in db.execute('SELECT day(e.at) AS date,COUNT(*) AS requests,'+
                    'SUM(e.total_tokens) AS total_tokens,SUM('+COST+') AS estimated_cost_usd'+JOIN+heat_where+
                    ' GROUP BY date', heat_params)}
            heatmap = []
            for n in range(365):
                day = (midnight-timedelta(days=364-n)).strftime('%Y-%m-%d')
                heatmap.append(heat.get(day, dict(date=day,requests=0,total_tokens=0,estimated_cost_usd=None)))
            prices = {row['model']:{key:row[key] for key in PRICE_FIELDS}
                      for row in db.execute('SELECT * FROM prices')}
            pricing_sources = self._pricing_sources(db, prices)
            agy_prices={row['model']:{key:row[key] for key in PRICE_FIELDS} for row in db.execute('SELECT * FROM agy_prices')}
            agy_sources={row['model']:{**dict(row),'tier':'standard','official_available':True,
                'long_context':{key:row['long_'+key] for key in PRICE_FIELDS} if row['context_threshold'] else None}
                for row in db.execute('SELECT * FROM agy_price_rules')}
            prices_by_platform={'gpt':prices,'agy':agy_prices}
            sources_by_platform={'gpt':pricing_sources,'agy':agy_sources}
            if platform=='agy':prices,pricing_sources=agy_prices,agy_sources
            elif platform=='all':
                prices={**agy_prices,**prices};pricing_sources={**agy_sources,**pricing_sources}
        total = summary['requests']
        result = dict(range=range_name, timezone=timezone, scan=self.platform_signal(platform), summary=summary,
                    models=models, available_models=available, available_accounts=available_accounts,
                    timeline=timeline, heatmap=heatmap,
                    rows=rows, pagination=dict(page=page, limit=limit, total=total,
                                               pages=max(1, math.ceil(total/limit))), pricing=prices,
                    pricing_sources=pricing_sources,pricing_by_platform=prices_by_platform,
                    pricing_sources_by_platform=sources_by_platform,account=account,accounts=accounts,attribution=self.attribution_state(platform),
                    start_date=start_date,end_date=end_date,platform=platform)
        with self._cache_lock:
            if revision == self._revision:
                if len(self._cache) >= 32:
                    self._cache.clear()
                self._cache[cache_key] = result
        return result
