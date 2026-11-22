"""轻量 HTML 解析器。

刻意只依赖标准库：项目要打包分发（runtime_cleanup 里已有 PyInstaller 的
_MEI 分支），多一个第三方解析依赖会增加打包体积与部署失败面。
从旧的主模块拆出；manga_core 会重新导出这些名字。
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterator


class HtmlNode:
    def __init__(
        self,
        tag: str,
        attrs: dict[str, str] | None = None,
        parent: "HtmlNode | None" = None,
    ) -> None:
        self.tag = tag.lower()
        self.attrs = dict(attrs or {})
        self.children: list[HtmlNode | str] = []
        #: 父节点：cheerio 的 .parent()/.closest() 在移植时很常用，而很多站点的
        #: 标题/封面/张数是**兄弟节点**（绅士漫画的 .pic_box 与 .info），
        #: 没有父指针就只能靠猜层级。
        self.parent = parent

    def attr(self, name: str) -> str:
        return self.attrs.get(name.lower(), "")

    def has_class(self, name: str) -> bool:
        return name in self.attr("class").split()

    def closest(self, selector: str) -> "HtmlNode | None":
        """从自身往上找第一个匹配的祖先（含自身）。"""
        from dumbcat.html_query import matches_selector

        node: HtmlNode | None = self
        while node is not None:
            if matches_selector(node, selector):
                return node
            node = node.parent
        return None

    def text_content(self) -> str:
        parts: list[str] = []

        def collect(node: HtmlNode) -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    collect(child)

        collect(self)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def iter_descendants(self) -> Iterator[HtmlNode]:
        for child in self.children:
            if isinstance(child, HtmlNode):
                yield child
                yield from child.iter_descendants()

    def find_all(
        self,
        tag: str | None = None,
        class_name: str | None = None,
    ) -> list[HtmlNode]:
        return [
            node
            for node in self.iter_descendants()
            if (tag is None or node.tag == tag.lower())
            and (class_name is None or node.has_class(class_name))
        ]


class HtmlTreeParser(HTMLParser):
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self._stack = [self.root]

    @classmethod
    def parse(cls, source: str) -> HtmlNode:
        parser = cls()
        parser.feed(source)
        parser.close()
        return parser.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {
            name.lower(): value or ""
            for name, value in attrs
        }
        node = HtmlNode(tag, normalized, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = {
            name.lower(): value or ""
            for name, value in attrs
        }
        self._stack[-1].children.append(
            HtmlNode(tag, normalized, self._stack[-1])
        )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)
