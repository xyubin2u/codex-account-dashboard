#!/usr/bin/env python3
"""Install an audited release locally, preserving account and history data."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile


def write_atomic(path,content,mode=0o600):
    if path.is_symlink():raise ValueError('安装目标不能是符号链接。')
    fd,name=tempfile.mkstemp(prefix='.codex-install-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(content)
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def manifest(source):
    entries=[]
    for line in (source/'MANIFEST.sha256').read_text().splitlines():
        digest,name=line.split('  ',1)
        path=Path(name)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] in ('accounts','backups','.git'):
            raise ValueError('安装清单含非法路径。')
        origin=source/path
        if origin.is_symlink() or not origin.is_file() or hashlib.sha256(origin.read_bytes()).hexdigest()!=digest:
            raise ValueError('文件校验失败：'+name)
        entries.append(path)
    return entries


def main(argv=None):
    parser=argparse.ArgumentParser(description='安装 Codex Account Dashboard；不导入任何登录资料')
    parser.add_argument('--service',action='store_true',help='安装并启动本机 systemd 用户服务')
    args=parser.parse_args(argv)
    if sys.platform!='linux' or sys.version_info<(3,10):raise ValueError('需要 Linux/WSL 与 Python 3.10 或更新版本。')
    source=Path(__file__).resolve().parent.parent
    entries=manifest(source)
    home=Path.home();destination=home/'.local/share/codex-switch';bindir=home/'.local/bin'
    for path in (destination,bindir,home/'.codex'):
        if path.is_symlink():raise ValueError('安装目录不能是符号链接。')
        path.mkdir(mode=0o700,parents=True,exist_ok=True)
    managed=destination/'.installed-files'
    if (destination/'core.py').exists() and not managed.exists():
        raise ValueError('目标已有手工管理的安装；请保留原目录并在独立用户下安装公开版。')
    for relative in entries:
        target=destination/relative
        for parent in (target,*target.parents):
            if parent==destination.parent:break
            if parent.is_symlink():raise ValueError('安装路径含符号链接，已停止。')
        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        raw=(source/relative).read_bytes()
        fd,name=tempfile.mkstemp(prefix='.codex-install-',dir=target.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                os.fchmod(stream.fileno(),0o700 if relative.suffix=='.sh' or relative.parts[0]=='private-bin' else 0o600)
                stream.write(raw)
            os.replace(name,target)
        finally:
            if os.path.exists(name):os.unlink(name)
    write_atomic(managed,'\n'.join(str(path) for path in entries)+'\n')
    for name,module in [('codex-switch','core.py'),('codex-account','multi.py')]:
        launcher=bindir/name
        if launcher.is_symlink():raise ValueError('启动入口已有符号链接，请先核对并保留原入口。')
        write_atomic(launcher,'#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(destination/module))+' "$@"\n',0o700)
    print('已安装：'+str(destination))
    print('将 '+str(bindir)+' 加入 PATH 后运行：codex-switch accounts list')
    print('看板：codex-switch web（http://localhost:8787）')
    print('更新后请退出并重开看板；后台服务运行：systemctl --user restart codex-quota-dashboard.service')
    print('已打开的 AUTO 窗口请在任务结束后正常退出重开。')
    for command in ('codex','node'):
        if not shutil.which(command):print('请先安装所需官方依赖：'+command)
    if args.service:
        binary=shutil.which('codex')
        if not binary or not shutil.which('node'):raise ValueError('启用服务前请先安装 Codex CLI 与 Node.js。')
        unitdir=home/'.config/systemd/user';unitdir.mkdir(mode=0o700,parents=True,exist_ok=True)
        if any(c in str(home)+str(sys.executable)+str(binary) for c in ('\n','\r','"','%')):
            raise ValueError('此用户路径无法用于 systemd，请使用前台启动。')
        search=os.environ.get('PATH',os.defpath)
        if any(c in search for c in ('\n','\r','"','%')):raise ValueError('PATH 无法用于 systemd，请使用前台启动。')
        unit='''[Unit]\nDescription=Codex Account Dashboard\nAfter=network.target\n\n[Service]\nType=simple\nUMask=0077\nEnvironment="PATH={search}"\nEnvironment="CODEX_QUOTA_REAL_CODEX={binary}"\nExecStart="{python}" "{script}"\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n'''.format(search=search,binary=binary,python=sys.executable,script=destination/'dashboard.py')
        write_atomic(unitdir/'codex-quota-dashboard.service',unit)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        subprocess.run(['systemctl','--user','enable','codex-quota-dashboard.service'],check=True)
        subprocess.run(['systemctl','--user','restart','codex-quota-dashboard.service'],check=True)
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        print('安装失败：'+(str(error) if isinstance(error,ValueError) else '本地文件或服务操作未完成。'),file=sys.stderr)
        sys.exit(1)
