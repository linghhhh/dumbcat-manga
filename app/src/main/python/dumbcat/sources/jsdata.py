"""从页面里的 JS 数据块里取值的工具。

很多站点（绅士漫画、漫画柜、快看…）把真实数据塞在页面内联脚本里，形如
`mReader.initData({"page_url":[...],"page_size":[...]})`。这些字面量不是严格
JSON：常见 JS 尾逗号、单引号、`undefined`。用 `json.loads` 直接读会失败，
用正则截数组又会被 URL 里的 `]` 提前截断（绅士漫画的图片名里就有方括号）。

这里提供两个能力：

- `extract_js_value(text, key)`：先找到键，再用括号配平扫描出完整的值，
  最后用宽松解析转成 Python 对象；
- `loads_lenient(text)`：容忍尾逗号 / 单引号 / `undefined` 的 JSON 解析。
"""
from __future__ import annotations

import json
import re
from typing import Any

_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")
_UNQUOTED_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:")


def loads_lenient(text: str) -> Any:
    """宽松解析 JS 字面量：去尾逗号 → 单引号转双引号 → undefined/null → JSON。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", raw)
    candidates.append(cleaned)
    # 单引号字符串 → 双引号（只处理没有双引号的简单情况，避免破坏内容）
    if "'" in cleaned and '"' not in cleaned:
        candidates.append(cleaned.replace("'", '"'))
    candidates.append(cleaned.replace("undefined", "null"))
    candidates.append(_UNQUOTED_KEY_RE.sub(r'\1"\2":', cleaned))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _find_value_start(text: str, key: str) -> int:
    """定位 `"key"` / `'key'` / `key` 之后第一个值起始字符。"""
    pattern = re.compile(
        r"""["']?%s["']?\s*:\s*""" % re.escape(str(key)),
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.end() if match else -1


def extract_js_value(text: str, key: str) -> Any:
    """按键取出完整值（数组 / 对象 / 标量），括号配平扫描，不受内部括号影响。"""
    source = str(text or "")
    start = _find_value_start(source, key)
    if start < 0 or start >= len(source):
        return None
    opener = source[start]
    if opener in "[{":
        closer = "]" if opener == "[" else "}"
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        for index in range(start, len(source)):
            char = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    in_string = False
                continue
            if char in "\"'":
                in_string = True
                quote = char
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return loads_lenient(source[start : index + 1])
        return None
    # 标量：读到分隔符为止
    end = start
    while end < len(source) and source[end] not in ",;\n\r}":
        end += 1
    return loads_lenient(source[start:end])


def extract_js_array(text: str, key: str) -> list[Any]:
    value = extract_js_value(text, key)
    return value if isinstance(value, list) else []


def extract_js_object(text: str, key: str) -> dict[str, Any]:
    value = extract_js_value(text, key)
    return value if isinstance(value, dict) else {}


def extract_number(text: str, key: str, default: int = 0) -> int:
    value = extract_js_value(text, key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group(0))
    return default


def upgrade_to_https(url: str) -> str:
    """把 http:// 换成 https://。

    不少图床（绅士漫画的 qy0.ru）对纯 HTTP 直接 RST，只有 HTTPS 才给图；
    封面还常写成协议相对地址 `//t4.qy0.ru/...`，需要补上 https:。
    """
    text = str(url or "").strip()
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("http://"):
        return "https://" + text[len("http://") :]
    return text
