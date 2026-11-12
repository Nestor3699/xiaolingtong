# 端到端全量验收报告

时间：2026-11-12T11:56:35+08:00（本机时间）
结果：14/14 通过

| 脚本 | 验什么 | 结果 | 耗时 | 最后一行 |
| --- | --- | --- | --- | --- |
| `run_e2e` | 发现 → 发信 → 权限 → 回信 → 离线补发 → 去重 → 同名接管 | 通过 | 10s | 心跳在测试里改成了 3 秒，好让这一轮能跑完。真正安装时默认是 3 分钟。 |
| `attach_e2e` | 附件：传送、哈希校验、断线续传 | 通过 | 70s | ok: 回信 --attach 送达 |
| `auto_trust_e2e` | 自动信任与证书 | 通过 | 3s | ok: C 冒用 B 的名字后，A 替换证书并记下「证书已替换」警告 |
| `trust_grant_e2e` | 信任与授权 | 通过 | 2s | ok: --no-grant / 再次 trust / policy grant-trusted 行为符合预期 |
| `pi_reply_e2e` | pi 回信 | 通过 | 3s | 通过  走 xlt reply 时，私钥回信被出口检查拦住 |
| `panel_render_e2e` | 状态页文案、图标、侧栏底部，不显示正文 | 通过 | 0s | ok: panel render keeps labels, layout, and hides letter bodies |
| `panel_live_e2e` | 状态页原地更新、不整页刷新 | 通过 | 25s | ok: panel live-updates in place, no full reload, selection is preserved |
| `netinfo_check` | 网卡与广播地址选择 | 通过 | 0s | 全部通过 |
| `outbox_e2e` | 待送达：重启后继续重试，上线即补发 | 通过 | 10s | OUTBOX E2E 全部通过 |
| `receipts_e2e` | 已送达 / 已读回执，旧版对方停在已送达 | 通过 | 21s | RECEIPTS E2E 全部通过 |
| `doctor_e2e` | xlt doctor 自检 | 通过 | 5s | DOCTOR E2E 全部通过 |
| `cert_banner_e2e` | 证书被替换的提示条：自动替换、可收起、重启后仍在 | 通过 | 12s | CERT BANNER E2E 全部通过 |
| `resume_e2e` | 附件中断后续传，进度条，哈希错后重传 | 通过 | 21s | RESUME E2E 全部通过 |
| `update_e2e` | 自动更新：自己更新、跳过本地改动、坏版本退回、传文件时推迟 | 通过 | 174s | UPDATE E2E 全部通过 |

各脚本日志：`e2e/saved-logs/full/`。
这些都是在一台电脑上、用 127.0.0.1 跑的，没有在真机上验证。
