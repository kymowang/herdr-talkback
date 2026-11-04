# 安装 Herdr Talkback 和 skill

支持 Windows、Linux 和 macOS。需要 Python 3.9+ 和已安装的 Herdr 0.9.1+。
先启动 Herdr；解压完整 ZIP，在该 Herdr 会话的终端中运行：

```text
python install.py
```

Linux/macOS 如使用 `python3`，运行 `python3 install.py`。脚本安装 Talkback、
跨机通信依赖及 Codex skill，并将插件链接到 Herdr。默认安装会从配置的 Python
包源获取依赖，因此需要网络或可用的内部包源。仅需本机通信时可用：

```text
python install.py --local-only
```

Herdr 不在 PATH 时使用 `--herdr /实际路径/herdr`（Windows 指向 `herdr.exe`）。
如果已有不同内容的 Talkback skill，脚本会停止；确定替换时使用 `--replace-skill`。
安装输出会显示 Talkback 命令完整路径和插件目录。将输出的命令所在目录加入 PATH，
或直接使用完整路径；之后新启动的终端才会继承更新的 PATH。

安装不修改已有的 Talkback 配置、状态数据库、网络证书或 token。新安装的跨机
通信仍需在配置文件中设置 peer、证书、token 和端口。若已有运行中的 Talkback
服务，安装后关闭旧服务窗格，再执行输出中的 `check` 和 `start` 命令。
