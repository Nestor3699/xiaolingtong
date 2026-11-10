# 小灵通

发信用技能 `skills/xlt-send/SKILL.md`（命令是 `xlt send --to <人名>`）。地址只写人，不要写对方的窗口。

交互窗口启动后登记一次。同一个窗口名再登记会接管，还没送达的信会跟过去：

```
xlt register --agent claude-code --window main --delivery channel --adopt
```
