# Codex Account Dashboard

在自己的电脑管理多个 Codex 与 AGY 账号，查看额度、调用用量、API 参考成本和额度容量估算。账号数量可继续扩展为 I、J、AA 等，无需修改源码。

## 支持范围

- Ubuntu/Linux、Windows 的 WSL2；Python 3.10+、Node.js 18+、已安装的官方 Codex CLI。
- AGY 管理需要另外安装官方 AGY CLI，以及系统可用的 `unshare` 用户/挂载命名空间。
- 网页仅监听 `127.0.0.1`。不提供公网账号托管。
- 项目不附带账号、Token、订阅额度或官方 CLI 二进制。每个人使用自己的登录。

## 安装

下载本仓库后，在 Ubuntu 或 WSL 终端运行：

本轮核验的 Codex CLI 版本是 `0.169.0`，启动器要求其支持 `--no-daemon`。未安装 Codex 时，可先运行 `npm install -g @openai/codex@0.169.0`。不满足隔离能力的版本会停止启动。

```bash
git clone https://github.com/xyubin2u/codex-account-dashboard.git
cd codex-account-dashboard
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
codex-switch web
```

浏览器打开 <http://localhost:8787>。安装器只复制本项目文件，不修改原有默认登录。需要后台自动启动时，运行 `bash install.sh --service`；前提是系统启用了 systemd 用户服务。

更新时在下载目录运行 `git pull --ff-only`，然后重新执行 `bash install.sh`。运行中的看板须退出重开；后台服务用 `systemctl --user restart codex-quota-dashboard.service`，或用 `bash install.sh --service` 更新并重启看板。已打开的 AUTO 窗口需正常退出重开才会加载更新；不要在其任务中途强制关闭。既有手工安装目录需要先单独备份、迁移，安装器不会直接覆盖。

## 添加、移除账号

网页点击“添加账号”，选择 Codex 或 AGY。可导入官方客户端生成的 JSON 登录文件，也可复制隔离登录命令到终端完成授权：

```bash
codex-switch accounts login codex --label A
codex-switch accounts login agy --label B
codex-switch accounts list
```

导入文件：

```bash
codex-switch accounts add codex --file /path/to/auth.json --label AA
codex-switch accounts add agy --file /path/to/antigravity-oauth-token --label C
```

省略 `--label` 自动选择空编号。AGY 的 A 表示原生默认登录，新登录从 B 开始。移除可在卡片操作，也可运行 `codex-switch accounts remove codex --label AA`，按提示确认。正在使用、查询或用券的账号须等待结束后再移除。

移除只删除本工具托管的登录资料，保留共享会话、历史用量和用券结果；AGY A 仅取消纳管，保留原生默认登录。此操作不向上游发送注销或撤销授权请求。历史用量中的账号编号是当时的槽位，同编号换人不表示同一身份。

## 启动与 AUTO

```bash
codex-switch A                 # 指定账号，选择已有会话
codex-switch AA --new           # 指定账号，新会话
codex-switch auto               # 在已启用号池中选择
codex-switch auto SESSION_ID    # 恢复指定会话
codex-switch quota AA           # 单独查询额度
```

新增账号后按需要在卡片设置中启用 AUTO 号池。执行中的任务达到设置阈值时先停止当前账号，再核对备选及原会话；等待输入的闲置窗口延后到下一次任务复查。会话归属无法确认时停止，不猜测其他窗口。

本版本沿用既有 Codex 会话启动器默认跳过审批/沙箱参数；仅在你信任的目录与命令中使用。AGY 登录只负责账号授权，不代表已实现 AGY 自动额度换号。AGY 手动启动入口见 `codex-switch accounts run agy --label B -- --help`。

用量以本机写出的日志为准，缺证历史保留未归属；成本是 API 参考估算，并非订阅实际扣款。参考价有核验日期，不是自动更新的实时报价。

## 数据与维护

源码位于 `~/.local/share/codex-switch`；Codex 独立凭据在其 `accounts` 子目录及 `~/.codex/auth_account_*.json`；AGY 独立资料在 `~/.local/share/agy-switch`。看板私有缓存、用量数据库和用券账本位于 `~/.local/state/codex-switch`。不要上传这些运行数据或任何原生登录文件。

卸载后台服务可运行 `systemctl --user disable --now codex-quota-dashboard.service`。删除程序前先备份自己需要的记录；本项目不自动清空账号或历史。

许可证：GPL-3.0-only；上游组件与改动说明见 [THIRD_PARTY.md](THIRD_PARTY.md)。
