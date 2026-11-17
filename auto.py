#!/usr/bin/env python3
"""Adapt a pinned private cx runtime to this machine's A–H account launcher."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from account_labels import valid_label,codex_labels
from core import ACCOUNTS, SwitchError, account_path, check_environment, load_auth
from multi import MultiError
from auto_child import account_lock, run_child
from auto_pool import read_pool, PoolError
from auto_threshold import read_thresholds, ThresholdError, NAME as THRESHOLD_NAME

HERE = Path(__file__).resolve().parent


def main():
    check_environment(os.environ)
    base = Path(os.environ.get('CODEX_MULTI_BASE', str(Path.home() / '.codex'))).resolve()
    profiles = Path(os.environ.get('CODEX_MULTI_PROFILES', str(Path.home() / '.local/share/codex-switch/accounts'))).resolve()
    original_path = os.environ.get('PATH', os.defpath)
    binary, node = shutil.which('codex'), shutil.which('node')
    if not binary or not node:
        raise SwitchError('自动模式需要已有的 codex 和 Node.js。')
    identities = set()
    accounts = []
    is_quota = sys.argv[1:] == ['quota']
    quota_target = os.environ.get('CODEX_QUOTA_ACCOUNT', '') if is_quota else ''
    if quota_target and not valid_label(quota_target):
        raise SwitchError('额度查询账户只能是 A–H。')
    if quota_target and not account_path(base, quota_target).exists():
        raise SwitchError(f'账户 {quota_target} 尚未登记，请先完成登录。')
    preferred = os.environ.get('CODEX_AUTO_PREFER', '')
    selected_pool = set(codex_labels(base)) if is_quota else read_pool(base)
    if not is_quota:
        read_thresholds(base)
    if preferred and preferred not in selected_pool:
        raise SwitchError(f'优先账户 {preferred} 未加入 AUTO 号池，请先在额度看板中开启。')
    for label in codex_labels(base):
        if quota_target and label != quota_target:
            continue
        if label not in selected_pool:
            continue
        if not account_path(base, label).exists():
            continue
        with account_lock(base, profiles, label) as home:
            identity = load_auth(home / 'auth.json')[1]
            if identity in identities:
                raise SwitchError('账户登记含重复身份，自动模式已停止。')
            identities.add(identity)
            accounts.append(f'{label}={home}')
    if not accounts:
        raise SwitchError('AUTO 号池中没有已登记账户，请在额度看板中开启或登录账户。' if not is_quota else '没有已登记账户，请先使用 codex-switch login。')
    if preferred and not any(item.startswith(preferred + '=') for item in accounts):
        raise SwitchError(f'优先账户 {preferred} 尚未登记，请先完成登录。')
    env = os.environ.copy()
    # AUTO must not inherit a forced account or mutate the shared config/goals.
    for key in ('CX_ACCOUNT', 'CX_ACCOUNT_COUNT', 'CODEX_THREAD_ID'):
        env.pop(key, None)
    env.update(CODEX_MULTI_BASE=str(base), CODEX_MULTI_PROFILES=str(profiles),
               CODEX_SQLITE_HOME=str(base), CODEX_AUTO_REAL_CODEX=binary,
               CODEX_AUTO_ORIGINAL_PATH=original_path,
               PATH=str(HERE / 'private-bin') + os.pathsep + original_path,
               CX_ACCOUNT_HOMES=','.join(accounts), CX_NO_TRUST='1',
               CX_AUTO_RESUME_GOAL='0', CX_LOCAL_SAFE_RESUME='1')
    if not is_quota:
        env['CX_LOCAL_THRESHOLD_FILE'] = str(base / THRESHOLD_NAME)
    else:
        env.pop('CX_LOCAL_THRESHOLD_FILE', None)
    env['CX_LOCAL_PREFER_ACCOUNT'] = preferred
    return run_child([node, str(HERE / 'vendor/codex-multi-account/bin/cx'), *sys.argv[1:]], env)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (SwitchError, MultiError, PoolError, ThresholdError) as exc:
        print('[auto] ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print('[auto] 无法读取账户或启动本地程序。', file=sys.stderr)
        sys.exit(1)
