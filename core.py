#!/usr/bin/env python3
"""Switch local ChatGPT credentials and resume shared Codex CLI history."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


from account_labels import valid_label, codex_labels

ACCOUNTS = 'ABCDEFGH'


class SwitchError(Exception):
    pass


def load_auth(path):
    try:
        data = json.loads(path.read_text())
        tokens = data.get('tokens', {})
        if data.get('auth_mode') not in (None, 'chatgpt'):
            raise ValueError()
        if not all(isinstance(tokens.get(k), str) and tokens[k]
                   for k in ('id_token', 'access_token', 'refresh_token', 'account_id')):
            raise ValueError()
        part = tokens['id_token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        if not claims.get('sub'):
            raise ValueError()
        return data, (claims['sub'], tokens['account_id'])
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        raise SwitchError(f'{path.name} 缺失或不是有效的 ChatGPT 登录凭据，请重新登录后保存。') from None


def atomic_write(path, data):
    if path.is_symlink():
        raise SwitchError(f'{path.name} 是符号链接，无法安全更新。')
    fd, name = tempfile.mkstemp(prefix='.codex-switch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def account_path(home, label):
    if not valid_label(label):
        raise SwitchError('账户编号须为有效的大写英文字母。')
    return home / f'auth_account_{label}.json'


def matches(home, identity):
    result = []
    for label in codex_labels(home):
        try:
            if load_auth(account_path(home, label))[1] == identity:
                result.append(label)
        except SwitchError:
            pass
    return result


def save(home, label):
    current, identity = load_auth(home / 'auth.json')
    dest = account_path(home, label)
    if dest.exists() and load_auth(dest)[1] != identity:
        raise SwitchError(f'账户 {label} 已保存另一账户；请使用空的账户位置，避免覆盖。')
    atomic_write(dest, current)


def switch(home, label):
    target_path = account_path(home, label)
    target, target_id = load_auth(target_path)
    current, current_id = load_auth(home / 'auth.json')
    # Validate everything before writing; same-account switches must keep refreshed tokens.
    if (home / 'auth.json').is_symlink() or target_path.is_symlink():
        raise SwitchError('登录凭据是符号链接，无法安全切换。')
    labels = matches(home, current_id)
    if labels:
        for old in labels:
            atomic_write(account_path(home, old), current)
    else:
        atomic_write(home / f'auth_before_switch_{time.time_ns()}.json', current)
        print('当前账户尚未归入 A/B/C/D/E/F/G/H，已保存额外备份。')
    atomic_write(home / 'auth.json', current if current_id == target_id else target)


def active_clients(proc=Path('/proc')):
    result = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / 'exe').resolve().name != 'codex':
                continue
            uid = next(x.split()[1] for x in (entry/'status').read_text().splitlines()
                       if x.startswith('Uid:'))
            if int(uid) != os.getuid():
                continue
            args = (entry/'cmdline').read_bytes().split(b'\0')[1:]
            if b'app-server' not in args:
                result.append(int(entry.name))
        except (OSError, StopIteration, ValueError):
            continue
    return sorted(result)


def check_environment(env):
    if any(env.get(k) for k in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN',
                                'OPENAI_IDENTITY_TOKEN_FILE', 'OPENAI_WORKLOAD_IDENTITY')):
        raise SwitchError('当前环境含 API/访问令牌或工作负载身份配置；请先取消这些覆盖再使用订阅账户切换。')


def resume_command(binary, session):
    cmd = [binary, '--no-daemon', '--dangerously-bypass-approvals-and-sandbox',
           '-c', 'cli_auth_credentials_store="file"', 'resume']
    return cmd + ([session] if session else ['--all'])


def _register_locked(home, label, binary):
    dest = account_path(home, label)
    if dest.exists() or dest.is_symlink():
        raise SwitchError(f'{label} 已保存账户，首次登录不会覆盖它。')
    # Browser login is isolated; it never replaces the active CLI's credentials.
    with tempfile.TemporaryDirectory(prefix='codex-account-login-') as directory:
        env = os.environ.copy()
        env['CODEX_HOME'] = directory
        print(f'请在浏览器中登录要保存为 {label} 的账户。当前账户保持原样。', flush=True)
        result = subprocess.run([binary, '-c', 'cli_auth_credentials_store="file"', 'login'], env=env)
        if result.returncode:
            return result.returncode
        data, identity = load_auth(Path(directory)/'auth.json')
        occupied = matches(home, identity)
        if occupied:
            raise SwitchError('登录的账户已保存为 ' + '/'.join(occupied) + '；请登录另一个账户。')
        if dest.exists() or dest.is_symlink():
            raise SwitchError(f'{label} 在登录期间已被登记，未覆盖账户。')
        atomic_write(dest, data)
        print(f'账户 {label} 已保存；以后在任意终端运行 codex-switch {label.lower()} 即可切换并恢复。')
        return 0


def register(home, label, binary):
    from codex_accounts import registration_lock
    try:
        with registration_lock(home):
            return _register_locked(home, label, binary)
    except ValueError as exc:
        raise SwitchError(str(exc)) from None


def show_status(home):
    try:
        current_id = load_auth(home/'auth.json')[1]
        labels = matches(home, current_id)
        print('默认目录账户：' + (' / '.join(labels) if labels else '未归入 A/B/C/D/E/F/G/H'))
    except SwitchError:
        current_id = None
        print('当前账户：未登录或凭据无效')
    for label in codex_labels(home):
        try:
            _, identity = load_auth(account_path(home, label))
            print(f'{label}：已保存' + ('（当前）' if identity == current_id else ''))
        except SwitchError:
            print(f'{label}：未保存或凭据无效')
    print('已保存仅表示本地格式有效；服务端有效性由启动后的登录检查确认。')


def main():
    if sys.argv[1:2] == ['accounts']:
        from account_cli import main as accounts_main
        return accounts_main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog='codex-switch',
                                     description='按账户独立启动，可多终端并行，共享原来的对话历史。')
    parser.add_argument('action', nargs='?', default='status',
                        help='A–H / 1–8 手动；AUTO 自动；QUOTA 在线额度；WEB 本机看板；BUZZ 使用 API；status 查看；save 保存；login 登录')
    parser.add_argument('value', nargs='?', help='会话 ID/名称；save/login/quota 时填 A–H')
    parser.add_argument('--new', action='store_true', help='在指定账户下新开对话')
    parser.add_argument('--prefer', type=str.upper,
                        help='AUTO 优先使用已加入号池的账户；耗尽后继续自动换号')
    args = parser.parse_args()
    home = Path(os.environ.get('CODEX_MULTI_BASE',
                os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))).expanduser().resolve()
    if not home.is_dir():
        raise SwitchError('账户基础目录不存在。')
    action = args.action.upper()
    action = {**{str(i): label for i, label in enumerate(ACCOUNTS, 1)}, 'S': 'STATUS'}.get(action, action)
    if action not in ('SAVE', 'LOGIN', 'STATUS', 'BUZZ', 'AUTO', 'QUOTA', 'WEB') and not valid_label(action):
        parser.error('使用 A–H、1–8、AUTO、QUOTA、WEB、BUZZ、status、save A–H 或 login A–H。')
    if args.new and (not (valid_label(action) or action in ('BUZZ', 'AUTO')) or args.value):
        parser.error('--new 只能与 A–H、AUTO 或 BUZZ 单独使用，不能同时指定旧会话。')
    if args.prefer and (action != 'AUTO' or not valid_label(args.prefer)):
        parser.error('--prefer 只能用于 AUTO。')
    if action == 'WEB':
        if args.value:
            parser.error('看板启动用法：codex-switch web。')
        return subprocess.run([sys.executable, str(Path(__file__).with_name('dashboard.py'))]).returncode
    if action in ('AUTO', 'QUOTA'):
        if action == 'QUOTA' and args.value and not valid_label(args.value.upper()):
            parser.error('额度查询用法：codex-switch quota [A–H]。')
        env = os.environ.copy()
        env['CODEX_MULTI_BASE'] = str(home)
        env.pop('CODEX_AUTO_PREFER', None)
        env.pop('CODEX_QUOTA_ACCOUNT', None)
        if action == 'QUOTA' and args.value:
            env['CODEX_QUOTA_ACCOUNT'] = args.value.upper()
        if args.prefer:
            env['CODEX_AUTO_PREFER'] = args.prefer
        command = [sys.executable, str(Path(__file__).with_name('auto.py'))]
        if action == 'QUOTA':
            command += ['quota']
        elif not args.new:
            command += ['resume', args.value] if args.value else ['resume', '--all']
        from auto_child import run_child
        return run_child(command, env)
    if action == 'STATUS':
        show_status(home)
        print('A–H 各窗口独立登录；选择账户不会改变默认目录账户。')
        return 0
    if action == 'BUZZ':
        binary = shutil.which('codex-buzz')
        if not binary:
            raise SwitchError('找不到 codex-buzz 命令，请先安装 BUZZ 启动脚本。')
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home)
        env['CODEX_SQLITE_HOME'] = str(home)
        env['CODEX_MULTI_BASE'] = str(home)
        command = [binary]
        if not args.new:
            command += ['resume', args.value] if args.value else ['resume', '--all']
        print('已选择 BUZZ（使用 BUZZ API 配置，共享原有对话历史）。', flush=True)
        return subprocess.run(command, env=env).returncode
    if action in ('SAVE', 'LOGIN') and (not args.value or not valid_label(args.value.upper())):
        parser.error(f'用法：codex-switch {action.lower()} A|B|C|D|E|F|G|H')
    check_environment(os.environ)
    if valid_label(action):
        # Never rewrite the global auth.json or acquire a session-long global lock.
        # Use the same isolated launcher as codex-account for every entry point.
        env = os.environ.copy()
        env['CODEX_MULTI_BASE'] = str(home)
        command = [sys.executable, str(Path(__file__).with_name('multi.py')), action]
        command += ['--new'] if args.new else ([args.value] if args.value else [])
        return subprocess.run(command, env=env).returncode
    running = active_clients() if action == 'SAVE' else []
    if running:
        raise SwitchError('save 读取默认目录凭据，请先退出运行中的 CLI；新增账户请用 login A–H。')
    binary = shutil.which('codex')
    if not binary:
        raise SwitchError('找不到 codex 命令。')
    if action == 'LOGIN':
        return register(home, args.value.upper(), binary)
    from codex_accounts import registration_lock
    try:
        with registration_lock(home):
            save(home, args.value.upper())
    except ValueError as exc:
        raise SwitchError(str(exc)) from None
    print(f'默认目录账户已保存为 {args.value.upper()}。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (SwitchError, OSError):
        # Never print JSON, tokens, or subprocess environment on failures.
        exc = sys.exc_info()[1]
        print('codex-switch：' + (str(exc) if isinstance(exc, SwitchError) else '本地文件或进程操作失败。'), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
