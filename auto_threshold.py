"""Per-account AUTO reserve thresholds, stored separately from credentials."""
import fcntl,json,os,tempfile
from pathlib import Path
from account_labels import valid_label,codex_labels,sort_key
NAME='codex-switch-auto-threshold.json'
class ThresholdError(ValueError):pass

def _read(base):
    path=Path(base)/NAME
    if path.is_symlink():raise ThresholdError('额度阈值配置路径异常。')
    try:data=json.loads(path.read_text())
    except FileNotFoundError:return {'five_hour':{x:0 for x in codex_labels(base)},'weekly':{x:0 for x in codex_labels(base)}}
    except (OSError,ValueError,UnicodeError):raise ThresholdError('额度阈值配置无法读取。') from None
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('thresholds'),dict) or not isinstance(data.get('weekly_thresholds',{}),dict):raise ThresholdError('额度阈值配置格式错误。')
    result={}
    for window,key in [('five_hour','thresholds'),('weekly','weekly_thresholds')]:
        values=data.get(key,{})
        if any(not valid_label(k) or type(v) is not int or not 0<=v<=99 for k,v in values.items()):raise ThresholdError('额度阈值配置无效。')
        result[window]={x:values.get(x,0) for x in sorted(set(codex_labels(base))|set(values),key=sort_key)}
    return result

def read_thresholds(base):return _read(base)['five_hour']
def read_weekly_thresholds(base):return _read(base)['weekly']
def read_all_thresholds(base):return _read(base)

def set_thresholds(base,labels,five_hour=None,weekly=None):
    if not isinstance(labels,list) or not labels or len(set(labels))!=len(labels) or any(not valid_label(x) for x in labels):raise ValueError('请选择有效账户。')
    for value in (five_hour,weekly):
        if value is not None and (type(value) is not int or not 0<=value<=99):raise ValueError('阈值需为0到99的整数。')
    if five_hour is None and weekly is None:raise ValueError('请设置至少一个阈值。')
    base=Path(base);base.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(base/(NAME+'.lock'),os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);data=_read(base)
        for label in labels:
            if five_hour is not None:data['five_hour'][label]=five_hour
            if weekly is not None:data['weekly'][label]=weekly
        outfd,tmp=tempfile.mkstemp(prefix='.auto-threshold-',dir=base)
        try:
            with os.fdopen(outfd,'w') as out:
                os.fchmod(out.fileno(),0o600);json.dump({'version':1,'thresholds':data['five_hour'],'weekly_thresholds':data['weekly']},out);out.write('\n');out.flush();os.fsync(out.fileno())
            os.replace(tmp,base/NAME)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    return data

def set_threshold(base,label,value,window='five_hour'):
    if window not in ('five_hour','weekly'):raise ValueError('额度类型无效。')
    set_thresholds(base,[label],**{window:value})
    return value
