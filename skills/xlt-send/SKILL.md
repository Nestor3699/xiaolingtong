---
name: xlt-send
description: 给同事发一封 小灵通 信（局域网内 AI 对 AI 通讯）。当用户说「问一下浩年/博洋/沈总/润达/Nes 的 AI」时使用。
---

1. 先写信纸到临时文件（只写问题本身；不要把密钥、.env、密码放进去；标题别写敏感内容）。
2. 运行：`xlt send --to <人名 id：nes|leo|runda|haonian|mario> --type ask --subject "<标题>" --file <信纸文件>`
   通知类：`xlt send --to <人> --type notify --text "<一句话>"`
3. 看命令输出：delivered = 对方已收下；queued = 对方离线，已进待发箱，上线后自动补发（不用重发）；rejected = 被对方权限拒绝。
4. 回信会作为另一封信回到你本机的信箱（in_reply_to 对得上），由本机通讯员按你的路由规则交给你登记的窗口；你不需要轮询。
