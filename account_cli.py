"""Interactive/local-file account management for both supported platforms."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    from accounts import AccountManager
    values=list(sys.argv[1:] if argv is None else argv)
    native_args=[]
    if values[:1]==['run'] and '--' in values:
        split=values.index('--');native_args=values[split+1:];values=values[:split]
    parser=argparse.ArgumentParser(prog='codex-switch accounts')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('list',help='列出已纳管账号；不显示登录资料')
    run=sub.add_parser('run',help='以隔离账号运行官方 AGY CLI')
    run.add_argument('platform',choices=('agy',))
    run.add_argument('--label',required=True)
    for action in ('add','login','remove'):
        command=sub.add_parser(action)
        command.add_argument('platform',choices=('codex','agy'))
        command.add_argument('--label',help='大写字母编号，如 I、AA；新增时省略则自动编号')
        if action=='add':command.add_argument('--file',required=True,help='官方客户端登录 JSON 文件；- 表示标准输入')
        if action=='remove':command.add_argument('--yes',action='store_true',help='确认从本工具移除，保留共享历史')
    args=parser.parse_args(values)
    try:
        manager=AccountManager()
        if args.action=='run':
            return manager.run(args.platform,args.label,native_args)
        if args.action=='list':
            rows=manager.public_state()['accounts']
            for row in rows:print(f"{row['platform'].upper()} {row['label']}"+('（原生账号）' if row['external'] else ''))
            if not rows:print('尚未添加账号。')
            return 0
        if args.action=='login':
            result=manager.login(args.platform,args.label)
            return result if isinstance(result,int) else 0
        if args.action=='add':
            if args.file=='-':raw=sys.stdin.read(256*1024+1)
            else:
                with Path(args.file).expanduser().open() as source:raw=source.read(256*1024+1)
            if len(raw.encode())>256*1024:raise ValueError('登录文件过大。')
            try:credential=json.loads(raw)
            except (ValueError,UnicodeError):raise ValueError('登录文件不是有效 JSON。') from None
            row=manager.add(args.platform,args.label,credential)
            print(f"已添加 {row['platform'].upper()} {row['label']}。")
            return 0
        if not args.label:raise ValueError('移除时请指定 --label。')
        row=next((row for row in manager.public_state()['accounts']
                  if row['platform']==args.platform and row['label']==args.label),None)
        if row is None:raise ValueError('此账号未被本工具管理。')
        if not args.yes:
            if not sys.stdin.isatty():raise ValueError('移除需要 --yes 确认，或在交互终端运行。')
            text='仅取消本工具管理，保留原生默认登录' if row['external'] else '删除本工具保存的两份登录，保留共享会话和历史'
            if input(f"{args.platform.upper()} {args.label}：{text}。输入 {args.label} 确认：").strip()!=args.label:
                print('已取消。');return 0
        manager.remove(args.platform,args.label,row['remove_token'],True)
        print(f"已移除 {args.platform.upper()} {args.label}。")
        return 0
    except (ValueError,OSError,KeyboardInterrupt) as error:
        message=str(error) if isinstance(error,ValueError) else '操作已取消。' if isinstance(error,KeyboardInterrupt) else '本地账号文件操作失败。'
        print('账号管理：'+message,file=sys.stderr)
        return 130 if isinstance(error,KeyboardInterrupt) else 1


if __name__=='__main__':sys.exit(main())
