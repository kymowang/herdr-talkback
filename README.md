# Herdr Talkback

Herdr 原生插件：在有角色名称的 agent pane 之间可靠转发消息，检查输入占用、重试和提交结果，并按配置恢复断流会话。迁自 Agent Hub `b14021a`，仅支持本机 Herdr 通信，保留队列、告警、重试、状态查询、覆盖层处理和回执提醒。

## 安装和运行

支持 Windows、Linux 和 macOS，需要 Python 3.9+、Herdr 0.9.1+。Python 3.9/3.10 还需要 `tomli`，通过安装本项目自动安装。

先安装 Python 包，再注册插件。安装会生成 `herdr-talkback` 和 `herdr-talkback-plugin` 入口，绑定安装时的 Python 解释器，不依赖系统中的 `python`/`python3` 别名。两个入口所在目录必须在 Herdr 的 `PATH` 中；使用虚拟环境时，应先激活环境再启动 Herdr。

Linux/macOS：

```bash
cd /path/to/herdr-talkback
python3 -m pip install -e .
herdr plugin link "$PWD"
herdr plugin action invoke herdr-talkback.check
herdr plugin log list --plugin herdr-talkback
herdr plugin action invoke herdr-talkback.start
```

Windows PowerShell：

```powershell
cd D:\repo\herdr-talkback
python -m pip install -e .
herdr plugin link "$PWD"
herdr plugin action invoke herdr-talkback.check
herdr plugin log list --plugin herdr-talkback
herdr plugin action invoke herdr-talkback.start
```

Windows 通过命名管道连接 Herdr 和 Talkback 服务，Linux/macOS 使用 Unix socket。配置中的 `socket_path` 和 `daemon_socket_path` 保持路径形式；Windows 下的同名文件只是管道标记，不是 TCP 端口文件。自定义 TOML 路径建议使用正斜杠或单引号，例如 `state_path = 'D:\talkback\state.db'`。

`link` 不会自动启动服务。`start` 在 Herdr 的独立 tab 中运行服务，关闭该 pane 即停止。下一次 Herdr server 启动时，startup hook 会重新打开服务 pane；hook 自身执行后退出，不运行脱离 Herdr 管理的后台进程。[Herdr 插件文档](https://herdr.dev/docs/plugins/)

同一会话的重复启动会被连接检查和进程锁阻止。检测到旧 Agent Hub 仍在运行时，插件拒绝启动，避免两个 Monitor 重复写入。Linux 上使用 systemd 管理旧服务时，切换前先停止：

```bash
systemctl --user stop agent-hub.service
systemctl --user disable agent-hub.service
herdr plugin action invoke herdr-talkback.start
```

旧仓库、数据库和配置不自动修改。新插件使用独立数据库；旧消息记录和待发队列不会自动重放。需要回退时关闭 Talkback 服务 pane，再启动旧服务。

## 配置与适用范围

```bash
herdr plugin config-dir herdr-talkback
```

首次执行插件 action 时在该目录生成 `config.toml`，不覆盖已有文件。[完整示例](examples/config.toml)包含原有默认行为。修改后先执行 `herdr-talkback.check` 验证，再关闭服务 pane 并执行 `herdr-talkback.start` 加载新配置。

每条恢复/追加规则必须显式设置 `agents`。它匹配 **Herdr pane 的角色名称/label**，例如 `lead2`、`scheduler`，区分大小写，支持 `lead*`；`["*"]` 表示所有有名称的 agent，`enabled = false` 禁用规则。恢复规则可另设 `agent_types = ["codex"]`，匹配 Herdr 检出的 CLI 类型，和名称范围取交集；未设置表示不限类型。两者不是模型名称。

默认配置包含：Codex 两种断流错误无限次恢复；原有 API error/rate limit 等恢复；`lead`/`lead2`/`scheduler` 的任务回执提醒。所有匹配文本、命令和范围均在配置中。

### 何时输入 continue

```toml
[[recovery_rules]]
id = "codex-disconnect"
agents = ["lead", "lead2", "scheduler"]
agent_types = ["codex"]
statuses = ["idle", "done", "blocked", "unknown"]
patterns = [
  '^\s*■\s*stream disconnected before completion:\s*error sending request\b',
  '^\s*■\s*request\s+timed\s+out\b',
]
window = "latest_turn"
command = "continue"
cooldown_seconds = 120
max_per_30m = 0
```

- `patterns`：忽略大小写的 Python 正则，多条为“任一匹配”。TOML 单引号字符串可直接保留反斜杠。
- `window = "latest_turn"`：只检查最新一轮，避免历史错误误触发；`recent` 检查最近 80 行，仅在确实需要时使用。
- `statuses`：仅允许停止工作的状态，禁止配置 `working`。
- `command`：发送到 agent 输入框的文本，不作为 shell 命令执行。始终先检查输入占用。
- `cooldown_seconds`：同一 pane 两次恢复的最小间隔；省略时沿用 `[monitor].continue_cooldown_seconds`。
- `max_per_30m`：同一 pane 的恢复次数限制，`0` 表示无限制；省略时沿用 `[monitor].max_continues_per_30m`。

恢复规则按顺序取第一个命中的规则。次数和冷却记录按 pane 共享。启动和重连时会检查已有 pane；平时由 Herdr 状态变化事件触发检查。

### 消息追加内容

```toml
[[message_rules]]
id = "task-receipt"
agents = ["lead", "lead2", "scheduler"]
targets = ["*"]
append = "上述内容如果是任务而非通知，完成后请发回执给我"
```

`agents` 是发送方名称范围；`targets` 是接收方名称范围，省略时为全部。所有命中的追加规则按配置顺序组成独立行，重试不会重复追加完整后缀块。

设置顶层 `recovery_rules = []` 或 `message_rules = []` 可分别关闭全部默认规则；如果提供自定义规则数组，会替换对应的整组默认规则。数组要放在所有 `[section]` 前面。

输入框占用检查、投递前后校验等保护不依赖策略规则，始终保留。

## 通信命令

插件 action 会自动注入配置/状态路径。普通终端中的 CLI 使用显式 `--config` 访问同一插件实例：

```bash
TALKBACK_CONFIG="$(herdr plugin config-dir herdr-talkback)/config.toml"
herdr-talkback --config "$TALKBACK_CONFIG" panes
herdr-talkback --config "$TALKBACK_CONFIG" pane list
herdr-talkback --config "$TALKBACK_CONFIG" send '[lead->dev-be] 请处理任务' --dry-run
herdr-talkback --config "$TALKBACK_CONFIG" send '[lead->dev-be] 请处理任务'
herdr-talkback --config "$TALKBACK_CONFIG" queue
herdr-talkback --config "$TALKBACK_CONFIG" status lead2 --json
herdr-talkback --config "$TALKBACK_CONFIG" input lead2 --json
herdr-talkback --config "$TALKBACK_CONFIG" exec lead2 continue
herdr-talkback --config "$TALKBACK_CONFIG" retry msg_xxx
herdr-talkback --config "$TALKBACK_CONFIG" alerts --json
herdr-talkback --config "$TALKBACK_CONFIG" alerts clear
herdr-talkback --config "$TALKBACK_CONFIG" tail
```

`pane list` 以表格显示全部窗格，包含未命名窗格与插件服务窗格。`NAME` 为窗格名称，`TITLE` 为终端标题，`TAB NAME` 为 Tab 名称（例如 `jupyter`）；另外显示窗格 ID、Agent、状态、Tab ID 和目录。中文名称按显示宽度对齐。`panes` 仍用于查看可路由的角色。

将指定窗格的名称设置为所属 Tab 的当前名称：

```bash
herdr-talkback pane name-from-tab w2:p5
# 批量填写当前会话中所有名称为空的窗格
herdr-talkback pane name-from-tab --all
# 强制覆盖所有窗格的已有名称
herdr-talkback pane name-from-tab --all --force
```

例如 Tab 名为 `jupyter`，执行后该窗格的 `NAME` 也变为 `jupyter`。默认跳过已有名称的窗格，`--force` 可强制覆盖，也可用于单个窗格。`--all` 处理当前 Herdr 会话的全部窗格（包括其他 workspace）；同一 Tab 中的多个窗格会得到相同名称。此命令只执行一次复制，不会随 Tab 改名自动同步；Tab 无名称时保持原名称并返回非零退出码。Talkback 消息角色只接受字母、数字、点、下划线和连字符，包含空格或中文的名称仅用于展示。

未安装 Python 包时可在仓库目录使用 `python3 -m herdr_talkback` 代替 `herdr-talkback`。消息头支持 `→`/`->` 和本机限定地址。跨主机目标被拒绝。

独立 CLI 默认配置是 `~/.config/herdr-talkback/config.toml`。插件使用 `HERDR_PLUGIN_CONFIG_DIR` 与 `HERDR_PLUGIN_STATE_DIR`；初始配置中的 `{session}` 由 Herdr socket 路径的 hash 替换，使多个 Herdr 会话的数据库、队列和进程锁互不冲突。CLI 和插件必须连接同一个 `HERDR_SOCKET_PATH`。

## 开发验证

## 跨主机通信（HTTPS 短连接）

跨主机功能默认关闭。两端都安装网络依赖，监听独立端口，通过普通 HTTPS
请求收发消息；空闲时没有心跳或轮询。默认端口为 TCP **18765**，可在配置中修改。

```bash
python -m pip install -e '.[network]'
herdr-talkback peer init-tls --directory /path/to/plugin-config/tls --host 192.168.90.18
```

Windows 使用自己的 IP，例如 `--host 192.168.60.55`。该命令生成 `node.crt`、
`node.key` 和随机 `pair.token`，不会输出私密内容，也不会覆盖已有文件；
确需重新生成时使用 `--force`。每端保留自己的私钥，把**公开证书**复制给对端；
同一对节点使用同一个 `pair.token`，通过可信渠道交换，勿提交到 Git。

Windows 示例（加入插件 `config.toml`；相对路径以配置目录为基准）：

```toml
[core]
node_id = "linux-main"

[network]
enabled = true
listen_host = "192.168.60.55"
listen_port = 18765
cert_file = "tls/node.crt"
key_file = "tls/node.key"

[peers.openclaw]
url = "https://192.168.90.18:18765"
ca_file = "tls/openclaw.crt"
token_file = "tls/pair.token"
```

Linux 端将本机 `node_id` 设置为 `openclaw`，监听 `192.168.90.18`；对端表使用
`[peers.linux-main]`，URL 为 `https://192.168.60.55:18765`，CA 指向 Windows 的公开证书。
有历史消息的节点保持原本的 `core.node_id`；两端 ID 必须不同。
证书必须包含 URL 中的 IP/主机名，不能关闭证书验证。
配置中已有 `[core]` 时修改原表，不要重复声明。

两端防火墙只向对端 IP 放行该 TCP 端口。修改配置后先运行插件 `check`，再关闭
Talkback 服务 pane 并执行 `start`。每个网络端口属于一个 Herdr 会话，多会话须使用
不同的配置和端口；离线对端不会阻止本地服务启动。

```bash
CFG="$(herdr plugin config-dir herdr-talkback)/config.toml"
herdr-talkback --config "$CFG" peers list
herdr-talkback --config "$CFG" peers check openclaw
herdr-talkback --config "$CFG" send '[talkback->openclaw/worker] 测试消息'
herdr-talkback --config "$CFG" message status msg_xxx --json
```

反向发送在 Linux 端执行，例如 `[worker->linux-main/talkback] 收到`。
消息先持久化，再发送；远端确认入队后，本端继续查询最终投递回执。
断网时按递增间隔重试，重启后使用相同消息 ID 续查/去重，不自动丢弃待发消息。
`--direct` 仅用于本机，不能绕过跨机队列。

`queued`/`accepted` 不等于 Pane 已收到，以 `message status` 的最终 `delivered`
为准。`delivery_unknown` 表示不能排除 Pane 已接收，不自动重复提交；人工 `retry`
是一次新投递，可能重复原消息。接收端更换/清空数据库后，实例 ID 会改变，发送端会
停止自动重放；应人工核对未完成消息，不能通过删除身份记录来强行重放。
消息归档不会删除跨机去重记录和最终回执。

## 开发验证

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q
python3 -m pip wheel --no-deps .
```

插件的常驻服务由 Herdr pane entrypoint 承载。不要在 systemd 中同时启动旧 Agent Hub 和 Talkback。
