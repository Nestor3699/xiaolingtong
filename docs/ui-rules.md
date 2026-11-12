# 小灵通状态页 UI 规则

这份说明是给之后改界面的人看的。数字都尽量写上来源：是从哪一页、哪份样式文件，或哪一张截图里量到的。没能直接看到的，会标明「推测」。

小灵通是自己的工具，只借用这套干净、紧凑的浅色界面习惯，**不使用也不出现对方的产品名、商标、图标和专有字体**。正文用 Inter，并加上系统和中文字体作为后备。页面不联网取字体、图标或脚本：Inter 没装时直接用系统字体，图标是内联 SVG，断网也能用。

## 1. 我们实际分析了什么

目标页是文件列表里的「最近查看」：

`https://www.figma.com/files/team/1626487634443124797/recents-and-sharing/recently-viewed`

分两轮：

**第一轮（未登录）。** 用 REA（`rea-agents` 6.8.0）配合独立的无头 Chrome，对公开页做了 `inspect-web-page` 和 `capture-web-screenshot`。未登录时，「最近查看」会跳到登录页，看不到侧栏和文件列表。登录页加载了产品样式表，里面有完整的色阶和字体代币。

**第二轮（已登录）。** Nes 在盒子浏览器里登录后，留下了全屏截图（`/workspace/figma-capture/`）。保存页面 HTML 因托管 Chrome 策略失败，所以这一轮只能量像素，再和 auth.css 里的代币对照。

| 截图 | 内容 |
| --- | --- |
| `recents-list.png` | 最近查看，列表模式 |
| `recents-grid.png` | 最近查看，卡片网格 |
| `hover.png` | 列表里悬停在「flow chart」那一行 |
| `selected.png` | 同一行被选中（蓝色描边） |
| `drafts.png` | Drafts 页（无顶部 AI 条，侧栏「Drafts」为选中） |

这五张图都是 **1920×1200** 的 PNG。任务说明里写的是 1280×800，并估计顶上浏览器栏约 58px、底下任务栏约 28px；那正好是把 1920×1200 按 1.5 倍缩小后的数（87÷1.5=58，41÷1.5≈27）。但图里的侧栏是整 240px、图标整 16px、内边距整 8px，都落在 4/8px 网格上。若按 1.5 倍除回去，会得到 160、10.67、5.33 这种对不齐的数。所以下面把 **1 个图片像素当作 1 个 CSS 像素**（1920×1200 窗口、100% 缩放）。

截图包含 Windows 的 Chrome 外壳和任务栏，量产品界面时已扣除：

| 区域 | 像素（图片坐标） | 高度 |
| --- | --- | --- |
| Chrome 标签 + 地址栏 | y=0–86 | 87px |
| 产品界面 | y=87–1158 | 1072px |
| 任务栏 | y=1159–1199 | 41px |

来源：五张截图左右边缘的颜色分界，彼此一致。

### 主要材料

- 产品样式表（第一轮 REA 在登录页列出，随后下载）：  
  `https://www.figma.com/webpack-artifacts/assets/auth-773366dfd34720ef.min.css`  
  约 641 KB。下文简称 **auth.css**。
- 登录页上实时算出来的 CSS 变量和控件尺寸（CDP `getComputedStyle`，视口 1440×813）。简称 **登录页实测**。
- 上面五张已登录截图的像素测量。简称 **截图实测**，并写明文件名。
- 介绍站样式只有专有字体声明，**不作为状态页依据**。

auth.css 里有一段 `[data-preferred-theme=light][data-editor-theme=bake-filebrowser]`，会把「选中」改成灰色。**已登录的「最近查看」页面并没有用这套灰色选中**：侧栏选中是浅蓝底，列表选中是蓝描边白底。状态页按截图实测为准，代币只用来对照颜色从哪一级色阶来。

## 2. 颜色

下列十六进制来自 auth.css 的 `:root` 色阶（`--ramp-*`），语义名来自浅色主题。登录页实测把这些变量解析成了同样的值。截图里看到的颜色在表中另注。

### 中性色阶（`--ramp-grey-*`、`--ramp-white-1000`、`--ramp-black-*`）

| 代币 | 值 | 用途（结合截图） |
| --- | --- | --- |
| grey-100 | `#f5f5f5` | 搜索框底、顶栏类型胶囊、标签选中底。**不是**侧栏底 |
| grey-200 | `#e6e6e6` | 默认描边。截图里分隔线实测为 `#e7e7e7`（`recents-list.png` x=240） |
| grey-300 | `#d9d9d9` | 禁用底 |
| grey-400 | `#b3b3b3` | 更淡的禁用 |
| grey-500 | `#757575` | 中灰；列表表头/日期大约是 `#808080`–`#8b8b8b`（`recents-list.png`） |
| grey-600 | `#444444` | 较深中灰 |
| grey-700 | `#383838` | 深灰 |
| grey-800 | `#2c2c2c` | 登录页主按钮底 |
| grey-900 | `#1e1e1e` | 接近黑 |
| grey-1000 | `#111111` | 最深灰 |
| white-1000 | `#ffffff` | 主背景、**侧栏底**、列表行底（`recents-list.png`） |
| black-100 | `#0000000d`（约 5%） | 登录输入框底 |
| black-200 | `#0000001a`（约 10%） | 半透明描边 |
| black-400 | `#0000004d`（约 30%） | 第三文案 |
| black-500 | `#00000080`（50%） | 次级文案 |
| black-800 | `#000000e5`（约 90%） | 主文案（文件名接近纯黑） |

### 语义色与截图对照

| 代币 / 现象 | 值 | 来源 |
| --- | --- | --- |
| `--color-bg` | `#ffffff` | auth.css；侧栏和主区都是白（`recents-list.png`） |
| `--color-bg-secondary` | `#f5f5f5` | 搜索框、选中标签（`recents-list.png`） |
| `--color-bg-selected`（默认浅色） | `#e5f4ff`（蓝 200） | auth.css；登录页实测。侧栏选中项实测为 `#e4f5ff` / `#e6f4ff`（`recents-list.png`、`drafts.png`），按这个 |
| `--color-bg-selected`（bake-filebrowser） | `#e6e6e6` | 只写在 auth.css 里，**这几张最近查看截图没用到** |
| `--color-text` | `#000000e5` | 登录页实测 |
| `--color-text-secondary` | `#00000080` | 登录页实测；表头接近这个灰 |
| `--color-border` | `#e6e6e6` | 登录页实测；截图分隔线 `#e7e7e7` |
| 列表行选中描边 | `#328dd5`（众数，邻近还有 `#308bd3`、`#2f8cd3`） | `selected.png` 描边像素。比色阶里的 `#0d99ff` / `#007be5` 稍暗，以截图为准 |
| 升级按钮（侧栏底部，仅作对照） | `#0b99ff` / `#0c98ff` | `recents-list.png`，对齐 `--ramp-blue-500` `#0d99ff` |
| 顶栏 AI 条（不采用） | `#f5f3fb` / `#f6f4fe` | `recents-list.png`、`recents-grid.png`。状态页不做这条 |

### 功能色（色阶，auth.css `:root`）

| 角色 | 100 / 200（浅底） | 500（主色） | 600（文字/悬停） |
| --- | --- | --- | --- |
| 蓝 | `#f2f9ff` / `#e5f4ff` | `#0d99ff` | `#007be5`（登录页链接实测偏 `#0768cf`） |
| 绿（成功、在线） | `#edf9ef` / `#e0f6e5` | `#14ae5c` | `#009951` |
| 红（危险、失败） | `#fff5f5` / `#ffe2e0` | `#f24822` | `#dc3412` |
| 黄（警告、等待） | `#fffbeb` / `#fff1c2` | `#ffcd29` | `#ffc21a`；最深文字可用 `#b86200` |

徽章用浅底（200）加深色文字（600/700）。文件浏览器主题里的紫色选中（`--ramp-violet-500: #4d49fc`）在这几张「最近查看」截图里没有出现，状态页也不用紫色做品牌色。

## 3. 字体

auth.css `:root`：

- `--font-family-default`：`"Inter", ui-sans-serif, system-ui, -apple-system, "BlinkMacSystemFont", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "Noto Sans", sans-serif`
- `--font-family-mono`：`"Roboto Mono", "Monaco", "Courier New", monospace`
- `--font-weight-default`：`450`
- `--font-weight-strong`：`550`

登录页标题和按钮实际用的是专有字体「Figma Sans」。**状态页禁用专有字体**，改用 Inter，中文后备为 `Noto Sans SC`、`PingFang SC`、`Microsoft YaHei`。

### 字号阶梯（auth.css `:root` 的 `--text-*`）

按 16px 根字号换算：

| 名称 | 字号 | 行高 | 字重 |
| --- | --- | --- | --- |
| body-small | 9px（`.5625rem`） | 14px | 450；加粗 500 |
| body-medium | 11px（`.6875rem`） | 16px | 450 / 550 |
| body-large | 13px（`.8125rem`） | 22px | 450 / 550 |
| heading-small | 13px | 22px | 550 |
| heading-medium | 15px（`.9375rem`） | 25px | 550 |
| heading-large | 24px（`1.5rem`） | 32px | 550 |
| mono-medium | 11px | 16px | 400 |

### 截图里估的字号

用字形墨水的高度反推（大写/汉字大约是字号的 0.7 倍），均来自 `recents-list.png`，必要时对照 `drafts.png`：

| 元素 | 墨水高度 | 估计字号 | 对应代币 |
| --- | --- | --- | --- |
| 主区标题「Recents / Drafts」 | 9px（y=107–115） | 13–16px | heading-small / 略大一点的标题 |
| 侧栏工作区名、侧栏项 | 6–9px | 11–13px | body-medium 到 body-large |
| 标签「Recently viewed」 | 10px（y=307–316） | 13–14px | body-large |
| 列表文件名 | 8px（y=403–410） | **11px** | body-medium |
| 列头「Name」等 | 8px（y=359–366） | **11px** | body-medium，灰色 |
| 日期「5 months ago」 | 10px | 11px 左右 | body-medium，灰色 |

文件列表的默认密度就是 **11px 正文**，页标题不必用 24px 的 heading-large。状态页：页标题 15px（heading-medium），侧栏和标签 13px，表头/编号/时间 11px，人名和信件标题 13px（中文在 11px 上偏吃力，略放大一档，仍用同一套阶梯）。

## 4. 间距、圆角、描边、阴影

来自 auth.css `:root`（代币），并与截图对照：

| 代币 | 值 | 截图里对上的地方 |
| --- | --- | --- |
| spacer-1 / 4px | 4px | 选中列表行的描边相对行盒子上下各缩进 4px（`selected.png`） |
| spacer-2 / 8px | 8px | 侧栏左右内边距；导航选中块的左右缩进（`recents-list.png`） |
| spacer-2-5 / 12px | 12px | 工作区头像大约距左 12px |
| spacer-3 / 16px | 16px | 主区标签距侧栏分隔线 32px（两个 16）；文件图标 16px |
| spacer-4 / 24px | 24px | 标签行高 24px；工作区头像 24px |
| spacer-5 / 32px | 32px | 侧栏导航行距 32px；顶栏类型胶囊约 32px 高；卡片间距 32px |
| radius-small | 2px | — |
| radius-medium | 5px | 可能用于较小的块 |
| 列表选中描边圆角 | 约 8px | `selected.png` 左上角：描边外沿从 x=280（顶）收到 x=274（直边），约 6–8px |
| radius-large | 13px | 网格卡片圆角约 12–13px（`recents-grid.png` 第一张卡左上） |
| radius-full | 9999px | 24px 高的标签、32px 高的搜索框和升级按钮，看起来是胶囊 |

描边：侧栏分隔线是 **1px** `#e7e7e7`（`recents-list.png` x=240，几乎贯穿产品区）。列表选中是 **2px** 实线蓝框（`selected.png` y=482–483 与 y=520–521 各两行实色）。卡片外框约 1px `#e5e5e5`–`#e7e7e7`（`recents-grid.png`）。

阴影：登录页按钮和输入框实测 `box-shadow: none`。文件列表行和侧栏也没有可见阴影（截图）。elevation 代币留给菜单和对话框。

过渡：`--duration-sm: .1s`，`--duration-md: .2s`（auth.css）。

## 5. 控件

登录页的大表单（48px 按钮、53px 输入）是账号页，不是文件列表。文件列表里量到的是下面这些。

### 搜索框（`recents-list.png` 侧栏）

- 背景 `#f5f5f5`，左右各缩进 8px。
- 高度约 32px（灰底大约在 y=136–166）。
- 看起来是胶囊圆角。

### 顶栏类型胶囊（Design / FigJam 等，仅作对照）

- 约 32px 高，背景 `#f5f5f5`，彼此隔开（`recents-list.png` y=95–125）。状态页可用同样手法放统计数字，但不复制这些产品名。

### 图标

状态页的图标一律用 Lucide（ISC 许可证），内联 SVG，见第 9 节。下面的尺寸来自截图实测。

- 文件类型图标：**16×16**（`recents-list.png` 第一行图标 x=282–297、y=398–413）。
- 侧栏导航图标约 12–16px 高，行距 32px；工作区头像 **24×24**（x=12–35、y=99–122）。
- 悬停/选中时，行左侧出现星标（约 12–16px，x≈252–263，`hover.png` / `selected.png`）。

### 升级按钮（仅作对照，状态页不用）

- 侧栏底部，高约 32px（y=1101–1132），左右各缩进 16px，宽约 208px，胶囊，颜色 `#0d99ff` 一带（`recents-list.png`）。

### 徽章

11px 字、左右 8px 内边距、胶囊或 5px 圆角，浅色底 + 深色字。

## 6. 左侧栏 + 顶栏 + 标签 + 列表（截图实测）

以前只能推测的数字，现在全部从截图量出。

### 侧栏（`recents-list.png`，并用 `drafts.png` 核对选中）

- 宽 **240px**（x=0–239）。主区从 x=241 开始。
- 背景 **#ffffff**，不是灰底。
- 右边一条 1px 分隔线，颜色 `#e7e7e7`（x=240）。
- 左右内边距 **8px**（选中块和搜索框都从 x=8 画到 x=231）。
- 顶部是工作区切换（24px 头像 + 名称），高度和主区顶栏对齐，约 48–49px。
- 其下是 32px 高的搜索框。
- 导航行距 **32px**（Recents / Drafts / Community 等图标中心大约每隔 32px）。
- 选中项是一块 **28px** 高的浅蓝底（`#e4f5ff` / `#e6f4ff`），在 32px 行里上下各留约 2px，左右各留 8px。`recents-list.png` 里 Recents 在 y=177–204；`drafts.png` 里 Drafts 在 y=209–236，同样是 28px、同样的浅蓝。
- 导航文字大约从 x=50 起（16px 边距 + 16px 图标 + 8px 空隙）。

### 主区顶栏（`recents-list.png`、`drafts.png`）

- 高 **49px**（y=87–135，白底）。和下面的 AI 条之间没有明显分隔线，只是颜色接上。按 48px 做也合理，差 1px。
- 左侧页标题（「Recents」「Drafts」）约 13–16px、字重偏强，文字从 x≈274 起（距侧栏分隔线 32px）。
- 右侧是一排类型胶囊，状态页可改放统计数字。

（截图里顶栏下面有一条「Describe what you want to build」的淡紫 AI 条，高约 56px 色块再加输入框。状态页不做这条。）

### 标签行（Recently viewed / Shared files / Shared folders）

来源：`recents-list.png`、`recents-grid.png`。

- 选中标签「Recently viewed」是一块 **24px** 高的浅灰底（`#f3f3f3`–`#f5f5f5`），大约 y=299–322，x=273–376（宽约 104px）。
- 距侧栏分隔线 **32px**（x=241+32=273）。
- 看起来是胶囊（24px 高，圆角接近 radius-full）。
- 未选中的「Shared files」「Shared folders」没有底，只是旁边的文字。
- 标签文字约 13px。

### 列表（`recents-list.png`、`hover.png`、`selected.png`）

- 列头在列表上方，约 11px 灰色（「Name」「Last modified」「Created」「Active in file」）。「Name」从 x≈275 起；「Last modified」约 x=1099；「Created」约 x=1362；「Active in file」约 x=1626。
- **行高 48px**。九个文件类型图标的顶端分别在 y=398、446、494、（暗色图标一行）、590、638、686、734、782，间隔全是 48px。
- 文件图标 16×16，垂直居中（第一行 y=398–413，上下各 16px）。
- 行底是 **白色**。行与行之间没有明显的 1px 分隔线。
- **悬停**（`hover.png`，对「flow chart」那一行）：和未悬停的 `recents-list.png` 相比，行底仍然是白的，没有换成 `#f5f5f5`。能看到的变化是左侧多了星标、名称右侧多了操作按钮。也就是说，列表悬停是「露出操作」，不是铺一层灰。
- **选中**（`selected.png` 同一行）：
  - 白底，加上 **2px** 蓝色描边，颜色众数 `#328dd5`。
  - 描边盒子 y=482–521（高 40px），相对 48px 行上下各缩进 4px；水平从 x=274 到约 x=1872。
  - 圆角约 8px。
  - 描边内侧是白的，不是浅蓝填充（中间 y=500、x=400–1000 的像素几乎全是 `#ffffff`；原先看到的浅青只出现在描边抗锯齿上）。
  - 选中时左侧星标也在。

### 网格（`recents-grid.png`，`drafts.png` 同样是卡片）

- 卡片 **294×225 px**。第一张大约 x=275–568、y=345–569。
- 水平、垂直间距都是 **32px**（下一张 x=601，下一行 y=602）。这一屏排 5 列。
- 圆角约 12–13px，浅灰描边 `#e5e5e5`–`#e7e7e7`。
- 上半是缩略图，底部约 36–40px 是标题和「Edited … ago」。
- 状态页以列表为主，网格尺寸留在这里，供以后做卡片视图时用。

## 7. 状态页怎么用这些规则

- 产品名只写「小灵通 / 通讯员」，用一个中文字做标记，不复制任何外部商标。
- 字体：Inter + 系统黑体后备。
- 240px 白底侧栏、1px `#e6e6e6` 分隔线、8px 内边距、32px 导航行、选中为 28px 高的浅蓝块（`#e5f4ff`）。
- 主区 48px 顶栏 + 24px 胶囊标签（选中 `#f5f5f5`）。
- 列表行高 48px，11–13px 字。悬停不铺灰底。若有焦点/选中，用 2px `#328dd5`、8px 圆角的描边，白底。
- 原有信息全部保留：统计数字、同事、待发箱、收信窗口、收到的信、被拒记录、睡醒说明、端口和启动时间。信封摘要照旧显示，正文不出现。
- 状态徽章：在线/已处理用绿 200+绿 600；等待用黄 200+深黄；失败/连不上用红 200+红 600；离线用灰。
- 证书替换警告用黄 200 底，全文提示保留。
- 不做 AI 输入条，不放外部产品的类型胶囊和升级按钮。
- 侧栏底部是带标签的「本机」信息，见第 9 节，不再是一整段文字。
- 证书被替换的提示是页面顶部的红色提示条，要点「知道了」才会收起，见第 9 节。

## 8. 若还要再补

侧栏宽度、行高、标签和选中描边已经从截图量过。若需要精确到亚像素的字重、hover 过渡曲线，或打开文件后的编辑器壳，需要在已登录会话里对页面再跑 REA（独立用户目录的 Chrome，`--remote-debugging-port` 只绑在 127.0.0.1）。不要把登录 cookie 或调试端口暴露到非本机地址。

## 9. 图标和侧栏底部（v2）

### 图标

- 只用开源免费图标，不手画、不用 emoji、不用字符画。当前用 [Lucide](https://lucide.dev)（ISC；少数源自 Feather 的是 MIT）。想换 Tabler（MIT）也行，同样内联。
- 不走 CDN、不装 npm 包。图标 SVG 由 `scripts/vendor_icons.py` 从固定提交的 Lucide 仓库取下，写进 `xlt/icons.py`；页面里用 `icon("名字", 尺寸)` 取。加新图标：把名字加进脚本的列表，重新跑一次脚本。
- 许可证原文在 `THIRD_PARTY_NOTICES.md` 和 `xlt/share/LICENSE-lucide.txt`，随代码一起发。换图标库时同步更新这两个文件。
- 统一规格：描边 1.75、线端圆角、`currentColor`（跟文字同色，选中或报警时自动变色）。导航和侧栏行里 16px，徽章里 12px，提示条里 18px。
- 图标只配文字，不单独出现。状态靠徽章文字和颜色，不靠图标本身（给色弱的人留余地）。
- 图标对照：同事 `users`、待送达 `clock`、已发出 `send`、收信窗口 `app-window`、收到的信 `inbox`、被拒记录 `shield-ban`；徽章里待送达 `clock`、已送达 `check`、已读 `check-check`、附件 `paperclip`；证书提示条 `triangle-exclamation-point`。

### 侧栏底部（原先一大段文字）

- 一行一件事：左边灰色小字标签 + 图标，右边值。行高 24px，11px 字，标签列 64px，窄屏 72px。悬停有浅灰底，和导航行一致。
- 顺序：机器名、主人、地址、指纹、网关、端口、版本。指纹只显示前 4 段，鼠标放上去看全部，旁边的复制按钮复制完整指纹。网关用绿点（运行中，下面一行小字写启动时间）或红点（没在运行）。端口分两行写 TCP、UDP。
- 睡醒检测说明是一个折叠项，默认收起；每 5 秒自动刷新时，展开状态会保留。
- 最底下一小行灰字：本页只在本机，以及本页的 127.0.0.1 端口。
- 截图（固定的夹具数据，用 `e2e/panel_screens.py` 重新生成）：`/workspace/xlt-ui/v2-sidebar.png`、`v2-sidebar-open.png`。

## 10. 版本与自动更新（v3）

- 侧栏「本机」块里，「版本」一行显示版本号和提交号（例如 `0.1.0 · e25ea45`，提交号用等宽字）；紧接着多一行「更新」，图标是 `refresh-cw`：第一行写「自动更新 开/关」，第二行小字写最近一次的结果和检查时间（刚更新成功、已是最新、更新失败已退回、有本地改动没更新、在传文件稍后更新、检查失败等）。鼠标放上去能看到完整的一句话。
- 自动更新或退回之后，页面顶部出现一条不挡操作的提示条（和证书替换提示条同一个位置、同一套样式，只是颜色不同）：成功是绿色（`refresh-cw` 图标），失败退回是黄色（`triangle-exclamation-point` 图标）。点「知道了」才收起，重启后不会自己消失，也不会因为 5 秒刷新而消失。
- 颜色沿用第 2 节的语义色：绿 = success，黄 = warning。不新增颜色。
- 截图（夹具数据，`e2e/panel_screens.py` 生成）：`/workspace/xlt-ui/v3-sidebar.png`、`v3-banner-updated.png`、`v3-banner-rollback.png`。

## 11. 开着的旧页面不能乱（v4）

状态页每 5 秒只把 `.app` 里的内容换成新的，`<head>` 里的样式表不会换。通讯员升级后，浏览器里开着的旧页面会得到「新内容 + 旧样式」，图标和文字错位、TCP/UDP 连成一串。规则：

- 样式表同时通过 `GET /local/panel.css?v=<版本>` 提供，并且 `<link>` 放在 `.app` 里面的最前面：旧页面换内容时会一起把新样式表带进来。
- 页面带 `<meta name="xlt-build">`（样式表 + 脚本的哈希）。新脚本发现版本变了，整页刷新一次（20 秒内不重复刷新，防止循环）。
- 关键布局直接写在元素上（内联样式）：图标的 `width/height/display/vertical-align`、导航里图标和文字的 flex 和间距、本机块的两列（`grid-template-columns: var(--kv-label,64px) 1fr`）、TCP/UDP 与网关的分行（`flex-direction:column`）、复制按钮 22×22、睡醒检测 `summary` 的 `display:flex; list-style:none`（只剩一个小三角）。样式表完全不生效时，这些也不乱。
- 本地接口的响应带 `Cache-Control: no-store`。
- 验证：`e2e/panel_stale_e2e.py`（真通讯员 + 真浏览器：旧版本页面遇上升级、样式变了自动刷新、没有样式表时的布局）。截图：`/workspace/xlt-ui/v4-*.png`（Chromium 和 WebKit，前后对比）。

## 12. 点开一封信看正文（v5）

列表里仍然只显示信封（标题、谁、状态、大小），正文不进每 5 秒刷新用的页面快照。要点开才去取正文。

- 在「收到的信」「已发出」「待送达」里点一行，或焦点在这一行时按 Enter，右侧滑出阅读面板（同一页，不跳走）。再点同一行、按 Esc，或点 ×，都会关掉。
- 地址变成 `#inbox:msg-…` / `#sent:msg-…` / `#pending:msg-…`。5 秒原地刷新和整页重新加载都要还能对着这封信；刷新不能把阅读区关掉，也不能把阅读区的滚动位置清掉。
- 阅读面板里有：来自/发给、类型、标题、时间、状态（收信是已读/未读，发出是待送达/已送达/已读等）、窗口/目录、消息编号、全文、附件列表（文件名、大小、本机路径）。附件只展示路径，页面不打开、不执行这些文件。
- 正文按普通文本处理：先把 HTML 转义，再允许非常简单的 markdown（标题、加粗、代码块、列表）。不要把信纸里的 HTML 当页面来跑。正文不在本机时写「正文已不在本机」，不要编造。信特别长时只显示前面一段，并注明完整内容在哪个文件。
- 打开一封还没读的收信时，用和 `xlt read` 相同的回执函数记为已读（`receipts.mark_read_local`，来源是 `panel`）。
- 正文只通过本机状态页接口 `GET /local/letter` 提供，要带和「知道了」一样的 `X-Xlt-Panel: 1` 头，绝不挂到局域网 TLS 端口上。
- 信纸位置：收到的在 `~/.xlt/inbox/<窗口>/msg-….letter.md`（也包括 `_hold`、`_expired`）；发出还没送到的在 `~/.xlt/pending/…/*.letter.md`。已发出只在 `sent.jsonl` 里留下信封时，常常已经没有信纸。附件在 `~/.xlt/inbox/<消息编号>/attachments/`。
- 图标继续用 Lucide：关闭是 `x`，附件是 `paperclip`。v4 的旧页面规则仍然有效（样式表链在 `.app` 里、关键布局写在元素上、版本号对不上就整页刷新一次）。
- 验证：`e2e/panel_letter_e2e.py`（真通讯员 + 真浏览器）。截图：`/workspace/xlt-ui/v5-letter.png`、`v5-letter-sidebar-reader.png`。
