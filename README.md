# 小灵通通讯员（v0.1）

小灵通是给同一间办公室里五台电脑用的局域网通讯员。每台电脑跑一个常驻小程序。它不是 AI，不花 token。它只做几件事：证明「我是这台电脑」、在局域网里找到同事、把信送到对方电脑。对方关机或睡眠，信先留在你自己的待发箱里，对方一上线就自动补发。

人只写收信人的名字（`nes` / `leo` / `runda` / `haonian` / `mario`）。交给对方哪个 AI，由对方自己的规则决定。

**给同事电脑上的 AI：** 请先读 [DEPLOY-AGENT.md](DEPLOY-AGENT.md)，按那份说明从上到下逐步安装，不要改命令、不要发明开关。默认会在局域网里自动发现同事，不必先交换证书。人自己看这份 README 即可。

公开仓库：<https://github.com/Nestor3699/xiaolingtong>

这一版按《小灵通架构方案》v0.4 做，没有另起一套设计。

## 在自己电脑上跑起来

需要 Python 3.11 或更新。只强制依赖 `cryptography`。`zeroconf`（mDNS 兜底）和 `watchdog`（盯文件夹）都是可选的，这一版默认不用它们：发现走 UDP 广播，信箱用很轻的本地查看，不叫任何 AI。

```bash
git clone https://github.com/Nestor3699/xiaolingtong.git
cd xiaolingtong
python3 -m venv ~/.xlt/venv
~/.xlt/venv/bin/pip install -e .
~/.xlt/venv/bin/xlt init --me nes-mac --person nes --owner Nes
~/.xlt/venv/bin/xlt serve
```

然后打开 [本机状态页](http://127.0.0.1:47321)。页面只在这台电脑上，别人从局域网打不开。

`xlt init` 会生成本机证书。默认开启 `auto_trust`：通讯员在局域网里发现同事后会自动写入名单，不必再交换证书。**`.key` 私钥不要发给任何人。**

同一局域网里的任何设备都可以加入，也可以用已经在用的机器名替换该成员的证书；不信任的网络请把 `auto_trust` 关掉（`xlt init --no-auto-trust`，或把 `~/.xlt/config.json` 里的 `auto_trust` 设为 `false`），再按部署说明附录手工交换证书。

Windows 上把 `python3` 换成 `py -3`，目录是 `%USERPROFILE%\.xlt`。

## 把同事加进名单

默认不必手工登记：双方启动通讯员后会互相发现。认人靠证书指纹，不靠 IP。手工 `xlt trust` 仍然可用（关闭自动登记时必须用）：

```bash
xlt trust --name haonian-win --person haonian --owner 浩年 --os windows --crt haonian-win.crt --primary
```

五个人的机器名见 `examples/xlt-directory.example.yaml`。那个文件里的指纹是空的，不能直接当真正的名单。

## 发信、收信

```bash
xlt send --to haonian --type ask --subject "退款流程" --file question.md
xlt send --to haonian --type notify --text "周五评审改到三点"
xlt send --to haonian --type notify --subject "截图" --text "请看附件" --attach ./shot.png
xlt reply msg-... --text "超过 30 天要人工审核"
xlt reply msg-... --text "表格在附件里" --attach ./table.csv
xlt status
xlt inbox
xlt peers
```

对方在线而且权限允许：输出里是 `delivered`（对方已收下）。对方不在：`queued`，信在你的待发箱，上线后自动补发，不用重发。对方没给你权限：`rejected`。

带文件用 `--attach`（可重复，任意类型）。接收方保存在 `~/.xlt/inbox/<消息id>/attachments/`，信纸末尾会列出本机路径；只阅读，不要执行。传送进度看 `xlt outbox` / `xlt status`，中断后会续传。密钥、`.env`、私钥一类文件发不出去。

AI 发信时读 `skills/xlt-send/SKILL.md`。Claude 的项目说明在 `CLAUDE.md`，Codex 的在 `AGENTS.md`，里面有一句同名登记（再登记一次会接管还没送达的信）。

收信窗口要先登记。同名再登记就是接管：

```bash
xlt register --agent claude-code --window main --delivery channel
xlt register --agent claude-code --window duty --delivery headless --workdir ~/.xlt/workdir/duty
xlt register --agent pi --window main --delivery extension
xlt register --agent codex --window desktop --delivery popup --workdir ~/.xlt/workdir/codex
xlt register --agent deepseek --window duty --delivery headless --workdir ~/.xlt/workdir/dsh
xlt register --agent grok-bot --window default --delivery webhook --secret-file ~/.xlt/secrets/grokbot.webhook
xlt route default claude-code/main
xlt route add --type ask --to claude-code/duty
```

Grok 的密钥文件是纯文本两行，权限只给自己。网址和密钥不要写进日志、聊天或 git：

```
url=https://...
key=crsr_...
```

权限在 `~/.xlt/policy.yaml`，默认全部拒绝。自动发现或 `xlt trust` 都只授予普通写信权限：提问（`ask`）、通知（`notify`）、回信（`reply`），以及这些信上的附件。读文件、列目录、搜索以及其它权限一律保持关闭，只有本机主人能在政策里打开；对方不能通过广播或来信给自己加权限。已有安装可再跑一次 `xlt trust`，或执行 `xlt policy grant-trusted`。格式和方案 §5.5 一样。`xlt policy check` 会看一眼有没有把密钥目录写进允许列表。不想自动授予写信权限时用 `xlt trust --no-grant`。

## 五种 AI 怎么交到信

通讯员只负责把信放到本机信箱，再按窗口的投递方式碰一下。命令行参数按方案写死，没有另发明开关。

| 窗口 | 通讯员做什么 |
|---|---|
| Claude 交互窗口 `channel` | 把信封和信纸写进 `inbox/<窗口>/`。Claude 用 `xlt/share/xlt-channel.mjs` 自己来取。启动要带 `claude --dangerously-load-development-channels server:xlt` |
| Claude 值班 `headless` | `claude -p … --output-format json --permission-mode dontAsk --allowedTools Read`，不加 `--bare`。信从标准输入进去，第一行是程序写的「来自」 |
| pi 交互窗口 `extension` | 把信纸镜像到 `bridges/pi/inbox/`。用方案里实测过的 `xlt/share/xlt-pi-extension.ts`：`XLT_ROOT=~/.xlt/bridges/pi pi -e xlt/share/xlt-pi-extension.ts` |
| pi 值班 | `pi -p --tools read,grep,find,ls --session-id xlt-<窗口>`，或 `pi --mode rpc` |
| DeepSeek | `dsh --profile headless --approval-policy never -`（有会话 id 才加 `--session-id`）。不往 `dsh web` 那个网页会话里推 |
| Codex 桌面版 | 默认弹窗 + `codex://new?prompt=` 预填，人自己点发送。中文稍长就改成复制，状态是「待手动处理」。`codex exec` 只在窗口登记成 headless 时用，默认不自动转 |
| Grok Bot | `POST` 一个只有 `{"msg_id":"..."}` 的 JSON，请求头 `Authorization: Bearer <密钥>`。正文留在本机 |

pi 扩展在实测版上改了一处：`xlt_reply` 接受正式编号（中间有连字符），并调用本机 `xlt reply`，不自己写回信文件。

## macOS：登录后自己启动

把 `install/com.xiaolingtong.gateway.plist` 拷到 `~/Library/LaunchAgents/com.xiaolingtong.gateway.plist`，把里面的 `/Users/nes` 改成你的用户名。第一次先在终端前台跑一次 `xlt serve`，系统如果问「本地网络」或「接受传入连接」，点允许。

```bash
plutil -lint ~/Library/LaunchAgents/com.xiaolingtong.gateway.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xiaolingtong.gateway.plist
launchctl kickstart -k gui/$(id -u)/com.xiaolingtong.gateway
```

停掉：`launchctl bootout gui/$(id -u)/com.xiaolingtong.gateway`。

防火墙如果开着：系统设置 → 网络 → 防火墙，允许这个 Python。这套步骤没有在真 Mac 上执行过。

## Windows：专用网络、防火墙、登录自启

浩年、博洋两台。办公室网络必须是「专用网络」，不然别人连不进来，也收不到上线广播。路由器重启后 Windows 有时会把它改回「公用」，连不上时先看这个。

管理员 PowerShell 可以参考 `install/windows-startup.ps1`（放行 TCP 47800、UDP 47801，只限专用网络、只限本子网，并建一个登录时启动的任务）。第一次弹出防火墙提示时，只勾「专用网络」。

这些 Windows 命令没有在真机上跑过。

## 状态页上看什么

在线的同事、待发箱、每个窗口、每封信的状态（排队、处理中、已回复、待手动处理、被拒）。只显示信封：谁、何时、编号、标题、大小。不显示正文。页面是左边栏加紧凑列表的浅色布局；颜色和间距写在 [docs/ui-rules.md](docs/ui-rules.md)。

## 重跑验收

不写单元测试。验收是一份端到端脚本：在本机起 3 个通讯员（不同端口、不同目录），走发现 → 发信 → 权限 → 假适配器 → 回信 → 离线补发 → 去重 → 同名接管。

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python e2e/run_e2e.py
# 只检查状态页文案和“不显示正文”：
.venv/bin/python e2e/panel_render_e2e.py
```

结果在 `e2e/last-report.md`，当时的通讯员日志在 `e2e/saved-logs/`。测试里心跳改成了 3 秒，安装后的默认是 3 分钟。

## 这一版还没在真办公室验证的

- 真子网广播、macOS「本地网络」权限、Windows「专用网络」和防火墙。这里只用了 127.0.0.1。
- 睡醒：Linux 上没有 macOS 的 NSWorkspace，也没有 Windows 的电源广播。代码里这两种监听都写了，但这台机器用的是时钟跳变（间隔超过 45 秒）和 IP 变化。没有做唤醒网卡。
- 没有真的把信送进 Claude、pi、DeepSeek、Codex、Grok。端到端用的是假适配器，只验证通讯员把信交到了约定的位置、并按方案拼好了命令。
- 列目录和搜索接口还没做。读文件做了，并且硬黑名单（`.env`、私钥等）挡在权限清单前面。
- mDNS 默认关。要开的话：`pip install 'xiaolingtong[mdns]'`，再在 `~/.xlt/config.json` 里写 `"mdns": true`。
