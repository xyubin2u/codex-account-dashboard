"""Run one quota reader inside an unprivileged, private DNS mount namespace."""
import os
from pathlib import Path
import subprocess
import sys

RESOLVER = Path('/usr/local/lib/ubuntu-network-terminal/wireguard-resolv.conf')


def main():
    # Namespace root maps to the existing user's UID; no host root or sudo involved.
    if len(sys.argv) < 4 or os.getuid() != 0:
        raise SystemExit('Quota DNS wrapper requires its private user namespace.')
    for kind, original in zip(('mnt', 'user'), sys.argv[1:3]):
        if os.readlink('/proc/self/ns/'+kind) == original:
            raise SystemExit('Refusing DNS changes outside a private namespace.')
    if not RESOLVER.is_file():
        raise SystemExit('WireGuard DNS configuration is unavailable.')
    subprocess.run(['/usr/bin/mount','--bind',str(RESOLVER),'/etc/resolv.conf'],check=True)
    subprocess.run(['/usr/bin/mount','-o','remount,bind,ro','/etc/resolv.conf'],check=True)
    os.execv(sys.argv[3], sys.argv[3:])


if __name__ == '__main__':
    main()
