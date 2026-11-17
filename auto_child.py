#!/usr/bin/env python3
"""Private Codex child bridge: retain the existing account locks and refresh sync."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from account_labels import valid_label
from core import ACCOUNTS, SwitchError, account_path, check_environment, load_auth
from multi import MultiError, prepare, sync_auth, check_shared_auth


@contextmanager
def account_lock(base, profiles, label):
    profiles.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(profiles / f'.{label}.lock', os.O_CREAT | os.O_RDWR, 0o600)
    home = profiles / label
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            home = prepare(base, profiles, label)
            sync_auth(base, home, label)
            fcntl.flock(lock, fcntl.LOCK_SH)
        except BlockingIOError:
            fcntl.flock(lock, fcntl.LOCK_SH)
            if home.is_symlink() or (home / 'auth.json').is_symlink():
                raise MultiError(f'{label} 的账户或凭据路径异常。')
            if load_auth(account_path(base, label))[1] != load_auth(home / 'auth.json')[1]:
                raise MultiError(f'{label} 的保存凭据与独立目录身份不一致。')
            check_shared_auth(base, home, label)
        try:
            yield home
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                sync_auth(base, home, label)
            except BlockingIOError:
                pass
            except (SwitchError, MultiError, OSError):
                print(f'[auto] {label} 退出后凭据同步未完成。', file=sys.stderr)


def run_child(command, env, stderr_tail=None, observer=None):
    """Keep stdin/stdout attached; forward termination and reap our own child."""
    child = None
    terminating = None
    timer = None
    reader = None
    output = sys.stderr

    def observe(method, *args):
        nonlocal observer
        if observer:
            try:
                getattr(observer, method)(*args)
            except Exception:
                # An observer must never change the child's exit or signals.
                observer = None

    def forward_stderr():
        while chunk := child.stderr.read1(4096):
            stderr_tail.extend(chunk)
            del stderr_tail[:-8192]
            if hasattr(output, 'buffer'):
                output.buffer.write(chunk)
                output.buffer.flush()
            else:
                output.write(chunk.decode('utf-8', errors='replace'))
                output.flush()

    def kill_if_running():
        if child is not None and child.poll() is None:
            child.kill()

    def forward(signum, _frame):
        nonlocal terminating, timer
        terminating = signum
        if child is not None and child.poll() is None:
            observe('freeze')
            child.send_signal(signum)
            if timer is None:
                timer = threading.Timer(8 if Path(command[0]).name in ('node', 'python3', 'python') else 3, kill_if_running)
                timer.daemon = True
                timer.start()

    saved = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        child = subprocess.Popen(command, env=env, stderr=subprocess.PIPE if stderr_tail is not None else None)
        observe('start', child.pid)
        if stderr_tail is not None:
            reader = threading.Thread(target=forward_stderr, daemon=True)
            reader.start()
        if terminating:
            forward(terminating, None)
        code = child.wait()
        observe('close')
        if reader:
            reader.join(timeout=2)
            if reader.is_alive():
                stderr_tail.clear()  # Incomplete output cannot authorize a retry.
        return 128 + terminating if terminating else (code if code >= 0 else 128 - code)
    finally:
        if timer:
            timer.cancel()
        observe('close')
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for s, handler in saved.items():
            signal.signal(s, handler)
        if reader and not reader.is_alive():
            child.stderr.close()


def main():
    check_environment(os.environ)
    base = Path(os.environ['CODEX_MULTI_BASE']).resolve()
    profiles = Path(os.environ['CODEX_MULTI_PROFILES']).resolve()
    requested = Path(os.environ['CODEX_HOME']).absolute()
    label = requested.name
    if not valid_label(label) or requested != profiles / label:
        raise MultiError('自动入口只能调用已登记的 A–H 账户目录。')
    binary = os.environ['CODEX_AUTO_REAL_CODEX']
    env = os.environ.copy()
    env['PATH'] = env['CODEX_AUTO_ORIGINAL_PATH']
    env['CODEX_SQLITE_HOME'] = str(base)
    env.pop('CODEX_THREAD_ID', None)
    with account_lock(base, profiles, label) as home:
        args = sys.argv[1:]
        if 'app-server' not in args:
            from account_display import account_title
            with account_title(label, base, args) as display_args:
                from multi import run_codex
                ownership = None
                if env.get('CX_LOCAL_SESSION_PROOF') and env.get('CX_LOCAL_SESSION_NONCE'):
                    ownership = dict(base=base, home=home, proof=env['CX_LOCAL_SESSION_PROOF'],
                        nonce=env['CX_LOCAL_SESSION_NONCE'], kind=env.get('CX_LOCAL_SESSION_KIND', 'cli'))
                return run_codex([binary, '--no-daemon', '-c', 'cli_auth_credentials_store="file"', *display_args, *args], env,
                                 ownership=ownership)
        return run_child([binary, '--no-daemon', '-c', 'cli_auth_credentials_store="file"', *args], env)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (SwitchError, MultiError) as exc:
        print('[auto] ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except (OSError, KeyError):
        print('[auto] 子进程启动或本地账户路径检查失败。', file=sys.stderr)
        sys.exit(1)
