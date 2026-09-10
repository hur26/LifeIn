"""控制台的一套外观:壳、组件、图标,以及一份够用的 Markdown。

**这个模块存在的理由是"改一处"。** 控制台从三个页面长到两层十几个页面
([ADR-029](../../docs/04-tech-decisions.md)),而原来内联在 `console.py` 里的
那十行 CSS 是按"三个页面"的量写的 —— 再抄一遍就是十几份各自跑偏的样式。
跑偏的第一个症状是同一个"停用"按钮在两个页面上颜色不一样,
于是人开始怀疑它们不是同一个动作。

## 仍然没有前端框架

[ADR-016](../../docs/04-tech-decisions.md#adr-016--服务端用-python--fastapi)
定的是"能被 systemd 拉起来的单进程"。上一个前端框架意味着一套构建、
一份依赖清单、一次 `npm audit`,而这些页面加起来的交互只有三样:
点链接、提交表单、展开一段说明 —— **后两样 HTML 自己就有**
(`<form>` 和 `<details>`)。

所以这里一个字节的 JavaScript 都没有。代价是没有即时校验和局部刷新;
换来的是**这套界面在关掉脚本的浏览器里、在十年后的浏览器里行为完全一样**。

## 也没有任何外部引用

字体用系统栈,图标是内联 SVG,颜色写在 CSS 变量里。一个外链就多一处
Referer 会带着东西出去的地方 —— 而这些页面上带着的是配码、导出、
和别人的用户名(`tests/test_console.py` 盯着这一条)。

## 每一个组件都自己转义

`esc()` 在这个文件里出现的次数看起来很啰嗦,而这是有意的:
组件的入参既有已经拼好的 HTML(`body`),又有原始文本(`title`),
**分不清哪个是哪个的那一刻就是 XSS 的入口**。约定是:
名字里带 `body` / `inner` / `actions` 的收 HTML,其余一律收文本并当场转义。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from lifein.api.console_style import CSS

# ---------------------------------------------------------------- 导航


@dataclass(frozen=True)
class NavItem:
    """左边那一列里的一条。`key` 用来判断"当前在哪一页"。"""

    key: str
    label: str
    href: str
    icon: str


@dataclass(frozen=True)
class NavGroup:
    title: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Identity:
    """右上角那一块:现在是谁、在哪一层。

    **两层共用一个壳,靠这一块区分。** 分成两个壳的话,"这是用户层还是
    运营层"就变成了两套 HTML 各自的表述,而那两套一定会在某次改动之后
    说得不一样 —— 这个问题答错的后果是有人以为自己在看自己的数据。
    """

    name: str
    layer: str
    layer_kind: str = "user"
    switch_label: str | None = None
    switch_href: str | None = None
    sign_out_href: str | None = None


# ---------------------------------------------------------------- 图标

_ICONS = {
    "gauge": "M4.5 17.5a8.5 8.5 0 1 1 15 0M12 12l3.5-3",
    "phone": "M7.5 3h9a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm3 15h3",
    "antenna": "M12 13.5V21M8.8 9.7a4.5 4.5 0 0 1 6.4 0M6 6.9a8.5 8.5 0 0 1 12 0",
    "box": "M4 8.5 12 4l8 4.5v7L12 20l-8-4.5v-7Zm0 0 8 4.5m0 0 8-4.5m-8 4.5V20",
    "shield": "M12 3.5 5 6.5v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9v-5l-7-3Z",
    "users": "M9.5 11.5a3.3 3.3 0 1 0 0-6.6 3.3 3.3 0 0 0 0 6.6ZM3.5 19.5c0-3 2.7-5 6-5s6 2 6 5"
             "M16.5 11.6a3 3 0 0 0 0-6M20.5 19.5c0-2.4-1-4-2.6-4.9",
    "clock": "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16Zm0 3.8V12l3 2",
    "coin": "M12 4c4.4 0 8 1.6 8 3.5S16.4 11 12 11 4 9.4 4 7.5 7.6 4 12 4Zm8 3.5v9c0 1.9-3.6 3.5"
            "-8 3.5s-8-1.6-8-3.5v-9",
    "key": "M20 4.5 12.8 11.7M17.6 6.9l2 2M9.5 15a3.9 3.9 0 1 1-5.5 5.5A3.9 3.9 0 0 1 9.5 15Z",
    "doc": "M6.5 3h7l4.5 4.5V21h-11.5V3Zm7 0v5h5M9.5 12.5h5M9.5 16.5h5",
    "alert": "M12 4.5 3.8 19h16.4L12 4.5Zm0 5.2v4.3m0 2.8v.1",
    "plus": "M12 5.5v13M5.5 12h13",
    "back": "M14.5 6.5 9 12l5.5 5.5",
    "logout": "M14.5 4.5h-9v15h9M20 12H10.5m9.5 0-3-3m3 3-3 3",
    "spark": "M12 4v3.5M12 16.5V20M4 12h3.5M16.5 12H20M6.7 6.7l2.4 2.4M14.9 14.9l2.4 2.4"
             "M17.3 6.7l-2.4 2.4M9.1 14.9l-2.4 2.4",
}
"""内联图标。**只有轮廓,没有填充** —— 填充的图标在深色下要另做一套,
而轮廓的那套换个 `currentColor` 就跟着走。

画得少是有意的:控制台上的图标是**用来在扫视时区分行的**,不是用来看的。
"""


def icon(name: str, *, size: int = 18) -> str:
    """一个内联 SVG。名字不认识时返回空串 —— **不画一个问号**:
    缺图标是开发期的事,而一个问号会被用户当成"这里出问题了"。
    """
    path = _ICONS.get(name)
    if not path:
        return ""
    return (
        f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true"><path d="{path}"/></svg>'
    )


# ---------------------------------------------------------------- 组件


def esc(value: object) -> str:
    return html.escape(str(value))


def card(title: str, body: str, *, hint_text: str = "", actions: str = "", tone: str = "") -> str:
    """一块内容。`tone="danger"` 给不可撤销的区域用(删除、吊销)。"""
    head = ""
    if title or actions:
        head = (
            f'<header class="card-head"><h2>{esc(title)}</h2>'
            f'<div class="card-actions">{actions}</div></header>'
        )
    foot = f'<p class="hint">{hint_text}</p>' if hint_text else ""
    tone_class = f" card-{tone}" if tone else ""
    return (
        f'<section class="card{tone_class}">{head}'
        f'<div class="card-body">{body}{foot}</div></section>'
    )


def stats(items: list[tuple[str, str, str, str]]) -> str:
    """一行大数字。每项是(标题、数字、下面那句话、语气)。

    **下面那句话不是可选的。** 一个没有解释的数字会被当成 KPI,
    而这里每个数字要回答的是"要不要现在做点什么"。
    """
    cells = "".join(
        f'<div class="stat{" stat-" + tone if tone else ""}">'
        f'<span class="stat-label">{esc(label)}</span>'
        f'<span class="stat-value">{value}</span>'
        f'<span class="stat-hint">{esc(note)}</span></div>'
        for label, value, note, tone in items
    )
    return f'<div class="stats">{cells}</div>'


def table(headers: list[str], rows: list[list[str]], *, empty: str = "没有内容") -> str:
    """一张表。**空表不显示表头** —— 一张只有表头的表看起来像加载失败。"""
    if not rows:
        return empty_state(empty)
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def badge(text: str, *, tone: str = "neutral") -> str:
    """一枚状态。`tone` 取 neutral / ok / warn / danger / accent。"""
    return f'<span class="badge badge-{tone}">{esc(text)}</span>'


def button(label: str, *, tone: str = "primary", icon_name: str = "") -> str:
    glyph = icon(icon_name, size=16) if icon_name else ""
    return f'<button class="btn btn-{tone}" type="submit">{glyph}<span>{esc(label)}</span></button>'


def link_button(label: str, href: str, *, tone: str = "ghost", icon_name: str = "") -> str:
    glyph = icon(icon_name, size=16) if icon_name else ""
    return f'<a class="btn btn-{tone}" href="{esc(href)}">{glyph}<span>{esc(label)}</span></a>'


def form(action: str, inner: str, *, cls: str = "") -> str:
    """**控制台上每一个会改变什么的动作都是 POST。**

    GET 会被浏览器预取、被 Referer 带走、被"重新打开上次的标签页"重放 ——
    而这里的动作包括签发配码、吊销设备、删数据。一个表单按钮和一个链接
    在用户眼里没有区别,在这几件事上差很远。
    """
    return f'<form class="{cls}" method="post" action="{esc(action)}">{inner}</form>'


def hidden(name: str, value: object) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def text_field(name: str, label: str, *, placeholder: str = "", kind: str = "text") -> str:
    return (
        f'<div class="field"><label for="f-{esc(name)}">{esc(label)}</label>'
        f'<input id="f-{esc(name)}" type="{esc(kind)}" name="{esc(name)}" '
        f'placeholder="{esc(placeholder)}" autocomplete="off"></div>'
    )


def banner(body: str, *, tone: str = "info", icon_name: str = "alert") -> str:
    return f'<div class="banner banner-{tone}">{icon(icon_name)}<div>{body}</div></div>'


def empty_state(text: str) -> str:
    return f'<div class="empty">{esc(text)}</div>'


def hint(body: str) -> str:
    return f'<p class="hint">{body}</p>'


def details(summary: str, body: str) -> str:
    """折叠一段。**用 `<details>`,不写 JavaScript** —— 见模块开头。"""
    return f'<details class="fold"><summary>{esc(summary)}</summary><div>{body}</div></details>'


def mono(value: object) -> str:
    """等宽显示一串机器看的东西(uuid、包名、设备号)。"""
    return f'<code class="mono">{esc(value)}</code>'


def when(value: object | None, *, empty: str = "从没有过") -> str:
    """时间。**没有值时显示一句话,不是空白** —— 空白看起来像没渲染出来。"""
    if value is None:
        return f'<span class="faint">{esc(empty)}</span>'
    return f'<time>{esc(str(value)[:16].replace("T", " "))}</time>'


# ---------------------------------------------------------------- 页面壳


def page(
    *,
    title: str,
    heading: str,
    body: str,
    lede: str = "",
    nav: tuple[NavGroup, ...] = (),
    active: str = "",
    identity: Identity | None = None,
    actions: str = "",
) -> str:
    """一整张页面。**两层共用这一个函数。**"""
    head = f"<h1>{esc(heading)}</h1>"
    if lede:
        head += f'<p class="lede">{lede}</p>'
    if actions:
        head += f'<div class="page-actions">{actions}</div>'
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="same-origin">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="shell">{_sidebar(nav, active, identity)}'
        f'<main class="main">{_topbar(identity)}'
        f'<div class="page"><header class="page-head">{head}</header>{body}</div>'
        "</main></div></body></html>"
    )


def bare_page(*, title: str, heading: str, body: str) -> str:
    """没有导航的一张页面:登录、链接过期,以及任何"还没认出你是谁"的时刻。

    **不给导航是有意的。** 一份列着"设备""数据""用户"的侧边栏,
    对着一个还没进门的人,等于把这台机器上有什么先说了一遍。
    """
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="same-origin">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f'<div class="bare"><div class="bare-card">'
        f'<div class="brand brand-lg">{_mark()}<span>LifeIn</span></div>'
        f"<h1>{esc(heading)}</h1>{body}</div></div></body></html>"
    )


def _mark() -> str:
    """品牌那个小方块。**是一个 SVG 不是一张图** —— 见模块开头那条"没有外部引用"。"""
    return (
        '<svg class="mark" width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">'
        '<rect x="2" y="2" width="20" height="20" rx="7" fill="currentColor" opacity=".14"/>'
        '<path d="M8 15.6V7.2m0 8.4h5.4M15.2 7.2v3" stroke="currentColor" stroke-width="1.9" '
        'stroke-linecap="round" fill="none"/>'
        '<circle cx="15.2" cy="14.6" r="1.7" fill="currentColor"/></svg>'
    )


def _sidebar(nav: tuple[NavGroup, ...], active: str, identity: Identity | None) -> str:
    if not nav:
        return ""
    groups = []
    for group in nav:
        links = "".join(
            f'<a class="nav-item{" is-active" if item.key == active else ""}" '
            f'href="{esc(item.href)}">{icon(item.icon)}<span>{esc(item.label)}</span></a>'
            for item in group.items
        )
        groups.append(
            f'<div class="nav-group"><span class="nav-title">{esc(group.title)}</span>{links}</div>'
        )
    layer = ""
    if identity:
        layer = f'<span class="layer layer-{esc(identity.layer_kind)}">{esc(identity.layer)}</span>'
    return (
        f'<aside class="sidebar"><div class="brand">{_mark()}<span>LifeIn</span>{layer}</div>'
        f'<nav>{"".join(groups)}</nav></aside>'
    )


def _topbar(identity: Identity | None) -> str:
    if identity is None:
        return ""
    right = []
    if identity.switch_href and identity.switch_label:
        right.append(
            f'<a class="btn btn-ghost" href="{esc(identity.switch_href)}">'
            f'{icon("back", size=16)}<span>{esc(identity.switch_label)}</span></a>'
        )
    if identity.sign_out_href:
        right.append(
            f'<form method="post" action="{esc(identity.sign_out_href)}">'
            f'<button class="btn btn-ghost" type="submit">{icon("logout", size=16)}'
            "<span>退出</span></button></form>"
        )
    return (
        f'<div class="topbar"><div class="who">{icon("users", size=16)}'
        f"<span>{esc(identity.name)}</span></div>"
        f'<div class="topbar-actions">{"".join(right)}</div></div>'
    )


# ---------------------------------------------------------------- Markdown

_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def markdown(text: str) -> str:
    """够用的一份 Markdown:标题、段落、列表、表格、引用、分隔线、粗体、行内代码。

    **不引 markdown 库。** 要渲染的只有一份文件(隐私说明),而它的写法是
    这个仓库自己的写法 —— 装一个包来读自己写的东西要先补一条 ADR
    (AGENTS.md §3),而那条 ADR 的理由会是"少写六十行"。

    **先转义再拼标签,顺序不能反。** 反过来的话文档里的一个 `<` 会把后面
    整段吞掉 —— 而这份文档是给还没接入的人看的,它坏掉的样子恰恰是最不该
    出现的。

    链接只保留文字、丢掉地址:控制台上不该有任何外部引用
    (`tests/test_console.py` 盯着这一条),而一个指向 `06-data-model.md`
    的相对链接在浏览器里是 404。
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []
    rows: list[list[str]] = []
    code: list[str] = []
    in_code = False

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    def flush_table() -> None:
        if not rows:
            return
        header, *rest = rows
        # 第二行是 |---|---| 那种分隔行,跳过它
        rest = [r for r in rest if not all(set(c.strip()) <= set("-: ") for c in r)]
        head = "".join(f"<th>{_inline(c)}</th>" for c in header)
        body = "".join(
            "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in rest
        )
        out.append(
            f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>"
        )
        rows.clear()

    def flush_all() -> None:
        flush_para()
        flush_bullets()
        flush_table()

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
                code.clear()
            else:
                flush_all()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            flush_all()
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_para()
            flush_bullets()
            rows.append([c.strip() for c in stripped.strip("|").split("|")])
            continue
        flush_table()
        if stripped.startswith("#"):
            flush_all()
            level = min(len(stripped) - len(stripped.lstrip("#")), 5)
            out.append(f"<h{level + 1}>{_inline(stripped.lstrip('#').strip())}</h{level + 1}>")
            continue
        if stripped in {"---", "***", "___"}:
            flush_all()
            out.append("<hr>")
            continue
        if stripped.startswith("> "):
            flush_all()
            out.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_para()
            bullets.append(stripped[2:])
            continue
        flush_bullets()
        para.append(stripped)

    flush_all()
    return "".join(out)


def _inline(text: str) -> str:
    out = html.escape(text)
    out = _LINK.sub(r"\1", out)  # 链接只留文字 —— 见 markdown() 的说明
    out = _CODE.sub(r'<code class="mono">\1</code>', out)
    return _BOLD.sub(r"<strong>\1</strong>", out)
