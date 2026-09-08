"""Web 控制台(P4 第 9 片)。

[03 的 P4](../../docs/03-roadmap.md#p4--多用户托管):

> **Web 控制台**(从 P3 推迟至此):数据导出、账号与授权管理、隐私说明页 ——
> 移动端查看已由 App 覆盖,**Web 只剩这些 P4 的合规必需项**

那句"只剩"是这一片的边界。**账本、待办、记忆都不进来** —— App 已经有了,
而一份两处实现的界面会有两套 bug 和两次要改。

## 认证:一次性链接,不是登录

这里有一个真实的设计问题:**浏览器打开一个链接时带不了 `Authorization` 头**。
所以 App 里那套 Bearer token 在 Web 上直接用不了。

三条路,选了第三条:

| 方案 | 为什么不 |
| --- | --- |
| 让人粘贴 token | 非技术背景的朋友做不到,而这个控制台正是给他用的 |
| 用户名密码 + cookie | **多一个认证面就多一处会被攻破的地方**，而凭据面已经够多（R11） |
| **一次性链接**(选它) | App 里点一下"在浏览器打开",拿一个短命的一次性 token,跳过去 |

一次性链接和[配码换取码](enroll.py)是同一个形状 —— 而**同一个形状用两次,
比为第二次发明一套新的更可靠**。

**链接失效之后回到的是首页,不是报错页。** 一个过期的链接对用户来说
不是"出错了",是"再点一次" —— 而报错页会让人以为自己做错了什么。

## 页面是服务端渲染的,没有前端框架

三个页面、几十行 HTML。上一个前端框架意味着一套构建、一份依赖清单、
一次 npm audit —— 而 [ADR-016](../../docs/04-tech-decisions.md#adr-016--服务端用-python--fastapi)
定的是"能被 systemd 拉起来的单进程"。
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Response
from pydantic import BaseModel

from lifein.api.deps import AppCaller, NowDep, SessionDep
from lifein.repos import console_links, credentials, export, users

log = logging.getLogger(__name__)

router = APIRouter(tags=["console"])

LINK_TTL = timedelta(minutes=15)
"""控制台链接活多久。**比配码那张长一点** —— 配码是当场扫,
而这个人可能要在浏览器里读一会儿隐私说明、导一次数据。"""


class ConsoleLink(BaseModel):
    url: str
    expires_at: datetime


@router.post("/app/console/link")
def make_link(caller: AppCaller, session: SessionDep, now: NowDep) -> ConsoleLink:
    """从 App 换一个能在浏览器里打开的链接。**一次性、十五分钟。**

    这是这个控制台唯一的入口 —— 没有登录页,也没有密码。
    """
    token, link = console_links.issue(caller.user_id, session, now=now, ttl=LINK_TTL)
    return ConsoleLink(url=f"/console?t={token}", expires_at=link.expires_at)


@router.get("/console")
def console_home(session: SessionDep, now: NowDep, t: str | None = None) -> Response:
    """控制台首页:这个人有哪些设备、能导出、能读隐私说明。"""
    user_id = console_links.resolve(session, token=t, now=now) if t else None
    if user_id is None:
        # **过期的链接不是错误,是"再点一次"。** 报错页会让人以为自己做错了什么
        return _page(
            "LifeIn",
            "<p>这个链接过期了,或者已经用过。</p>"
            "<p>在 App 里重新点一次「在浏览器打开」就好。</p>"
            '<p><a href="/console/privacy">先看看隐私说明</a></p>',
        )

    user = users.get_user(user_id, session)
    devices = credentials.list_device_credentials(user_id, session)
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(d.device_id or "(没有名字)"),
            html.escape(d.kind),
            "已吊销" if d.revoked_at else "有效",
        )
        for d in devices
    )

    return _page(
        f"LifeIn · {html.escape(user.display_name) if user else ''}",
        f"""
        <h2>你的设备</h2>
        <table><tr><th>设备</th><th>用途</th><th>状态</th></tr>{rows or
            '<tr><td colspan="3">还没有配过设备</td></tr>'}</table>
        <p class="hint">两条一台:采集一把密钥、查询一把,分开签发。
        手机丢了告诉白杨,吊销这台的两条即可,别的设备不受影响。</p>

        <h2>你的数据</h2>
        <p><a href="/console/export?t={html.escape(t or '')}">下载一份导出</a>
        (JSON,不含凭据)</p>
        <p class="hint">这份文件里有你的全部内容。<strong>凭据不在里面</strong> ——
        它会躺在你的下载目录,而如果里面有邮箱授权码,
        那份文件就比它保护的东西还危险。</p>

        <h2>关掉与删除</h2>
        <p class="hint"><strong>在 App 里</strong>:状态页 → 关掉采集 / 删掉已采的数据。
        不放在这里,是因为那两个动作在手机上随手就能做,
        而这个页面每次都要重新点一个链接才能打开。</p>

        <p><a href="/console/privacy">隐私说明</a></p>
        """,
    )


@router.get("/console/export")
def download_export(session: SessionDep, now: NowDep, t: str | None = None) -> Response:
    """把导出文件直接下下来。**同一个一次性 token 换出来的会话内有效。**"""
    user_id = console_links.resolve(session, token=t, now=now) if t else None
    if user_id is None:
        return _page("LifeIn", "<p>这个链接过期了。在 App 里重新点一次。</p>")

    data = export.export_user(user_id, session, now=now)
    payload = {
        "user_id": data.user_id,
        "exported_at": data.exported_at.isoformat(),
        "note": "凭据(邮箱授权码、设备密钥)不在这份文件里,见隐私说明第 5 节",
        "tables": data.tables,
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="lifein-{data.user_id[:8]}.json"'
        },
    )


@router.get("/console/privacy")
def privacy() -> Response:
    """隐私说明。**这一页不需要任何认证** —— 一份要先登录才能看的隐私说明,
    等于没有隐私说明:接入之前的人恰恰是最该读它的那个。
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "docs" / "09-privacy.md"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        # 文件没跟着部署上来。**说出来而不是显示一个空页** ——
        # 空页看起来像"这个系统不收集什么",而那是一句假话
        log.error("隐私说明文件读不到:%s", source)
        return _page("隐私说明", "<p>隐私说明暂时读不到。在接入之前请先问白杨要一份。</p>")

    return _page("隐私说明", f"<pre>{html.escape(text)}</pre>")


def _page(title: str, body: str) -> Response:
    """最简单的一张页面。**没有前端框架**(见模块开头)。"""
    return Response(
        content=f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
 body {{ font: 16px/1.7 system-ui, sans-serif; max-width: 44rem; margin: 2rem auto;
        padding: 0 1rem; color: #222; }}
 table {{ border-collapse: collapse; width: 100%; }}
 td, th {{ border-bottom: 1px solid #ddd; padding: .4rem .2rem; text-align: left; }}
 .hint {{ color: #666; font-size: .9rem; }}
 pre {{ white-space: pre-wrap; word-break: break-word; }}
</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>""",
        media_type="text/html; charset=utf-8",
    )
