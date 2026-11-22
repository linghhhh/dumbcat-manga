"""HTML 选择器查询（cheerio / CSS 的一个够用子集）。

为什么要它：Breeze 的漫源插件用 cheerio 选择器写解析（一个插件里几十处
`$(".book-list .item a[href*='/book/']")`）。要把这些源移植成 Python provider，
如果不提供选择器查询，就得把每条选择器手工改写成 `find_all(tag, class)` 链，
既慢又容易改错。这里实现一个受控的 CSS 子集，让移植可以逐字照抄选择器。

支持（够覆盖实际插件用法）：

- 类型 / 类 / id：`div`、`.item`、`#main`
- 属性：`[href]`、`[href="/x"]`、`[href*="book"]`、`[href^="http"]`、`[href$=".html"]`
- 组合：`div.item#id[data-x="1"]`
- 层级：后代（空格）、直接子元素（`>`）
- 逗号并列：`a, b`
- 位置伪类：`:first`、`:last`、`:eq(n)`、`:nth-child(n)`（1 基，和 CSS 一致）

刻意不做：`+`/`~` 兄弟组合、`*` 通配、`:not()`、`:has()`。真需要时再按需补，
保持实现小到能被完全理解——解析器的可靠性直接决定各漫源能不能抓到数据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from dumbcat.html_parser import HtmlNode

#: 一段简单选择器（不含组合子）
_SIMPLE_RE = re.compile(
    r"""
    (?P<tag>[a-zA-Z][\w-]*)              # 标签名
    | \#(?P<id>[\w-]+)                   # #id
    | \.(?P<cls>[\w-]+)                  # .class
    | \[(?P<attr>[^\]]+)\]               # [attr...]
    | :(?P<pseudo>[\w-]+)(?:\((?P<arg>[^)]*)\))?   # :first / :eq(0)
    """,
    re.VERBOSE,
)

_ATTR_RE = re.compile(
    r"""^\s*(?P<name>[\w:-]+)\s*
        (?:(?P<op>[~^$*|]?=)\s*(?P<value>.*?)\s*)?$""",
    re.VERBOSE,
)


@dataclass
class _Simple:
    tag: str = ""
    node_id: str = ""
    classes: tuple[str, ...] = ()
    attrs: tuple[tuple[str, str, str], ...] = ()  # (name, op, value)
    pseudo: str = ""
    pseudo_arg: str = ""


def _parse_simple(text: str) -> _Simple:
    simple = _Simple()
    classes: list[str] = []
    attrs: list[tuple[str, str, str]] = []
    position = 0
    for match in _SIMPLE_RE.finditer(text):
        if match.start() != position:
            # 有未识别片段：当作"匹配不到"，避免静默返回错误结果
            return _Simple(tag="\0")
        position = match.end()
        if match.group("tag"):
            simple.tag = match.group("tag").lower()
        elif match.group("id"):
            simple.node_id = match.group("id")
        elif match.group("cls"):
            classes.append(match.group("cls"))
        elif match.group("attr") is not None:
            parsed = _ATTR_RE.match(match.group("attr") or "")
            if not parsed:
                return _Simple(tag="\0")
            value = parsed.group("value")
            if value is not None:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
            attrs.append(
                (parsed.group("name").lower(), parsed.group("op") or "", value or "")
            )
        elif match.group("pseudo"):
            simple.pseudo = match.group("pseudo").lower()
            simple.pseudo_arg = (match.group("arg") or "").strip()
    if position != len(text):
        return _Simple(tag="\0")
    simple.classes = tuple(classes)
    simple.attrs = tuple(attrs)
    return simple


def _attr_matches(node: HtmlNode, name: str, op: str, value: str) -> bool:
    actual = node.attr(name)
    if not op:
        return bool(actual) or name in node.attrs
    if op == "=":
        return actual == value
    if op == "*=":
        return value in actual
    if op == "^=":
        return actual.startswith(value)
    if op == "$=":
        return actual.endswith(value)
    if op == "~=":
        return value in actual.split()
    if op == "|=":
        return actual == value or actual.startswith(f"{value}-")
    return False


def matches_selector(node: HtmlNode, selector: str) -> bool:
    """单个节点是否匹配选择器（用于 `closest()` / `is()`）。

    只支持最后一个简单选择器（不含组合子）；需要组合子时请用 `query_all`。
    """
    if node is None:
        return False
    branches = _parse_selector(selector)
    if not branches:
        return False
    for steps in branches:
        if not steps:
            continue
        simple = steps[-1][1]
        if _matches(node, simple) and not simple.pseudo:
            return True
    return False


def _matches(node: HtmlNode, simple: _Simple) -> bool:
    if simple.tag == "\0":
        return False  # 解析失败：绝不"差不多匹配"
    if simple.tag and node.tag != simple.tag:
        return False
    if simple.node_id and node.attr("id") != simple.node_id:
        return False
    if simple.classes and not all(node.has_class(name) for name in simple.classes):
        return False
    for name, op, value in simple.attrs:
        if not _attr_matches(node, name, op, value):
            return False
    return True


def _apply_pseudo(nodes: list[HtmlNode], pseudo: str, arg: str) -> list[HtmlNode]:
    if not pseudo:
        return nodes
    if pseudo == "first":
        return nodes[:1]
    if pseudo == "last":
        return nodes[-1:]
    if pseudo == "eq":
        try:
            index = int(arg)
        except ValueError:
            return []
        if index < 0:
            index += len(nodes)
        return nodes[index : index + 1] if 0 <= index < len(nodes) else []
    if pseudo == "nth-child":
        try:
            wanted = int(arg)
        except ValueError:
            return []
        return [
            node
            for node in nodes
            if _child_index(node) == wanted
        ]
    if pseudo in {"contains", "has", "not", "visible"}:
        # 明确不支持：返回空而不是悄悄忽略，免得出错数据
        return []
    return []


def _child_index(node: HtmlNode) -> int:
    """该节点在父节点里的位置（1 基）。节点没有父指针，靠外部遍历时缓存。"""
    index = getattr(node, "_child_index", None)
    return int(index) if isinstance(index, int) else -1


def _index_children(root: HtmlNode) -> None:
    """给每个节点标上"在同标签兄弟里的序号"，供 :nth-child 使用。"""
    stack = [root]
    while stack:
        node = stack.pop()
        counters: dict[str, int] = {}
        for child in node.children:
            if not isinstance(child, HtmlNode):
                continue
            counters[child.tag] = counters.get(child.tag, 0) + 1
            try:
                child._child_index = counters[child.tag]  # type: ignore[attr-defined]
            except AttributeError:
                pass
            stack.append(child)


def _parse_selector(selector: str) -> list[list[tuple[str, _Simple]]]:
    """把选择器解析成"每个逗号分支 = [(组合子, 简单选择器), ...]"。"""
    branches: list[list[tuple[str, _Simple]]] = []
    for chunk in str(selector or "").split(","):
        text = chunk.strip()
        if not text:
            continue
        # 先把 > 前后补空格，统一按空白切分
        text = re.sub(r"\s*>\s*", " > ", text)
        parts = [part for part in text.split() if part]
        steps: list[tuple[str, _Simple]] = []
        combinator = " "
        for part in parts:
            if part == ">":
                combinator = ">"
                continue
            steps.append((combinator, _parse_simple(part)))
            combinator = " "
        if steps:
            branches.append(steps)
    return branches


def _descendants(node: HtmlNode) -> Iterator[HtmlNode]:
    return node.iter_descendants()


def query_all(root: HtmlNode, selector: str) -> list[HtmlNode]:
    """按 CSS 子集选择器取出所有匹配节点（文档顺序，已去重）。"""
    if root is None:
        return []
    _index_children(root)
    branches = _parse_selector(selector)
    if not branches:
        return []
    results: list[HtmlNode] = []
    seen: set[int] = set()
    for steps in branches:
        current: list[HtmlNode] = []
        head_combinator, head = steps[0]
        # 第一个简单选择器：从整棵子树里挑（不要求是直接子元素）
        for node in _descendants(root):
            if _matches(node, head):
                current.append(node)
        current = _apply_pseudo(current, head.pseudo, head.pseudo_arg)
        for combinator, simple in steps[1:]:
            candidates: list[HtmlNode] = []
            if combinator == ">":
                for node in current:
                    for child in node.children:
                        if isinstance(child, HtmlNode) and _matches(child, simple):
                            candidates.append(child)
            else:
                for node in current:
                    for child in _descendants(node):
                        if _matches(child, simple):
                            candidates.append(child)
            # 同一层里按文档顺序去重
            ordered: list[HtmlNode] = []
            local_seen: set[int] = set()
            for node in candidates:
                marker = id(node)
                if marker in local_seen:
                    continue
                local_seen.add(marker)
                ordered.append(node)
            current = _apply_pseudo(ordered, simple.pseudo, simple.pseudo_arg)
        for node in current:
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            results.append(node)
    return results


def query_one(root: HtmlNode, selector: str) -> HtmlNode | None:
    """取第一个匹配节点（等价 `$(sel).first()`）。"""
    found = query_all(root, selector)
    return found[0] if found else None


def text_of(root: HtmlNode, selector: str, default: str = "") -> str:
    node = query_one(root, selector)
    return node.text_content() if node is not None else default


def attr_of(root: HtmlNode, selector: str, name: str, default: str = "") -> str:
    node = query_one(root, selector)
    return node.attr(name) if node is not None else default


def attrs_of(root: HtmlNode, selector: str, name: str) -> list[str]:
    return [node.attr(name) for node in query_all(root, selector)]


def texts_of(root: HtmlNode, selector: str) -> list[str]:
    return [node.text_content() for node in query_all(root, selector)]


def iter_matches(root: HtmlNode, selector: str) -> Iterable[HtmlNode]:
    return query_all(root, selector)
