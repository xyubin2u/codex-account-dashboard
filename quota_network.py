"""Dashboard-only network selection; never changes the system VPN or CLI environment."""
from pathlib import Path
import subprocess
import threading
import sys
import os

from quota_data import NativeQuotaProbe

MODES = ('clash', 'direct')
PROXY_KEYS = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')


def configure_proxy(env):
    """Preserve the original dashboard's default WSL Clash discovery."""
    if env.get('HTTPS_PROXY') or env.get('https_proxy'):
        return
    try:
        if 'microsoft' not in Path('/proc/sys/kernel/osrelease').read_text().lower():
            return
        route = subprocess.check_output(['ip','route','show','default'],text=True,timeout=3).split()
        gateway = route[route.index('via')+1]
        port = int(env.get('CODEX_QUOTA_PROXY_PORT','7898'))
        proxy = f'http://{gateway}:{port}'
        for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
            env[key] = proxy
        env.setdefault('NO_PROXY','localhost,127.0.0.1,::1')
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


def network_environment(source, mode):
    if mode not in MODES:
        raise ValueError('查询路线只能是 Clash 或 WireGuard／系统网络。')
    env = dict(source)
    if mode == 'direct':
        for key in PROXY_KEYS:
            env.pop(key, None)
        env['NO_PROXY'] = env['no_proxy'] = '*'
    else:
        # A previous direct-mode NO_PROXY must not override explicit Clash routing.
        for key in ('NO_PROXY','no_proxy'):
            env[key] = 'localhost,127.0.0.1,::1'
        configure_proxy(env)
    return env


class RoutedQuotaProbe:
    def __init__(self, base, profiles, binary, env):
        self.base, self.profiles, self.binary = base, profiles, binary
        self.source = dict(env)
        self.current = None

    def set_mode(self, mode):
        env = network_environment(self.source, mode)
        prefix = []
        resolver = Path('/usr/local/lib/ubuntu-network-terminal/wireguard-resolv.conf')
        if mode == 'direct' and resolver.is_file() and 'microsoft' in Path('/proc/sys/kernel/osrelease').read_text().lower():
            prefix = ['/usr/bin/unshare','--user','--map-root-user','--mount',
                      '--propagation','private',sys.executable,
                      str(Path(__file__).with_name('quota_dns_exec.py')),
                      os.readlink('/proc/self/ns/mnt'),os.readlink('/proc/self/ns/user')]
        fresh = NativeQuotaProbe(self.base, self.profiles, self.binary,
                                 env=env, stop=threading.Event(), command_prefix=prefix)
        if self.current is not None:
            self.current.stop.set()
        self.current = fresh

    def capture(self):
        # Each queued job holds its own route; changing the selector cannot mutate it.
        return self.current

    def close(self):
        if self.current is not None:
            self.current.stop.set()
