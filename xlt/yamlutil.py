"""只够读写方案里那几份 YAML 的小解析器。不引入 PyYAML。"""

from __future__ import annotations


class YamlError(ValueError):
    pass


def loads(text: str):
    lines = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        raw = raw.replace("\t", "    ")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((lineno, raw))
    if not lines:
        return None
    value, idx = _parse(lines, 0, 0)
    if idx < len(lines):
        lineno, raw = lines[idx]
        raise YamlError(f"第 {lineno} 行附近没解析完：{raw.strip()}")
    return value


def dumps(obj) -> str:
    return "\n".join(_emit(obj, 0)) + "\n"


def _indent(raw: str) -> int:
    return len(raw) - len(raw.lstrip(" "))


def _parse(lines, idx, min_indent):
    if idx >= len(lines):
        return None, idx
    _, raw = lines[idx]
    if _indent(raw) < min_indent:
        return None, idx
    if raw.lstrip().startswith("- "):
        return _parse_list(lines, idx, _indent(raw))
    return _parse_dict(lines, idx, _indent(raw))


def _parse_dict(lines, idx, ind):
    obj = {}
    while idx < len(lines):
        lineno, raw = lines[idx]
        cur = _indent(raw)
        if cur < ind:
            break
        if cur > ind:
            raise YamlError(f"第 {lineno} 行缩进不对：{raw.strip()}")
        stripped = raw.strip()
        if stripped.startswith("- "):
            break
        if ":" not in stripped:
            raise YamlError(f"第 {lineno} 行不是「键: 值」：{stripped}")
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest in ("|", "|-", ">", ">-"):
            idx += 1
            block, idx = _block(lines, idx, ind)
            obj[key] = block
            continue
        if rest == "":
            idx += 1
            if idx >= len(lines) or _indent(lines[idx][1]) <= ind:
                obj[key] = None
                continue
            obj[key], idx = _parse(lines, idx, ind + 1)
            continue
        obj[key] = scalar(rest)
        idx += 1
    return obj, idx


def _parse_list(lines, idx, ind):
    arr = []
    while idx < len(lines):
        lineno, raw = lines[idx]
        if _indent(raw) < ind:
            break
        if _indent(raw) != ind or not raw.lstrip().startswith("- "):
            break
        item = raw.lstrip()[2:]
        if item == "":
            idx += 1
            val, idx = _parse(lines, idx, ind + 1)
            arr.append(val)
            continue
        if _looks_like_key(item):
            content_indent = ind + 2
            sub = [(lineno, " " * content_indent + item)]
            idx += 1
            while idx < len(lines) and _indent(lines[idx][1]) >= content_indent:
                sub.append(lines[idx])
                idx += 1
            val, end = _parse_dict(sub, 0, content_indent)
            if end < len(sub):
                raise YamlError(f"第 {sub[end][0]} 行列表项没解析完")
            arr.append(val)
            continue
        arr.append(scalar(item))
        idx += 1
    return arr, idx


def _looks_like_key(item: str) -> bool:
    if item.startswith("{") or item.startswith("[") or item.startswith("'") or item.startswith('"'):
        return False
    key, sep, _rest = item.partition(":")
    return bool(sep) and bool(key.strip()) and " " not in key.strip()


def _block(lines, idx, parent_indent):
    rows = []
    base = None
    while idx < len(lines):
        _, raw = lines[idx]
        if raw.strip() and _indent(raw) <= parent_indent:
            break
        if base is None:
            base = _indent(raw) if raw.strip() else parent_indent + 2
        rows.append(raw[base:] if len(raw) >= base else raw.lstrip(" "))
        idx += 1
    text = "\n".join(rows)
    if text.endswith("\n"):
        text = text[:-1]
    return text, idx


def scalar(text: str):
    s = text.strip()
    if s in ("", "null", "~", "Null", "NULL"):
        return None
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    if s.startswith("[") and s.endswith("]"):
        return _flow_list(s)
    if s.startswith("{") and s.endswith("}"):
        return _flow_map(s)
    if _is_int(s):
        return int(s)
    return s


def _is_int(s: str) -> bool:
    if s.startswith("-"):
        s = s[1:]
    return bool(s) and s.isdigit()


def _split_flow(inner: str) -> list[str]:
    parts = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf and "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _flow_list(s: str):
    inner = s[1:-1].strip()
    if not inner:
        return []
    return [scalar(part) for part in _split_flow(inner)]


def _flow_map(s: str):
    inner = s[1:-1].strip()
    obj = {}
    if not inner:
        return obj
    for part in _split_flow(inner):
        key, sep, rest = part.partition(":")
        if not sep:
            raise YamlError(f"花括号里缺少冒号：{part}")
        obj[key.strip()] = scalar(rest.strip())
    return obj


def _fmt(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    if (
        text == ""
        or text != text.strip()
        or any(ch in text for ch in ":#{}[],&*!|>%@`\"'")
        or text.lower() in ("null", "true", "false", "yes", "no")
    ):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _emit(obj, ind: int) -> list[str]:
    pad = "  " * ind
    if isinstance(obj, dict):
        lines = []
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                if value == {} or value == []:
                    lines.append(f"{pad}{key}: {'{}' if isinstance(value, dict) else '[]'}")
                else:
                    lines.append(f"{pad}{key}:")
                    lines.extend(_emit(value, ind + 1))
            else:
                lines.append(f"{pad}{key}: {_fmt(value)}")
        return lines
    if isinstance(obj, list):
        lines = []
        for item in obj:
            if isinstance(item, dict) and item:
                pairs = list(item.items())
                first_key, first_val = pairs[0]
                if isinstance(first_val, (dict, list)) and first_val not in ({}, []):
                    lines.append(f"{pad}- {first_key}:")
                    lines.extend(_emit(first_val, ind + 2))
                else:
                    shown = "{}" if first_val == {} else "[]" if first_val == [] else _fmt(first_val)
                    lines.append(f"{pad}- {first_key}: {shown}")
                for key, value in pairs[1:]:
                    if isinstance(value, (dict, list)) and value not in ({}, []):
                        lines.append(f"{pad}  {key}:")
                        lines.extend(_emit(value, ind + 2))
                    else:
                        shown = "{}" if value == {} else "[]" if value == [] else _fmt(value)
                        lines.append(f"{pad}  {key}: {shown}")
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.extend(_emit(item, ind + 1))
            else:
                lines.append(f"{pad}- {_fmt(item)}")
        return lines
    return [pad + _fmt(obj)]
