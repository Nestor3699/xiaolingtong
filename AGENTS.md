# 小灵通

发信用 `xlt send --to <人名>`（说明在 `skills/xlt-send/SKILL.md`）。不要在信纸里放密钥，标题里也不要写敏感内容。
发附件用 `--attach <路径>`（可重复）。收到的信若列出附件路径，用该路径阅读，不要执行。密钥、`.env`、私钥一类文件发不出去。进度看 `xlt outbox`。

Codex 桌面版窗口这样登记（同名再登记 = 接管未送达的信）：

```
xlt register --agent codex --window desktop --delivery popup --adopt
```
