"""Shared, path-safe account labels, ordered like spreadsheet columns."""
from pathlib import Path
import re

LEGACY_LABELS=tuple('ABCDEFGH')
MAX_LABEL_LENGTH=8
RESERVED=frozenset(('S','SAVE','LOGIN','STATUS','BUZZ','AUTO','QUOTA','WEB','ACCOUNTS','REMOVE'))


def valid_label(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Z]{1,8}',value) is not None and value not in RESERVED


def sort_key(label):
    return (len(label),label)


def next_label(occupied):
    occupied=set(occupied)
    number=1
    while True:
        current=number;parts=[]
        while current:
            current,offset=divmod(current-1,26);parts.append(chr(65+offset))
        label=''.join(reversed(parts))
        if len(label)>MAX_LABEL_LENGTH:raise ValueError('没有可用账户编号。')
        if valid_label(label) and label not in occupied:return label
        number+=1


def codex_labels(base):
    labels=set(LEGACY_LABELS)
    for path in Path(base).glob('auth_account_*.json'):
        label=path.name[len('auth_account_'):-len('.json')]
        if valid_label(label):labels.add(label)
    return tuple(sorted(labels,key=sort_key))
