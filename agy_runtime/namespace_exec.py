"""Mount private AGY directories, drop capabilities, then run the official CLI."""
import ctypes
import os
from pathlib import Path
import sys

from files import directory


def private_view(gemini_source,gemini_target,config_source,config_target):
    if not sys.platform.startswith('linux'):raise ValueError('此入口只支持 Linux/WSL。')
    libc=ctypes.CDLL(None,use_errno=True)
    for source,target in ((gemini_source,gemini_target),(config_source,config_target)):
        with directory(source) as source_fd,directory(target) as target_fd:
            if os.fstat(source_fd).st_uid!=os.getuid():raise ValueError('隔离目录不属于当前用户。')
            if libc.mount(os.fsencode('/proc/self/fd/'+str(source_fd)),os.fsencode('/proc/self/fd/'+str(target_fd)),None,4096,None):
                raise OSError(ctypes.get_errno(),'无法建立 AGY 独立目录视图')
    if libc.prctl(47,4,0,0,0):raise OSError(ctypes.get_errno(),'无法清除 ambient 能力')
    class Header(ctypes.Structure):_fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
    class Data(ctypes.Structure):_fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    header=Header(0x20080522,0);data=(Data*2)()
    if libc.capset(ctypes.byref(header),ctypes.byref(data)):raise OSError(ctypes.get_errno(),'无法清除挂载能力')


def main():
    if len(sys.argv)<7 or sys.argv[5]!='--':raise ValueError('隔离启动参数无效。')
    private_view(*sys.argv[1:5])
    os.execvpe(sys.argv[6],sys.argv[6:],os.environ)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError):raise SystemExit('AGY 隔离失败，未回退原账号。')
