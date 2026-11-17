"""Numeric-only Codex rollout parser; never retain messages or credentials."""
from datetime import datetime
import hashlib
import json
import math

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens')
INTERESTING = (b'"session_meta"', b'"turn_context"', b'"token_count"',
               b'"token_usage_record"', b'"thread_settings_applied"',
               b'"task_started"', b'"turn_aborted"', b'"function_call_output"',
               b'"custom_tool_call_output"')


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp() if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def clean(value, default='unknown'):
    if not isinstance(value, str):
        return default
    value = ''.join(ch for ch in value if ch.isprintable()).strip()
    return value[:160] or default


def counters(value):
    if not isinstance(value, dict) or not all(k in value for k in ('input_tokens', 'output_tokens')):
        return None
    values = [value.get(k, 0) for k in FIELDS]
    if any(type(v) is not int or not 0 <= v <= 10**15 for v in values):
        return None
    return values


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':'), sort_keys=True).encode()).hexdigest()


class UsageParser:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.state.setdefault('skipped', 0)

    def _event(self, values, at, source, key):
        if at is None or not any(values):
            return None
        s = self.state
        i, c, w, o, r = values
        c = min(c, i)
        w = min(w, i-c)
        r = min(r, o)
        start = s.get('boundary')
        duration = (at-start)*1000 if type(start) in (int, float) and at > start else None
        if duration is not None and (not math.isfinite(duration) or duration > 3600000):
            duration = None
        speed = o/(duration/1000) if duration and duration >= 100 and o >= 100 else None
        s['boundary'] = at
        return {'id': digest(key), 'at': at, 'model': s.get('model', 'unknown'),
                'provider': s.get('provider', 'unknown'),
                'session': digest(s.get('session', 'unknown'))[:16], 'source': source,
                'input_tokens': i, 'cached_input_tokens': c, 'cache_write_input_tokens': w,
                'uncached_input_tokens': i-c-w, 'output_tokens': o,
                'reasoning_output_tokens': r, 'total_tokens': i+o,
                'duration_ms': round(duration, 2) if duration else None,
                'speed_tps': round(speed, 3) if speed is not None else None}

    def feed(self, raw):
        if not any(marker in raw for marker in INTERESTING):
            return None
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict) or not isinstance(event.get('payload'), dict):
            return None
        kind, p, s = event.get('type'), event['payload'], self.state
        at = stamp(event.get('timestamp'))
        if kind == 'session_meta':
            s['session'] = clean(p.get('id') or p.get('session_id'))
            s['provider'] = clean(p.get('model_provider'))
            s['created'] = stamp(p.get('timestamp')) or at
            s['inherited'] = bool(p.get('forked_from_id') or p.get('history_base') or
                                   p.get('history_mode') == 'continuation')
            return None
        if kind == 'turn_context':
            s['model'] = clean(p.get('model'), s.get('model', 'unknown'))
            s['boundary'] = at
            return None
        if kind == 'response_item':
            if p.get('type') in ('function_call_output', 'custom_tool_call_output'):
                s['boundary'] = at
            return None
        if kind == 'event_msg':
            if p.get('type') in ('task_started', 'turn_aborted'):
                s['boundary'] = at if p['type'] == 'task_started' else None
                return None
            if p.get('type') == 'thread_settings_applied':
                settings = p.get('thread_settings')
                if isinstance(settings, dict):
                    s['model'] = clean(settings.get('model'), s.get('model', 'unknown'))
                return None
            if p.get('type') != 'token_count':
                return None
        if kind not in ('token_usage_record', 'event_msg'):
            return None
        if at is None:
            s['skipped'] += 1
            return None
        if s.get('inherited') and s.get('created') is not None and at < s['created']:
            return None
        if kind == 'token_usage_record':
            if p.get('thread_id') and p['thread_id'] != s.get('session'):
                return None
            values = counters(p.get('usage'))
            if values is None:
                s['skipped'] += 1
                return None
            key = ['record', s.get('session'), clean(p.get('response_id'), '') or [at, values]]
            record_key = digest(key)
            seen = s.setdefault('records_seen', {})
            duplicate_record = record_key in seen
            if not duplicate_record:
                pending = s.setdefault('pending_records', [])
                pending.append({'usage':values, 'at':at})
                if len(pending) > 128:
                    del pending[:-128]
                seen[record_key] = True
                if len(seen) > 256:
                    del seen[next(iter(seen))]
            boundary = s.get('boundary')
            result = self._event(values, at, 'record', key)
            if duplicate_record:
                s['boundary'] = boundary
            return result
        info = p.get('info')
        if not isinstance(info, dict):
            return None
        s['model'] = clean(info.get('model') or info.get('model_name') or p.get('model'),
                           s.get('model', 'unknown'))
        last, total = counters(info.get('last_token_usage')), counters(info.get('total_token_usage'))
        if last is None and total is None:
            return None
        signature = digest([last, total])
        sources = s.setdefault('snapshots', {})
        source = clean(p.get('limit_id') or (p.get('rate_limits') or {}).get('limit_id')
                       if isinstance(p.get('rate_limits', {}), dict) else p.get('limit_id'), 'default')
        recent = s.setdefault('recent_snapshots', {})
        lanes = s.setdefault('lane_totals', {})
        lane_total = lanes.get(source)
        reset = total is not None and lane_total is not None and (
            total[0] < lane_total[0] or total[3] < lane_total[3])
        repeated = total is not None and (signature in recent or signature == sources.get(source))
        previous = s.get('total')
        pending = s.setdefault('pending_records', [])
        pending[:] = [item for item in pending if at-item['at'] <= 3600]
        covered = False
        if not repeated or reset:
            for index, item in enumerate(pending):
                if last == item['usage'] or last is not None and not any(last):
                    del pending[index]
                    covered = True
                    break
            if last is None and total is not None and not covered and pending:
                candidate = None
                if previous is not None and all(a >= b for a,b in zip(total,previous)):
                    candidate = [a-b for a,b in zip(total,previous)]
                elif previous is None and not s.get('inherited'):
                    candidate = total
                accumulated = [0]*len(FIELDS)
                for index, item in enumerate(pending):
                    accumulated = [a+b for a,b in zip(accumulated,item['usage'])]
                    if candidate is not None and accumulated[:4] == candidate[:4]:
                        del pending[:index+1]
                        covered = True
                        break
                if candidate is None and s.get('inherited') and previous is None:
                    pending.clear()
                    covered = True
                    s['skipped'] += 1
        if reset:
            # A same-lane reset is ambiguous in old snapshots. Rebase without
            # charging its first snapshot, then accept subsequent increments.
            s['recent_snapshots'] = recent = {}
            s['snapshots'] = sources = {}
            s['lane_totals'] = lanes = {}
            s['skipped'] += 1
        if total is not None:
            if reset or previous is None or not repeated and total[0] >= previous[0] and total[3] >= previous[3]:
                s['total'] = total
            sources[source] = signature
            recent[signature] = True
            lanes[source] = total
            if len(recent) > 256:
                del recent[next(iter(recent))]
            if len(sources) > 32:
                s['snapshots'] = {source: signature}
                s['lane_totals'] = {source: total}
        s['previous_snapshot'] = signature
        if s.pop('pending_record', False) or covered or repeated or reset:
            return None
        if last is not None:
            values, mode = last, 'snapshot'
        elif previous is None:
            if s.get('inherited'):
                s['skipped'] += 1
                return None
            values, mode = total, 'delta'
        elif any(a < b for a, b in zip(total, previous)):
            s['skipped'] += 1
            return None
        else:
            values, mode = [a-b for a, b in zip(total, previous)], 'delta'
        return self._event(values, at, mode, ['snapshot', s.get('session'), at, signature])
