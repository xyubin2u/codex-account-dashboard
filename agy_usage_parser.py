"""Bounded, numeric-only decoder for native AGY generation metadata.

Schema evidence: official local CLI SHA256
9db59b6371107b939befa9c507c0a24989fda974e75d81d8529f3689ef7322e5.
Embedded protobuf DescriptorProto offsets (bytes in that artifact):
ModelUsageStats 112600559; CortexStepGeneratorMetadata 112749584;
ChatModelMetadata 112751954; ChatStartMetadata 112753989;
CortexStepMetadata 112755577. This module does not execute the CLI.

gen.data[1] = ChatModelMetadata; chat[4] = ModelUsageStats.
Usage input (2), cache write (4), and cache read (5) are separate counters;
native records frequently have read > input. Output (3) includes thinking (9).
Main usage equals the sum of retry usage in observed records: never add both.
Steps' model_usage duplicates generation usage: steps supply timestamps only.
No credential, prompt, arbitrary custom metadata, or account is returned.
"""
import hashlib
import math
import re

MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_FIELDS = 16384
MAX_STEPS = 4096
MAX_TOKENS = 10 ** 15
_MODEL = re.compile(r'(?:gemini|claude|gpt|o[134])[-a-zA-Z0-9_.]{0,120}\Z')


class InvalidMetadata(ValueError):
    pass


def _varint(data, pos):
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise InvalidMetadata('truncated integer')
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise InvalidMetadata('integer overflow')
        result |= (byte & 127) << shift
        if byte < 128:
            return result, pos
    raise InvalidMetadata('integer overflow')


def _fields(value):
    if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) > MAX_BLOB_BYTES:
        raise InvalidMetadata('metadata bound')
    value = memoryview(value)
    result = {}
    pos = count = 0
    while pos < len(value):
        key, pos = _varint(value, pos)
        tag, wire = key >> 3, key & 7
        count += 1
        if not 1 <= tag < 2 ** 29 or count > MAX_FIELDS:
            raise InvalidMetadata('field bound')
        if wire == 0:
            item, pos = _varint(value, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            item = value[pos:pos + size]
            pos += size
        elif wire == 2:
            size, pos = _varint(value, pos)
            item = value[pos:pos + size]
            pos += size
        else:
            raise InvalidMetadata('unsupported wire type')
        if pos > len(value):
            raise InvalidMetadata('truncated field')
        result.setdefault(tag, []).append((wire, item))
    return result


def _one(fields, tag, wire, default=None):
    values = fields.get(tag)
    if values is None:
        return default
    if len(values) != 1 or values[0][0] != wire:
        raise InvalidMetadata('ambiguous field')
    return values[0][1]


def _message(fields, tag):
    value = _one(fields, tag, 2)
    return _fields(value) if value is not None else {}


def _counter(fields, tag):
    result = _one(fields, tag, 0, 0)
    if result > MAX_TOKENS:
        raise InvalidMetadata('counter bound')
    return result


def _timestamp(value):
    fields = _fields(value)
    seconds = _one(fields, 1, 0, 0)
    nanos = _one(fields, 2, 0, 0)
    if not 946684800 <= seconds <= 4102444800 or not 0 <= nanos < 10 ** 9:
        raise InvalidMetadata('timestamp bound')
    return seconds + nanos / 10 ** 9


def _duration(fields, tag):
    value = _one(fields, tag, 2)
    if value is None:
        return None
    fields = _fields(value)
    seconds = _one(fields, 1, 0, 0)
    nanos = _one(fields, 2, 0, 0)
    if seconds > 3600 or nanos >= 10 ** 9:
        raise InvalidMetadata('duration bound')
    return seconds + nanos / 10 ** 9


def _indices(fields):
    result = []
    for wire, value in fields.get(2, []):
        if wire == 0:
            values = [value]
        elif wire == 2:
            values = []
            pos = 0
            while pos < len(value):
                item, pos = _varint(value, pos)
                values.append(item)
                if len(values) > MAX_STEPS:
                    raise InvalidMetadata('step bound')
        else:
            raise InvalidMetadata('step wire')
        result.extend(values)
        if len(result) > MAX_STEPS or any(v > 2 ** 31 - 1 for v in values):
            raise InvalidMetadata('step bound')
    return list(dict.fromkeys(result))


def generation_step_indices(blob):
    """Return bounded referenced step indices; malformed metadata gives []."""
    try:
        return _indices(_fields(blob))
    except (InvalidMetadata, TypeError, ValueError):
        return []


def generation_event_id(trajectory_id, gen_idx):
    """Stable key, independent of mutable counters and source filesystem path."""
    return hashlib.sha256(('agy\0' + trajectory_id + '\0' + str(gen_idx)).encode()).hexdigest()


def _model(chat, usage):
    for tag in (22, 19):
        value = _one(chat, tag, 2)
        if value is not None and len(value) <= 128:
            try:
                model = bytes(value).decode('ascii')
            except UnicodeDecodeError:
                continue
            if _MODEL.fullmatch(model):
                return model
    enum = _one(chat, 3, 0, _one(usage, 1, 0, 0))
    return 'agy-model-' + str(enum) if enum else 'unknown'


def _quota_group(model, chat):
    # Exact same-source model mappings verified by the caller against native
    # official quota metadata. Unknown model variants must retain no group.
    if re.fullmatch(r'gemini-4-argon-(?:high|medium|low)', model):
        return 'argon', 'response_model'
    if model.startswith('gemini-') and 'argon' not in model:
        return 'gemini', 'response_model'
    if (re.fullmatch(r'claude-(?:opus|sonnet)-5-5-(?:high|medium|low)', model)
            or model == 'gpt-oss-120b-medium'):
        return '3p', 'response_model'
    # The backend records "trial" rather than an effort-specific Argon ID.
    # Its explicit native display name can still establish the shared family.
    display = _one(chat, 21, 2)
    if model == 'gemini-4-argon-trial' and display is not None and len(display) < 80:
        if bytes(display) in (b'Gemini 4 Argon (High)', b'Gemini 4 Argon (Medium)',
                              b'Gemini 4 Argon (Low)'):
            return 'argon', 'model_display_name'
    return None, None


def parse_generation(trajectory_id, gen_idx, blob, step_metadata=None):
    """Decode one generation into an event, or None without reliable usage/time.

    ``step_metadata`` maps referenced steps.idx to steps.metadata bytes. ``at``
    is the invocation's first generated step creation timestamp, with native
    ChatStartMetadata.created_at preferred if present. Read usage only once.
    No account or quota-pool attribution can be established by this record.
    """
    try:
        if (not isinstance(trajectory_id, str) or not 1 <= len(trajectory_id) <= 512
                or type(gen_idx) is not int or not 0 <= gen_idx < 2 ** 31):
            return None
        gen = _fields(blob)
        chat = _message(gen, 1)
        usage = _message(chat, 4)
        raw_input, output, write, read, thinking = (
            _counter(usage, t) for t in (2, 3, 4, 5, 9))
        if thinking > output or not any((raw_input, output, write, read)):
            return None
        total_input = raw_input + read + write
        if total_input > MAX_TOKENS:
            return None
        start = _message(chat, 9)
        timestamp = _one(start, 4, 2)
        at = _timestamp(timestamp) if timestamp is not None else None
        time_source = 'chat_start_created_at' if at is not None else None
        indices = _indices(gen)
        if at is None:
            for idx in sorted(indices):
                value = (step_metadata or {}).get(idx)
                if value is None:
                    continue
                step = _fields(value)
                timestamp = _one(step, 1, 2)
                if timestamp is not None:
                    at = _timestamp(timestamp)
                    time_source = 'referenced_step_created_at'
                    break
        if at is None:
            return None
        # This is the native completion of generated output, not tool execution
        # completion (step[8]). Require every referenced step, otherwise retain
        # unknown and let capacity block an interval with incomplete evidence.
        ended = []
        for idx in indices:
            value = (step_metadata or {}).get(idx)
            if value is None:
                break
            try:
                value = _one(_fields(value), 7, 2)
                if value is None:
                    break
                ended.append(_timestamp(value))
            except InvalidMetadata:
                break
        completed_at = max(ended) if indices and len(ended) == len(indices) else None
        if completed_at is not None and completed_at < at:
            completed_at = None
        first = _duration(chat, 11)
        stream = _duration(chat, 12)
        duration = (first + stream) if first is not None and stream is not None else None
        if duration is not None and not 0 < duration <= 3600:
            duration = None
        speed = output / duration if duration and output else None
        model = _model(chat, usage)
        group, group_source = _quota_group(model, chat)
        return {
            'id': generation_event_id(trajectory_id, gen_idx), 'at': at,
            'model': model, 'provider': 'antigravity',
            'platform': 'agy', 'quota_group': group,
            'session': hashlib.sha256(('agy\0' + trajectory_id).encode()).hexdigest()[:16],
            'source': 'agy_gen_metadata', 'input_tokens': total_input,
            'completed_at': completed_at,
            'cached_input_tokens': read, 'cache_write_input_tokens': write,
            'uncached_input_tokens': raw_input, 'output_tokens': output,
            'reasoning_output_tokens': thinking, 'total_tokens': total_input + output,
            'duration_ms': round(duration * 1000, 2) if duration else None,
            'speed_tps': round(speed, 3) if speed is not None and math.isfinite(speed) else None,
            'evidence': {'schema': 1, 'timestamp_source': time_source,
                         'api_provider_enum': _one(usage, 6, 0, 0),
                         'model_enum': _one(chat, 3, 0, 0),
                         'generation_index': gen_idx, 'referenced_steps': len(indices),
                         'ended_at': completed_at,
                         'end_timestamp_source': 'all_referenced_steps_finished_generating_at'
                             if completed_at is not None else None,
                         'quota_group_source': group_source},
        }
    except (InvalidMetadata, TypeError, ValueError, OverflowError):
        return None
