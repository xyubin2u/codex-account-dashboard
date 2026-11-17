"""Private files accessed through no-follow directory descriptors."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import stat


@contextmanager
def directory(path,create=False):
    path=Path(path).absolute()
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            if component in ('.','..'):raise ValueError('账户目录无效。')
            if create:
                try:os.mkdir(component,0o700,dir_fd=fd)
                except FileExistsError:pass
            try:new=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except (NotADirectoryError,OSError) as error:
                if isinstance(error,FileNotFoundError):raise
                raise ValueError('账户目录不能是符号链接，且须为有效目录。') from None
            os.close(fd);fd=new
        yield fd
    finally:os.close(fd)


def private_dir(path):
    with directory(path,create=True) as fd:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid():raise ValueError('账户目录不属于当前用户。')
        os.fchmod(fd,0o700)
    return Path(path)


@contextmanager
def private_file(path,flags=os.O_RDONLY):
    path=Path(path)
    with directory(path.parent) as parent:
        try:fd=os.open(path.name,flags|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        except OSError as error:
            if isinstance(error,FileNotFoundError):raise
            raise ValueError('登录文件必须为当前用户的私有普通文件。') from None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                raise ValueError('登录文件必须仅当前用户可读写。')
            yield fd
        finally:os.close(fd)


def read_json(path,limit=256*1024):
    with private_file(path) as fd:
        if os.fstat(fd).st_size>limit:raise ValueError('登录文件过大。')
        raw=os.read(fd,limit+1)
        if len(raw)>limit:raise ValueError('登录文件过大。')
        return json.loads(raw)


def write_json(path,data,replace=False):
    path=Path(path);raw=(json.dumps(data,ensure_ascii=True,allow_nan=False)+'\n').encode()
    with directory(path.parent) as parent:
        name='.agy-'+secrets.token_hex(16)
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            if replace:
                try:
                    info=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                        raise ValueError('账户登记文件必须为当前用户的私有普通文件。')
                except FileNotFoundError:pass
                os.rename(name,path.name,src_dir_fd=parent,dst_dir_fd=parent)
            else:
                try:os.link(name,path.name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
                except FileExistsError:raise ValueError('此位置已有登录文件，未覆盖原文件。') from None
        finally:
            try:os.unlink(name,dir_fd=parent)
            except FileNotFoundError:pass


def exists(path):
    path=Path(path)
    try:
        with directory(path.parent) as parent:os.stat(path.name,dir_fd=parent,follow_symlinks=False)
        return True
    except FileNotFoundError:return False
