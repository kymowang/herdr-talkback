# Herdr Talkback

Herdr 原生插件：在有角色名称的 agent pane 之间可靠转发消息，检查输入占用、重试和提交结果，并按配置恢复断流会话。迁自 Agent Hub `b14021a`，仅支持本机 Herdr 通信，保留队列、告警、重试、状态查询、覆盖层处理和回执提醒。

## 安装和运行

### Herdr 插件市场安装

先确保 Python 3.9+、Git 和 Herdr 0.9.1+ 可用，并使安装 Python 命令的目录出现在 Herdr 的 PATH 中。插件市场安装不会自动安装 Python 依赖；在启动服务前执行：

```bash
python -m pip install "herdr-talkback[network] @ git+https://github.com/kymowang/herdr-talkback.git"
herdr plugin install kymowang/herdr-talkback
herdr plugin action invoke herdr-talkback.check
herdr plugin log list --plugin herdr-talkback
herdr plugin action invoke herdr-talkback.start
```

Linux/macOS 上可能需要用 `python3` 代替 `python`。检查日志确认配置及连接成功；远程通信还需双方配置 peer。更新时同时更新 Python 包和 Herdr 插件，并重启服务；已链接的本地插件需先解除链接。


支持 Windows、Linux 和 macOS，需要 Python 3.9+、Herdr 0.9.1+。Python 3.9/3.10 还需要 `tomli`，通过安装本项目自动安装。

也可以生成包含 Talkback、Herdr 插件清单和 Codex skill 的跨平台安装包：

```bash
python tools/build_bundle.py
```

生成的 `dist/herdr-talkback-0.1.0-bundle.zip` 解压后，在 Herdr 会话内运行
`python install.py`；Linux/macOS 可用 `python3 install.py`。安装默认包含跨机依赖，
需要可用的 Python 包源；`--local-only` 仅安装本地功能。安装入口和升级说明见包内
`INSTALL.md`。脚本保留既有配置、数据库和网络凭据，不会静默覆盖不同内容的 skill。

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

### 查看版本

`herdr-talkback --version` 显示当前命令的发行版本和代码指纹。`herdr-talkback version` 比较命令与运行中服务的版本和指纹；`matches: false` 表示二者不同，应确认安装和服务重启。若服务离线或旧版不支持查询，会返回 `service_error`，而不是推断服务版本。可用 `--config <配置路径>` 指定会话配置。

`herdr-talkback --config <配置路径> peers check <节点>` 也会显示支持该功能的远端服务版本。指纹在进程启动时由包内 Python 和默认 TOML 内容计算，可区分同一发行版本的不同源码；它不是 Git 提交号，也不能证明进程启动后文件未被修改。

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
append = "上述内容如果是任务而非通知，完成后请通过talkback发送回执给我"
```

`agents` 是发送方名称范围；`targets` 是接收方名称范围，省略时为全部。所有命中的追加规则按配置顺序组成独立行，重试不会重复追加完整后缀块。

设置顶层 `recovery_rules = []` 或 `message_rules = []` 可分别关闭全部默认规则；如果提供自定义规则数组，会替换对应的整组默认规则。数组要放在所有 `[section]` 前面。

输入框占用检查、投递前后校验等保护不依赖策略规则，始终保留。

本地 Pane 发送后等待 5 秒，再检查当前可见输入框；如果识别到待发送消息仍在输入框，补发一次 Enter 并确认结果。发送后的 Codex 输入框出现 `[Pasted Content ... chars]` 折叠内容时也会补发 Enter；发送前已存在的折叠草稿仍视为占用。跨主机消息由目标机器的 Talkback 对本地 Pane 执行此检查。

### 无人值守时的输入框占用

当有消息等待本机 Pane 投递，且输入框被占用时，Talkback 每 30 秒检查当前可见画面。画面连续 5 分钟不变后再次确认，补按一次 Enter；无论草稿来源都会提交。只有确认输入框清空后才继续投递待发消息。画面任何变化均重新计时；服务重启后也重新计时。折叠内容无法确认是否改变，不触发该兜底；无法识别输入框或 Pane 离线时也不会盲目提交。跨机消息由接收端 Talkback 执行此处理。

可在 `[delivery]` 中设置 `stale_input_seconds = 300`；设为 `0` 关闭兜底。设置会对其他来源的未提交草稿产生提交效果。

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

未安装 Python 包时可在仓库目录使用 `python3 -m herdr_talkback` 代替 `herdr-talkback`。消息头支持 `→`/`->` 和 `节点/pane` 地址；跨主机通信需先配置对端。

独立 CLI 默认配置是 `~/.config/herdr-talkback/config.toml`；Windows 上是 `%APPDATA%\herdr\plugins\config\herdr-talkback\config.toml`。插件使用 `HERDR_PLUGIN_CONFIG_DIR` 与 `HERDR_PLUGIN_STATE_DIR`；初始配置中的 `{session}` 由 Herdr socket 路径的 hash 替换，使多个 Herdr 会话的数据库、队列和进程锁互不冲突。CLI 和插件必须连接同一个 `HERDR_SOCKET_PATH`。

## 逻辑状态监控与自动继续

可指定一个已运行 AI 的专用 pane 作为判断器，监控本机其他 pane。
先用 `herdr-talkback pane list` 查看 pane ID/名称，再在插件配置中加入：

```toml
[logic_monitor]
enabled = true
monitor_pane = "monitor"
targets = ["worker", "w2:p6"]
observation_seconds = 10
stable_seconds = 10
poll_seconds = 10
timeout_seconds = 120
cooldown_seconds = 120
max_continues_per_30m = 3
```

选择器使用 pane ID 或唯一 pane 名称；同名窗格不自动选择，monitor 不能监控自身。
monitor 必须已启动能理解提示词并输出 JSON 的 AI，保持空闲并专供此功能使用。
修改配置后重启 Talkback 服务。此功能默认关闭，不会自动创建 AI 或监控所有窗格。

Talkback 分别保存 `herdr_status`（Herdr 事实状态）和 `logic_status`（插件判断）。
事实为 `working`/`blocked` 时直接记录并暂停判断；其他状态下，仅使用当前可见的一屏内容
（最多 64 KiB，去除 ANSI、统一换行）稳定后交给 monitor，多个目标串行判断。
目标、monitor 和输入框检查都使用 `visible` 快照，不请求历史内容，不主动翻页或滚动。
内容变化进入 `init` 并重新计时；稳定后进入 `todo`，请求发出后为 `judging`。
每 10 秒读取 monitor 回答，120 秒未得到有效回答则记为 `timeout`。

判断结果为 `blocked`、`continue`、`waiting`、`done`、`unknown`。
需要用户决策、信息或明确授权时为 `blocked`；等待子代理/其他 pane 时为 `waiting`。
等待执行下一步，以及“任务完成，如果需要还可以做 xxx”，都按 `continue` 推进；
完成且没有后续动作才是 `done`。可恢复环境错误可以继续，永久访问/配置问题不默认重试。

结果带请求 ID，与目标快照绑定；旧回答、提示词回显及过期结果不触发输入。
执行前复查 pane 身份、内容、状态及输入框，仅在输入框为空时输入并提交 `continue`。
不清空草稿、不关闭弹层；同一快照最多尝试一次，重启或提交不确定时不自动重发。
与已有恢复规则共用每 pane 的执行锁和恢复计数。`waiting`、`unknown`、`timeout`
在冷却后可以重新判断；`blocked`/`done` 等待内容或事实状态变化。
此判断依赖 AI 和有限的终端快照，不能替代用户对后续任务范围的判断。

只读查看记录（不需要运行 Herdr socket）：

```bash
herdr-talkback --config "$CFG" monitor status
herdr-talkback --config "$CFG" monitor status --json
```

状态查询显示事实/逻辑状态、时间、快照 hash 和执行结果，不输出原始终端内容。
终端快照和判断记录保存在本机状态数据库，按本机私有数据管理。

运行中的逻辑监控可以通过命令立即关闭或启用：

```bash
herdr-talkback --config "$CFG" monitor disable
herdr-talkback --config "$CFG" monitor enable
herdr-talkback --config "$CFG" monitor state
```

在 Herdr 会话中省略 `--config` 时，`monitor` 命令优先查找当前 Herdr 的插件配置；
如需控制独立 CLI 配置，使用 `--config` 显式指定。`monitor --disable` 和
`monitor --enable` 分别与 `monitor disable`、`monitor enable` 等价。

命令只控制逻辑监控，原有恢复规则和消息服务继续运行。启用前必须在配置中指定
`monitor_pane` 和 `targets`。命令的选择保存在当前 Herdr 会话状态数据库中，重启后
继续生效，并优先于配置文件的 `enabled`；`monitor state` 显示保存的选择和服务是否
运行，并区分实际监控任务是否运行。启停命令要求服务正在运行；服务未运行时返回错误。

## 注册通信名称

给本机或已配置的远端节点登记容易记忆的名称，供 `send` 使用：

```bash
herdr-talkback --config "$CFG" pane register local_dev pc55/ast-dev1
herdr-talkback --config "$CFG" pane register remote_lead openclaw/lead
herdr-talkback --config "$CFG" pane registered --json
herdr-talkback --config "$CFG" send '[ast-dev3->remote_lead] 消息内容'
herdr-talkback --config "$CFG" pane unregister remote_lead
```

`节点/pane` 是统一的逻辑地址：本机节点走本地 Herdr socket，远端节点使用
`[peers.节点]` 中配置的 HTTPS 地址、端口和认证。`pane registered` 显示名称、
逻辑地址、本地/远端及远端 URL；登记信息仅保存在当前机器和 Herdr 会话的状态
数据库中，不会自动同步到另一台机器。远端节点必须先在配置中设置；登记时要求能
连接本地 Herdr，并拒绝与现有本机 pane 或配置别名冲突的名称。登记名称用于普通队列
投递，不支持 `--direct`。

### 批量登记、whoami 和人工转发

```bash
herdr-talkback --config "$CFG" pane register-all
herdr-talkback --config "$CFG" whoami monitor 192.168.90.18 lead
herdr-talkback pane identity monitor
# 把上一条输出的 JSON 转发给对方，对方执行：
herdr-talkback --config "$CFG" pane import '<identity-json>'
```

`register-all` 按项报告未命名或冲突的 Pane，不覆盖已有登记。`whoami` 在发送端登记目标，接收端认证发送节点后登记发送方并投递消息。查询返回的消息 ID 以确认最终状态。双方都需要新版 Talkback 及已配置的可信 peer。`identity` 自动查找当前 Herdr 插件配置，并使用其明确的监听地址和端口；监听地址是通配地址或仅本机可访问时，用 `--url` 显式指定可访问的 HTTPS 地址。身份卡不包含凭据，也不自动创建 peer。身份卡返回本机公开证书路径和按 peer 区分的 token 路径，路径只在发送机器上有意义；需要另行通过可信方式交换文件。首次生成插件配置时使用本机 hostname 作为节点 ID；既有配置不会自动改名。

### 远程服务管理

接收端可在特定 peer 的配置中加入 `allow_remote_control = true` 并重启 Talkback，允许该 peer 读取输入框、提交输入和更新安装。默认为关闭。使用独立 token 区分 peer；多个 peer 共享 token 时不能仅靠节点名区分其身份。

```bash
herdr-talkback peer input pc-54 arc-bus
# 使用上面返回的 pane_id 和 screen_sha256：
herdr-talkback peer submit pc-54 arc-bus --pane-id <pane-id> --screen-sha256 <hash>
herdr-talkback peer submit-status pc-54 <request-id>
herdr-talkback peer update pc-54
herdr-talkback peer update-status pc-54 <request-id>
```

远程检查只读当前可见画面，`visible_excerpt` 可包含输入区后的界面信息；折叠的正文无法直接读取。提交前必须核对返回的输入；画面或 Pane 身份改变会被拒绝。画面校验和发送按键之间不是原子操作，仍存在并发输入竞态。折叠内容默认允许提交，但只能校验可见画面，无法确认折叠正文是否变化；可使用 `--reject-folded` 拒绝此类提交。按键接受不代表已被 AI 处理。每次提交保存唯一请求 ID；超时时查询原 ID，不要盲目使用新 ID 重试。

`peer update` 仅从固定的 GitHub 项目构建并更新远程服务所使用的 Python 安装，不修改远程 Git 工作树、用户配置和凭据。安装后自动重启服务；有消息正在投递时不切换。状态为 `completed` 表示服务已恢复；`failed` 需检查状态。该操作不更新 Herdr 插件清单的登记元数据或已安装的 Skill；如清单变更则需另行更新。对端需先安装并启动支持该功能的版本。

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
node_id = "pc55"

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
`[peers.pc55]`，URL 为 `https://192.168.60.55:18765`，CA 指向 Windows 的公开证书。
有历史消息的节点保持原本的 `core.node_id`；两端 ID 必须不同。
证书必须包含 URL 中的 IP/主机名，不能关闭证书验证。
配置中已有 `[core]` 时修改原表，不要重复声明。

两端防火墙只向对端 IP 放行该 TCP 端口。修改配置后先运行插件 `check`，再关闭
Talkback 服务 pane 并执行 `start`。每个网络端口属于一个 Herdr 会话，多会话须使用
不同的配置和端口；离线对端不会阻止本地服务启动。

链路异常时先跑诊断，它会逐层报告第一个失败点（TCP、TLS 握手与证书指纹、
证书 SAN 与 URL 是否匹配、免凭据 `/v1/ping`、带 token 的 `/v1/info`、本机
凭据文件健康度）：

```bash
herdr-talkback peer diag pc55          # 人类可读
herdr-talkback peer diag pc55 --json   # 供脚本使用
```

`/v1/ping` 不需要证书和 token，可在凭据交换完成前验证网络是否可达；对端版本
较旧时会返回 401，诊断会标注为"对端尚未提供该探针"而不是链路失败。报告不会
输出 token 内容或私钥，只给文件路径、证书 SHA-256 指纹和人工交换说明。

```bash
CFG="$(herdr plugin config-dir herdr-talkback)/config.toml"
herdr-talkback --config "$CFG" peers list
herdr-talkback --config "$CFG" peers check openclaw
herdr-talkback --config "$CFG" send '[talkback->openclaw/worker] 测试消息'
herdr-talkback --config "$CFG" message status msg_xxx --json
```

反向发送在 Linux 端执行，例如 `[worker->pc55/talkback] 收到`。
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
