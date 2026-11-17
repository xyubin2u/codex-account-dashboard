#!/usr/bin/env python3
"""Run concurrent Codex accounts with separate auth and shared local history."""
import argparse
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from account_labels import valid_label,codex_labels
from core import ACCOUNTS, SwitchError, account_path, atomic_write, check_environment, load_auth


class MultiError(Exception):
    pass


SHARED_NAMES = (
    'AGENTS.md', 'config.toml', 'sessions', 'session_index.jsonl', 'history.jsonl',
    'skills', 'memories', 'plugins', 'rules', 'superpowers', 'shell_snapshots',
    'attachments', 'thread-writer-locks', 'packages', 'cache', 'generated_images',
    'mcp-oauth-locks', 'hooks.json',
)


def _label(value):
    label = value.upper()
    if not valid_label(label):
        raise MultiError('账户只能是 A、B、C、D、E、F、G、H。')
    return label


def _rank(path, data):
    stamp = data.get('last_refresh') or ''
    try:
        refreshed = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError):
        refreshed = 0.0
    return (refreshed, path.stat().st_mtime_ns)


def _copy_auth(path, data):
    atomic_write(path, data)
    os.chmod(path, 0o600)


def _auth_signature(path):
    try:
        if path.is_symlink():
            raise MultiError('凭据路径是符号链接，拒绝同步。')
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    except OSError:
        raise MultiError('凭据在核对期间发生变化，请稍后重新启动。') from None


def _matching_default(base, identity):
    """Default login is a read-only recovery source, never a switch target."""
    path = Path(base) / 'auth.json'
    if path.is_symlink() or not path.is_file():
        return None
    signature = _auth_signature(path)
    try:
        data, actual = load_auth(path)
    except SwitchError:
        return None
    if _auth_signature(path) != signature:
        raise MultiError('默认登录正在更新，请稍后重新启动；没有覆盖凭据。')
    if actual != identity:
        return None
    return path, data, signature


def check_shared_auth(base, home, label):
    """Never replace credentials held by an already running profile."""
    data, identity = load_auth(Path(home) / 'auth.json')
    default = _matching_default(base, identity)
    if default is None:
        return
    path, current, _ = default
    left = _rank(path, current)[0]
    right = _rank(Path(home) / 'auth.json', data)[0]
    if (current['tokens']['refresh_token'] != data['tokens']['refresh_token']
            and left >= right):
        raise MultiError(f'账户 {label} 的旧窗口仍在运行，默认登录有更新或冲突的凭据。'
                         f'请仅退出该账户的旧窗口后重新启动；其他账户无需退出。')


def prepare(base, profiles, label):
    """Create one persistent auth home per account; link only shared state."""
    label = _label(label)
    base = Path(base).resolve()
    profiles = Path(profiles).resolve()
    source = account_path(base, label)
    if source.is_symlink():
        raise MultiError(f'账户 {label} 的凭据是符号链接，拒绝使用。')
    if not source.exists():
        raise MultiError(f'账户 {label} 尚未登录，请先运行 codex-switch login {label}。')
    data, _ = load_auth(source)
    profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(profiles, 0o700)
    home = profiles / label
    if home.is_symlink():
        raise MultiError(f'账户 {label} 的目录是符号链接，拒绝使用。')
    home.mkdir(mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    for name in SHARED_NAMES:
        item = base / name
        link = home / name
        if not item.exists() or link.is_symlink() and link.resolve() == item.resolve():
            continue
        if link.exists() or link.is_symlink():
            raise MultiError(f'{link} 已存在且不是预期共享路径，请先人工核查。')
        link.symlink_to(item)
    dest = home / 'auth.json'
    if dest.is_symlink():
        raise MultiError(f'账户 {label} 的凭据是符号链接，拒绝使用。')
    if not dest.exists():
        _copy_auth(dest, data)
    return home


def sync_auth(base, home, label):
    """Keep the fresher local refresh token, never replace another identity."""
    label = _label(label)
    source = account_path(Path(base), label)
    dest = Path(home) / 'auth.json'
    if source.is_symlink() or dest.is_symlink():
        raise MultiError('凭据路径是符号链接，拒绝同步。')
    snapshots = {p: _auth_signature(p) for p in (source, dest)}
    source_data, source_id = load_auth(source)
    dest_data, dest_id = load_auth(dest)
    if source_id != dest_id:
        raise MultiError(f'账户 {label} 的保存凭据与独立目录身份不一致，未覆盖任何文件。')
    candidates = [(source, source_data), (dest, dest_data)]
    default = _matching_default(base, source_id)
    if default is not None:
        default_path, default_data, signature = default
        snapshots[default_path] = signature
        candidates.append((default_path, default_data))
    latest = max(_rank(path, data)[0] for path, data in candidates)
    finalists = [data for path, data in candidates if _rank(path, data)[0] == latest]
    if len({data['tokens']['refresh_token'] for data in finalists}) != 1:
        raise MultiError('同一账户的登录凭据时间相同但令牌不同，无法判断先后；未覆盖任何文件。')
    winner = finalists[0]
    for path, signature in snapshots.items():
        if _auth_signature(path) != signature:
            raise MultiError('凭据在核对期间发生变化，请稍后重新启动；未覆盖任何文件。')
    for path, data in ((source, source_data), (dest, dest_data)):
        if data != winner:
            for check_path, signature in snapshots.items():
                if _auth_signature(check_path) != signature:
                    raise MultiError('凭据在同步期间发生变化，已停止；请重新启动核对。')
            _copy_auth(path, winner)
            snapshots[path] = _auth_signature(path)


def command(label, session=None):
    return [sys.executable, str(Path(__file__).resolve()), _label(label)] + ([session] if session else [])


def _codex_command(binary, session):
    args = [binary, '--no-daemon', '--dangerously-bypass-approvals-and-sandbox',
            '-c', 'cli_auth_credentials_store="file"']
    if session == '--new':
        return args
    return args + ['resume'] + ([session] if session else ['--all'])


def run_codex(command, env, ownership=None):
    """Retry only the terminal startup routing timeout, before a session opens."""
    bootstrap_error = ('Error: account/read failed during TUI bootstrap: '
                       'account/read failed: workspace routing discovery timed out (code -32603)')
    for attempt in range(1, 4):
        tail = bytearray()
        from auto_child import run_child
        observer = None
        if ownership:
            from process_session_owner import ProcessSessionObserver
            observer = ProcessSessionObserver(**ownership)
        returncode = run_child(command, env, stderr_tail=tail, observer=observer)
        terminal_text = re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]', b'', bytes(tail))
        last_line = terminal_text.decode('utf-8', errors='replace').strip().splitlines()
        if returncode != 1 or not last_line or last_line[-1] != bootstrap_error:
            return returncode
        if attempt == 3:
            print('\n账户启动已尝试 3 次，路由查询仍超时。请检查到 ChatGPT 的代理连接；'
                  '此错误不代表需要重新登录。', file=sys.stderr, flush=True)
            return returncode
        print(f'\nCodex 启动前路由查询超时，正在重试（{attempt + 1}/3）…',
              file=sys.stderr, flush=True)
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(prog='codex-account',
                                     description='并行打开独立账户；所有账户共享原来的本地对话历史。')
    parser.add_argument('account', help='A–H；或 status')
    parser.add_argument('session', nargs='?', help='原会话 ID；省略时显示全部对话')
    parser.add_argument('--new', action='store_true', help='在指定账户下新开对话')
    args = parser.parse_args()
    base = Path(os.environ.get('CODEX_MULTI_BASE', os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))).expanduser().resolve()
    profiles = Path(os.environ.get('CODEX_MULTI_PROFILES',
                                   str(Path.home() / '.local/share/codex-switch/accounts'))).expanduser().resolve()
    if args.account.lower() == 'status':
        for label in codex_labels(base):
            try:
                load_auth(account_path(base, label))
                print(f'{label}：已保存本地凭据' + ('；独立目录已建立' if (profiles / label / 'auth.json').exists() else ''))
            except SwitchError:
                print(f'{label}：未保存或凭据无效')
        print('本地凭据可读不代表服务端账户额度可用。')
        return 0
    label = _label(args.account)
    if args.new and args.session:
        parser.error('--new 不能同时指定旧会话。')
    check_environment(os.environ)
    binary = shutil.which('codex')
    if not binary:
        raise MultiError('找不到 codex 命令。')
    help_result = subprocess.run([binary, '--help'], capture_output=True, text=True)
    if help_result.returncode or '--no-daemon' not in help_result.stdout:
        raise MultiError('本机 Codex 不支持 --no-daemon，无法隔离后台账户。')
    # Exclusive while preparing, shared for concurrent windows of the same account.
    profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = profiles / f'.{label}.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            home = prepare(base, profiles, label)
            sync_auth(base, home, label)
            fcntl.flock(lock, fcntl.LOCK_SH)
        except BlockingIOError:
            # Another window already owns this account home; join it without replacing auth.
            fcntl.flock(lock, fcntl.LOCK_SH)
            home = profiles / label
            _, saved_id = load_auth(account_path(base, label))
            _, active_id = load_auth(home / 'auth.json')
            if saved_id != active_id:
                raise MultiError(f'账户 {label} 的独立目录身份不一致，拒绝并行启动。')
            check_shared_auth(base, home, label)
        env = os.environ.copy()
        env['CODEX_MULTI_BASE'] = str(base)
        env['CODEX_MULTI_PROFILES'] = str(profiles)
        env['CODEX_HOME'] = str(home)
        env['CODEX_SQLITE_HOME'] = str(base)
        print(f'已选择账户 {label}（独立登录，共享原有对话历史）。', flush=True)
        try:
            from account_display import account_title
            command_args = _codex_command(binary, '--new' if args.new else args.session)
            with account_title(label, base, command_args[1:]) as display_args:
                return run_codex([command_args[0], *display_args, *command_args[1:]], env)
        finally:
            # The last exiting window persists its refreshed credential snapshot.
            fcntl.flock(lock, fcntl.LOCK_UN)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                sync_auth(base, home, label)
            except BlockingIOError:
                pass
            except (SwitchError, MultiError) as exc:
                print('退出后凭据同步未完成：' + str(exc), file=sys.stderr)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (MultiError, SwitchError, OSError) as exc:
        print('codex-account：' + (str(exc) if isinstance(exc, (MultiError, SwitchError)) else '本地文件操作失败。'),
              file=sys.stderr)
        sys.exit(1)
