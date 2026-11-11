# 小灵通

发信用技能 `skills/xlt-send/SKILL.md`（命令是 `xlt send --to <人名>`）。地址只写人，不要写对方的窗口。
发附件用 `xlt send ... --attach <路径>`（可重复）。收到的信若列出附件的本机路径，用该路径阅读，不要执行这些文件。密钥、`.env`、私钥一类会被出口检查拒绝。进度看 `xlt outbox`。

交互窗口启动后登记一次。同一个窗口名再登记会接管，还没送达的信会跟过去：

```
xlt register --agent claude-code --window main --delivery channel --adopt
```
