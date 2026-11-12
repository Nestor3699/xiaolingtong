#!/usr/bin/env python3
"""把状态页用到的 Lucide 图标取下来，写成 xlt/icons.py（内联 SVG 片段）。

Lucide 是 ISC 许可证，部分图标源自 Feather（MIT）；许可证全文在仓库根目录 THIRD_PARTY_NOTICES.md。
为了结果可复现，固定在一个 Lucide 提交上。状态页运行时不联网，所以图标必须是随包带的。

用法：python3 scripts/vendor_icons.py        （需要联网，只在更新图标时跑一次）
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

COMMIT = "03eae6069edfc373673bd05d723491f05ba936e3"  # lucide-icons/lucide，2026-11-10
BASE = f"https://raw.githubusercontent.com/lucide-icons/lucide/{COMMIT}"
ICONS = [
    "users", "clock", "send", "app-window", "inbox", "shield-ban",
    "laptop", "network", "fingerprint-pattern", "activity", "plug", "info",
    "copy", "check", "check-check", "triangle-exclamation-point", "paperclip", "refresh-cw", "x", "chevron-right",
]
ROOT = Path(__file__).resolve().parent.parent


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def inner(svg: str) -> str:
    body = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
    return " ".join(line.strip() for line in body.strip().splitlines())


def main() -> int:
    rows = []
    for name in ICONS:
        rows.append(f'    "{name}": {inner(fetch(f"{BASE}/icons/{name}.svg"))!r},')
    license_text = fetch(f"{BASE}/LICENSE")
    (ROOT / "xlt" / "share" / "LICENSE-lucide.txt").write_text(license_text, encoding="utf-8")
    out = f'''"""状态页用的图标：Lucide（ISC 许可证，部分源自 Feather，MIT）。由 scripts/vendor_icons.py 生成，不要手改。

来源 https://github.com/lucide-icons/lucide 提交 {COMMIT}。许可证全文见仓库根目录 THIRD_PARTY_NOTICES.md
和 xlt/share/LICENSE-lucide.txt。图标直接内联进页面，不走网络，断网也能显示。
"""

from __future__ import annotations

LUCIDE_COMMIT = "{COMMIT}"

PATHS = {{
{chr(10).join(rows)}
}}


def icon(name: str, size: int = 16, extra_style: str = "") -> str:
    """返回一段内联 <svg>。颜色跟随文字颜色（currentColor）。

    大小、display、对齐都直接写在元素上（不只靠样式表）：浏览器里开着的旧页面只会换新内容、
    不会换样式表，样式表缺了这些规则时，图标也不会撑成默认大小或掉到基线下面。"""
    inner = PATHS[name]
    return (
        f'<svg class="ic" width="{{size}}" height="{{size}}" viewBox="0 0 24 24" fill="none" '
        f'style="display:inline-block;flex:0 0 auto;width:{{size}}px;height:{{size}}px;vertical-align:-0.15em;{{extra_style}}" '
        f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{{inner}}</svg>'
    )
'''
    (ROOT / "xlt" / "icons.py").write_text(out, encoding="utf-8")
    notice = f"""# 第三方许可证声明

## Lucide 图标（状态页）

状态页里的图标来自 [Lucide](https://lucide.dev)（<https://github.com/lucide-icons/lucide>，提交 `{COMMIT}`），
以内联 SVG 的形式放在 `xlt/icons.py` 里，由 `scripts/vendor_icons.py` 生成。Lucide 采用 ISC 许可证；
其中部分图标源自 Feather 项目，采用 MIT 许可证。下面是 Lucide 仓库 LICENSE 文件的原文，按许可证要求随代码一起分发。

```text
{license_text.rstrip()}
```
"""
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(notice, encoding="utf-8")
    print(f"写入 {len(ICONS)} 个图标、xlt/icons.py、THIRD_PARTY_NOTICES.md、xlt/share/LICENSE-lucide.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
