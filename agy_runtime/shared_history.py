"""Attach an unused profile to an already configured shared AGY history.

No full-history migration is performed and existing independent history is never
moved or deleted. Native rename-based caches stay local to the new profile.
"""
import os
import fcntl
from pathlib import Path
import shutil
import stat

from .files import directory, private_dir, read_json, write_json

PATHS=('conversations','brain','annotations','implicit','presence',
       'conversation_summaries.db','jetbox_summaries_proto.pb',
       'cache/last_conversations.json','cache/conversation_metadata.json')
CACHES={'jetbox_summaries_proto.pb','cache/last_conversations.json','cache/conversation_metadata.json'}


def attach_shared_history(profiles,label):
    shared=profiles.root/'shared/history';manifest=profiles.root/'shared/history-manifest.json'
    try:
        with directory(manifest.parent) as parent:
            info=os.stat(manifest.name,dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('共享历史登记无效。')
        with directory(shared):pass
    except FileNotFoundError:return
    cli=profiles.path(label).parent;created=[]
    try:
        for relative in PATHS:
            target=shared/relative;path=cli/relative
            try:
                with directory(target.parent) as parent:
                    info=os.stat(target.name,dir_fd=parent,follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('共享历史路径无效。')
                    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):raise ValueError('共享历史类型无效。')
            except FileNotFoundError:continue
            private_dir(path.parent)
            with directory(path.parent) as parent:
                try:old=os.stat(path.name,dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError:old=None
                if old is not None:
                    if stat.S_ISLNK(old.st_mode) and os.readlink(path.name,dir_fd=parent)==str(target):continue
                    # Existing independent data needs a separate explicit migration.
                    if relative in CACHES and stat.S_ISREG(old.st_mode):continue
                    raise ValueError('此账号已有独立历史，未自动迁移或覆盖；请先检查原历史。')
                if relative in CACHES:
                    # A new cache is a derived copy, not shared writable state.
                    with directory(target.parent) as source_parent:
                        source=os.open(target.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=source_parent)
                        dest=os.open(path.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                        created.append(path)
                        with os.fdopen(source,'rb') as src,os.fdopen(dest,'wb') as dst:shutil.copyfileobj(src,dst)
                else:
                    os.symlink(str(target),path.name,dir_fd=parent);created.append(path)
        sync_continue_pointer(profiles,label)
    except Exception:
        for path in reversed(created):
            try:
                with directory(path.parent) as parent:os.unlink(path.name,dir_fd=parent)
            except OSError:pass
        raise


def sync_continue_pointer(profiles,label):
    """Merge small derived pointers while keeping native atomic caches local."""
    shared=profiles.root/'shared/history';relative='cache/last_conversations.json'
    try:
        with directory(profiles.root/'shared') as parent:
            info=os.stat('history-manifest.json',dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('共享历史登记无效。')
    except FileNotFoundError:return
    private_dir(profiles.root/'runtime')
    with directory(profiles.root/'runtime') as parent:
        fd=os.open('history-pointer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=parent)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('共享历史锁无效。')
        fcntl.flock(fd,fcntl.LOCK_EX)
        observed=[];merged={};canonical=shared/relative
        for path in [canonical]+[profiles.path(item).parent/relative for item in profiles.labels()]:
            try:
                data=read_json(path)
                if isinstance(data,dict):observed.append((path.stat().st_mtime_ns,data))
            except (OSError,ValueError,UnicodeError):continue
        for _,data in sorted(observed,key=lambda item:item[0]):
            for workspace,conversation in data.items():
                if not isinstance(workspace,str) or not isinstance(conversation,str) or not conversation or len(conversation)>512:continue
                if any(c in conversation for c in ('/','\\','\0')) or conversation in ('.','..'):continue
                try:
                    with directory(shared/'conversations') as parent:
                        info=os.stat(conversation+'.db',dir_fd=parent,follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid():merged[workspace]=conversation
                except FileNotFoundError:continue
        if merged:
            for path in (canonical,profiles.path(label).parent/relative):
                private_dir(path.parent);write_json(path,merged,replace=True)
    finally:os.close(fd)
