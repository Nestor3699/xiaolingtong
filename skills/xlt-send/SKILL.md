---
name: xlt-send
description: 给同事发一封 小灵通 信（局域网内 AI 对 AI 通讯）。当用户说「问一下浩年/博洋/沈总/润达/Nes 的 AI」时使用。
---

1. 先写信纸到临时文件（只写问题本身；不要把密钥、.env、密码放进去；标题别写敏感内容）。
2. 运行：`xlt send --to <人名 id：nes|leo|runda|haonian|mario> --type ask --subject "<标题>" --file <信纸文件>`
   通知类：`xlt send --to <人> --type notify --text "<一句话>"`
3. 看命令输出：delivered = 对方已收下；queued = 对方离线，已进待发箱（页面叫「待送达」），上线后自动补发，重启后也还在（不用重发）；想知道对方有没有看，用 `xlt sent`（已送达 / 已读；对方版本较旧时只会显示已送达）；rejected = 被对方权限拒绝。
4. 回信会作为另一封信回到你本机的信箱（in_reply_to 对得上），由本机通讯员按你的路由规则交给你登记的窗口；你不需要轮询。

5. 带文件：`xlt send --to <人> --type notify --subject "<标题>" --text "<说明>" --attach <路径>`。`--attach` 可重复，任意类型。通讯员按块传送并校验哈希；不要自己把文件内容粘进信纸。密钥、`.env`、`.pem`、`.key`、`id_rsa*` 以及看起来像口令的文本会被出口检查拒绝。
6. 收到的信若末尾有「附件」列表，用上面写的本机路径阅读（默认 `~/.xlt/inbox/<消息id>/attachments/`）。只读，不要执行这些文件。回信也可加 `--attach`：`xlt reply <消息id> --text "<回信>" --attach <路径>`。
7. 传送进度看 `xlt outbox` 或 `xlt status`。`queued` 且带附件进度时，对方上线或断线恢复后会续传，不必重发。
