# 小灵通状态页 UI 规则

这份说明是给之后改界面的人看的。数字都尽量写上来源：是从哪一页、哪份样式文件里量到的。没能直接看到的，会标明「推测」。

小灵通是自己的工具，只借用这套干净、紧凑的浅色界面习惯，**不使用也不出现对方的产品名、商标、图标和专有字体**。正文用 Inter，并加上系统和中文字体作为后备。

## 1. 我们实际分析了什么

目标页是文件列表里的「最近查看」：

`https://www.figma.com/files/team/1626487634443124797/recents-and-sharing/recently-viewed`

本机（分析用的盒子）没有该网站的登录态。用 REA（`rea-agents` 6.8.0）配合 Chrome DevTools，对已打开的页面做了 `inspect-web-page` 和 `capture-web-screenshot`。结果如下：

| 打开的地址 | 实际落到的页面 | 能否看到文件列表 |
| --- | --- | --- |
| 上面的「最近查看」链接 | 被重定向到登录页 ` /login?is_not_gen_0=true&resource_type=team` | **不能**。未登录看不到侧栏和文件列表 |
| `https://www.figma.com/login` | 登录页 | 能看到登录表单，并加载了产品样式包 |
| `https://www.figma.com/`、`/release-notes/` | 对外的介绍站（另一套 Next.js 样式） | 主要是营销页，不是文件列表 |
| `https://www.figma.com/community` | 这次返回了 CDN 错误页 | 未采用 |

因此：**登录后的「最近查看」页面本身没有被分析到。** 规则主要来自登录页加载的同一份产品样式表，里面已经带有文件浏览器主题的变量。若要补上侧栏宽度、行高、图标排布这些只能从真页面量到的数字，需要在已登录的浏览器里再跑一遍 REA（例如 Nes 自己的 Mac）。

### 主要材料

- 产品样式表（登录页经 REA 列出的资源，随后下载）：  
  `https://www.figma.com/webpack-artifacts/assets/auth-773366dfd34720ef.min.css`  
  约 641 KB。下文简称 **auth.css**。
- 登录页上实时算出来的 CSS 变量和控件尺寸（CDP `getComputedStyle`，视口 1440×813）。简称 **登录页实测**。
- REA 对登录页的无障碍树：能看到按钮、输入框、标题，没有侧栏或文件行。
- 介绍站样式 `https://www.figma.com/_netlify/_next/static/css/617e0bc399c83bea.css` 只有专有字体声明，**不作为状态页依据**（专有字体不能用）。

auth.css 里除了默认浅色主题，还有一段专门给文件浏览器的覆盖：

`[data-preferred-theme=light][data-editor-theme=bake-filebrowser]`

这段把「选中」「主按钮」从蓝色改成了更安静的灰色，和文件列表那种浅色工具更接近。状态页按 **浅色默认代币 + 文件浏览器的选中色** 来做。

## 2. 颜色

下列十六进制来自 auth.css 的 `:root` 色阶（`--ramp-*`），语义名来自浅色主题（`:root` 与 `[data-preferred-theme=light]` 一致）。登录页实测把这些变量解析成了同样的值。

### 中性色阶（`--ramp-grey-*`、`--ramp-white-1000`、`--ramp-black-*`）

| 代币 | 值 | 用途 |
| --- | --- | --- |
| grey-100 | `#f5f5f5` | 次级底、悬停底、侧栏底 |
| grey-200 | `#e6e6e6` | 默认描边、第三底、文件浏览器里的「选中」底 |
| grey-300 | `#d9d9d9` | 禁用底、选中后按下 |
| grey-400 | `#b3b3b3` | 更淡的禁用 |
| grey-500 | `#757575` | 中灰 |
| grey-600 | `#444444` | 较深中灰 |
| grey-700 | `#383838` | 深灰 |
| grey-800 | `#2c2c2c` | 登录页主按钮底、深描边 |
| grey-900 | `#1e1e1e` | 接近黑的面板底（菜单，本页不用） |
| grey-1000 | `#111111` | 最深灰 |
| white-1000 | `#ffffff` | 主背景 |
| black-100 | `#0000000d`（约 5%） | 透明悬停、登录输入框底（实测 `rgba(0,0,0,0.05)`） |
| black-200 | `#0000001a`（约 10%） | 半透明描边 |
| black-400 | `#0000004d`（约 30%） | 第三文案、禁用图标 |
| black-500 | `#00000080`（50%） | 次级文案（登录页实测 `--color-text-secondary`） |
| black-800 | `#000000e5`（约 90%） | 主文案（登录页实测 `--color-text` 与标题颜色） |

### 语义色（浅色主题）

| 代币 | 解析后的值 | 来源 |
| --- | --- | --- |
| `--color-bg` | `#ffffff` | auth.css `:root`；登录页实测 |
| `--color-bg-secondary` | `#f5f5f5` | 同上 |
| `--color-bg-tertiary` | `#e6e6e6` | auth.css |
| `--color-bg-hover` | `#f5f5f5` | auth.css |
| `--color-bg-pressed` | `#e6e6e6` | auth.css |
| `--color-bg-selected`（默认浅色） | `#e5f4ff`（蓝 200） | 登录页实测。这是设计编辑器的选中色 |
| `--color-bg-selected`（文件浏览器主题） | `#e6e6e6`（灰 200） | auth.css `bake-filebrowser`。状态页采用这一条 |
| `--color-text` | `#000000e5` | 登录页实测 |
| `--color-text-secondary` | `#00000080` | 登录页实测 |
| `--color-text-tertiary` / disabled | `#0000004d` | auth.css |
| `--color-border` | `#e6e6e6` | 登录页实测 |
| `--color-border-strong` | `#2c2c2c` | auth.css |
| `--color-border-selected`（默认） | `#0d99ff` | auth.css 浅色主题 |
| `--color-border-selected`（文件浏览器） | `#2c2c2c` | auth.css `bake-filebrowser` |
| `--color-icon` | `#000000e5` | auth.css |

### 功能色（色阶，auth.css `:root`）

| 角色 | 100 / 200（浅底） | 500（主色） | 600（文字/悬停） |
| --- | --- | --- | --- |
| 蓝（链接、默认品牌） | `#f2f9ff` / `#e5f4ff` | `#0d99ff` | `#007be5`（按下/链接偏 `#0768cf`，登录页链接实测） |
| 绿（成功、在线） | `#edf9ef` / `#e0f6e5` | `#14ae5c` | `#009951` |
| 红（危险、失败） | `#fff5f5` / `#ffe2e0` | `#f24822` | `#dc3412` |
| 黄（警告、等待） | `#fffbeb` / `#fff1c2` | `#ffcd29` | `#ffc21a`；最深文字可用 `#b86200` |

徽章用浅底（200）加深色文字（600/700），不要大块高饱和底。这是从代币用法推断的，登录页没有徽章可量。

文件浏览器主题里，选中图标/文字会偏向紫色（`--ramp-violet-500: #4d49fc`，600 为 `#443deb`）。状态页**不用紫色做品牌色**，以免看起来像在仿一个具体产品；选中行只用中性灰。

## 3. 字体

auth.css `:root`：

- `--font-family-default`：`"Inter", ui-sans-serif, system-ui, -apple-system, "BlinkMacSystemFont", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "Noto Sans", sans-serif`
- `--font-family-mono`：`"Roboto Mono", "Monaco", "Courier New", monospace`
- `--font-weight-default`：`450`
- `--font-weight-strong`：`550`

登录页标题和按钮实际用的是专有字体「Figma Sans」（H1 32px / 字重 500；主按钮 18px / 700）。**状态页禁用专有字体**，改用 Inter，中文后备为 `Noto Sans SC`、`PingFang SC`、`Microsoft YaHei`（auth.css 的默认栈里已有 Noto Sans）。介绍站还声明了「Figma Sans」「Figma Mono」，同样不用。

### 字号阶梯（auth.css `:root` 的 `--text-*`）

按 16px 根字号换算：

| 名称 | 字号 | 行高 | 字重 | 字距 |
| --- | --- | --- | --- | --- |
| body-small | 9px（`.5625rem`） | 14px | 450；加粗 500 | 很紧 |
| body-medium | 11px（`.6875rem`） | 16px | 450 / 550 | `.00344rem` |
| body-large | 13px（`.8125rem`） | 22px | 450 / 550 | `-.002rem` |
| heading-small | 13px | 22px | 550 | `-.002rem` |
| heading-medium | 15px（`.9375rem`） | 25px | 550 | `-.00469rem` |
| heading-large | 24px（`1.5rem`） | 32px | 550 | `-.0255rem` |
| mono-medium | 11px | 16px | 400 | 同 body-medium |
| mono-large | 12px（`.75rem`） | 22px | 400 | — |

产品界面默认密度是 **body-medium = 11px**（auth.css 里这个代币出现最多）。登录表单更大（标题 32px、输入 18px），那是账号页，不拿来做状态页。

中文在 11px 上偏吃力。状态页正文采用 **body-large / heading-small（13px）**，表头、时间、消息编号用 11px 的 body-medium / mono-medium。这仍是同一套阶梯，只是选了大一号的正文，并在本文标明。

## 4. 间距、圆角、描边、阴影

来自 auth.css `:root`：

| 代币 | 值 |
| --- | --- |
| spacer-1 / 4px | 4px（`.25rem`） |
| spacer-2 / 8px | 8px（`.5rem`） |
| spacer-2-5 / 12px | 12px（`.75rem`） |
| spacer-3 / 16px | 16px（`1rem`） |
| spacer-4 / 24px | 24px（`1.5rem`） |
| spacer-5 / 32px | 32px（`2rem`） |
| spacer-6 / 40px | 40px（`2.5rem`） |
| radius-small | 2px（`.125rem`） |
| radius-medium | 5px（`.3125rem`，登录页变量实测一致） |
| radius-large | 13px（`.8125rem`） |
| radius-full | 9999px（胶囊） |

auth.css 里 `padding-left` / `gap` 用得最多的是 `var(--spacer-2)`（8px）和 `var(--spacer-1)`（4px）。

描边：默认 `1px solid var(--color-border)`（`#e6e6e6`），这是样式表里最常见的边框写法。选中/焦点在默认主题是 1–2px 的品牌蓝，文件浏览器主题则是深灰。

阴影（`--elevation-*`，auth.css `:root`）非常轻，用来做菜单和对话框，不做整页卡片墙：

- 100：`0 1px 3px #00000026, 0 0 0.5px #0000004d`
- 300：用于弹出层
- 登录页的按钮和输入框 **实测 `box-shadow: none`**

过渡：`--duration-sm: .1s`，`--duration-md: .2s`，常用曲线 `cubic-bezier(.645,.045,.355,1)` 或 `ease-out`。

## 5. 控件

以下分两类：**登录页实测**（账号表单，偏大）和 **auth.css 里的产品控件代币**（更紧凑，更接近工具界面）。状态页按后者。

### 按钮（产品代币 + 登录页对照）

- 高度代币 `--btn-height`：24px（`1.5rem`，默认/md）、32px（`2rem`，lg）、16px（`1rem`，小号）。来源：auth.css。
- 内边距：`0 var(--spacer-2)`（左右 8px）或 lg 用 `spacer-2-5`（12px）。
- 登录页按钮是另一档：宽 358px、高 **48px**、圆角 **8px**、字重 700。次按钮透明底 + `1px solid #2c2c2c`；主按钮底 `#2c2c2c`、白字。来源：登录页实测。
- 焦点：`inset` 或外描边使用 `--color-border-selected`。

状态页几乎没有表单按钮。如需补，用 32px 高、5px 圆角、主按钮 `#2c2c2c` 白字、次按钮白底深描边。

### 输入框（产品代币 + 登录页对照）

- UI3 紧凑输入：高度 `var(--spacer-4)` = **24px**，背景 `--color-bg-secondary`，圆角 `--radius-medium`（5px），焦点加 `1px solid var(--color-border-selected)`。来源：auth.css 里 `.basic_form--textInput` 及 `[data-fpl-ui3-override]`。
- 较大输入：高度 42px 或 `spacer-5`（32px）。
- 登录页输入：高 **53px**，背景 `rgba(0,0,0,0.05)`，圆角 **6px**，无边框，内边距 `24px 12px 6px`，字号 18px、字重 320。标签是 11px 等宽大写。来源：登录页实测。状态页不采用这档大表单。

### 图标

- 样式表里宽高出现最多的是 **16px**、其次 24px、32px。来源：auth.css 声明计数。
- `--icon-button-size` 常见 24px（`1.5rem`）或 32px（`2rem`）；`--icon-button-icon-size` 16px 或 24px。圆角默认 `radius-medium`。来源：auth.css。

### 徽章

样式表有 `--color-badge-*`，但没有单独量到的尺寸。推测：11px 字、左右 8px 内边距、圆角 `radius-full` 或 5px，浅色底 + 深色字。

## 6. 左侧栏 + 列表布局

**未登录，没有直接看到「最近查看」的侧栏和文件行。** 下面是依据代币和常见工具栏布局的用法，一律标为推测；数字能对应到代币的，会写出来。

推测的页面骨架：

- 左侧固定栏，宽约 **240px**（15rem 一带，未能从真页面量到）。
- 侧栏底用 `--color-bg-secondary`（`#f5f5f5`），主区底 `--color-bg`（`#ffffff`），中间 `1px solid #e6e6e6`。`--z-index-sidebar: 6` 出现在 auth.css，说明产品里确实有侧栏层。
- 导航行高 32px（`spacer-5`），左右内边距 8–12px，行间距 2–4px。默认透明；悬停 `#f5f5f5` / 在灰色侧栏上可用更浅或 `#e6e6e6`；选中用文件浏览器的 `--color-bg-selected`（`#e6e6e6`），圆角 5px，字重 550。
- 主区顶部一条薄工具栏，下面是紧凑列表，而不是大卡片网格（状态页的信息更像列表/表格）。
- 表头：11px、次级颜色、字重 550。
- 数据行：最小高度 32px，上下 8px、左右 12px，行与行之间只有 1px `#e6e6e6` 分隔，不要每张表再套一层厚卡片阴影。
- 悬停：`--color-bg-hover`（`#f5f5f5`）。选中：灰色 `#e6e6e6`，不要默认主题那种浅蓝，避免和「文件列表」气质不一致。
- 主区不要大阴影；弹出菜单才用 elevation-100/300。

## 7. 状态页怎么用这些规则

- 产品名只写「小灵通 / 通讯员」，用一个中文字做标记，不复制任何外部商标。
- 字体：Inter + 系统黑体后备；不用专有英文字体。
- 浅色中性色、左边栏、紧凑行、细线分隔。
- 原有信息全部保留：统计数字、同事、待发箱、收信窗口、收到的信、被拒记录、睡醒说明、端口和启动时间。信封摘要照旧显示，正文不出现。
- 状态徽章：在线/已处理用绿 200+绿 600；等待用黄 200+深黄；失败/连不上用红 200+红 600；离线用灰。
- 证书替换警告用黄 200 底，全文提示保留。

## 8. 若要补上「最近查看」真页面

在已经登录该网站的浏览器里（例如 Nes 的 Mac），用独立用户目录启动带调试端口的 Chrome，打开「最近查看」，再执行：

```bash
npx rea-agents list-browser-targets http://127.0.0.1:9222 --json
npx rea-agents inspect-web-page http://127.0.0.1:9222 <目标ID> --include-accessibility-text --json
npx rea-agents capture-web-screenshot http://127.0.0.1:9222 <目标ID> --json
```

特别值得量的是：侧栏实际宽度、导航行高、文件行高、列表/网格切换、顶栏高度、图标大小。不要把登录 cookie 或调试端口暴露到非本机地址。
