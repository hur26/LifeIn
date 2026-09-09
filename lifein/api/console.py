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

## token 进了 URL,所以它只在门口出现一次

URL 里的 token 会进浏览器历史、进反代的访问日志、可能进 Referer。
原来页面里每个链接都带着它(`/console/export?t=…`),于是那串东西**在整个
会话里反复出现**,而**导出那一条是全部个人数据**:任何拿到那行历史记录的人,
十五分钟内点一下就能把它下下来。

改成两步:

1. `/console?t=…` 认出人之后 **换成一个 HttpOnly 的 cookie,然后 303 跳到
   干净的 `/console`** —— 地址栏和后面每一次请求里都不再有 token
2. 页面里的链接一个都不带 token。导出读 cookie,不读 query

**导出还改成了 POST。** GET 会被浏览器预取、被 Referer 带走、被"重新打开
上次的标签页"重放,而**下载一份全部个人数据不该是一个能被顺手重放的动作**。
一个表单按钮和一个链接在用户眼里没有区别。

cookie 上的四样都不是可选的:`HttpOnly`(页面脚本读不到)、`Secure`
(不走明文)、`SameSite=Strict`(别的站点点过来时不带上)、
`Path=/console`(别的接口拿不到它)。

**`Secure` 意味着控制台必须走 HTTPS。** 这和 App 那边"base_url 必须是 https"
是同一条(ADR-022 之后服务端本来就在 TLS 后面),而为了本机调试放开它,
等于让线上那份也少一道。

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
from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from lifein import qr
from lifein.api.deps import AppCaller, NowDep, SessionDep, SettingsDep
from lifein.repos import console_links, credentials, enrollment, export, users

log = logging.getLogger(__name__)

router = APIRouter(tags=["console"])

INVITE_TTL = timedelta(minutes=10)
"""配码活多久。**和 `enrollment.DEFAULT_TTL` 一样,而且是有意重复写一遍的** ——
控制台上那句"十分钟内有效"是给人看的,它必须和真正的过期时间是同一个数,
而不是各写各的。

比控制台链接那十五分钟短:配码是当场扫的动作,而在聊天记录里躺三天的码
等于没有一次性。
"""

COOKIE = "lifein_console"
"""会话 cookie 的名字。**值就是那张 token** —— 库里存的仍然是它的 sha256,
换个名字并不会多一层保护,而少一个概念少一处会写错的地方。"""

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
def console_home(
    session: SessionDep,
    now: NowDep,
    t: str | None = None,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """控制台首页:这个人有哪些设备、能导出、能读隐私说明。

    **带 `?t=` 进来的那一次不渲染页面,而是换 cookie 之后跳到干净的 URL。**
    见模块开头:token 只在门口出现一次。
    """
    if t:
        link = console_links.resolve(session, token=t, now=now)
        if link is not None:
            return _redirect_with_cookie(t, expires_in=link.expires_at - now)
        # token 不认识就当没带,落到下面那张"再点一次"的页面

    link = (
        console_links.resolve(session, token=lifein_console, now=now)
        if lifein_console
        else None
    )
    user_id = link.user_id if link else None
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
    # 首页那句"几分钟内有效"和出码页上那句必须是同一个数 ——
    # 各写各的话,先过期的是用户不知道的那一个
    invite_minutes = int(INVITE_TTL.total_seconds() // 60)
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

        <form method="post" action="/console/devices/invite">
          <button type="submit">添加设备</button>
        </form>
        <p class="hint">出一张二维码,在新手机的 LifeIn 里扫它。
        <strong>图里没有密钥</strong> —— 只有一张 {invite_minutes} 分钟内、
        只能用一次的换取码,所以它可以直接发过去。</p>

        <h2>你的数据</h2>
        <form method="post" action="/console/export">
          <button type="submit">下载一份导出</button>(JSON,不含凭据)
        </form>
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


@router.post("/console/export")
def download_export(
    session: SessionDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """把导出文件直接下下来。**只认 cookie,而且只认 POST。**

    GET 会被浏览器预取、被 Referer 带走、被"重新打开上次的标签页"重放,
    而这一条下下来的是**全部个人数据** —— 不该是一个能被顺手重放的动作。
    """
    link = (
        console_links.resolve(session, token=lifein_console, now=now)
        if lifein_console
        else None
    )
    if link is None:
        return _page("LifeIn", "<p>这个链接过期了。在 App 里重新点一次。</p>")
    user_id = link.user_id

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


@router.post("/console/devices/invite")
def add_device(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """出一张配码二维码(03 的 P4:"Web 控制台里点添加设备 → 页面显示二维码")。

    **这一条是那句"现在的配码要人在服务器上跑 `issue-device`,非技术背景的人
    做不到"的答案。** 二维码的内容在 P4 第 1 片已经修对了(里面是一次性换取码,
    不是密钥),而出码这个动作在这之前还留在终端里。

    **POST 不是 GET。** 它签发的是一张能换走两把密钥的码,而 GET 会被浏览器
    预取、被"重新打开上次的标签页"重放 —— **每重放一次就多一张有效的码**,
    每一张都能配上一台设备。

    **`user_id` 从会话里取,页面上没有任何地方能指定别人。** 这张码只能配到
    点它的那个人自己的账号上;给朋友开账号是另一件事,那要跑 `admin add-user`。
    """
    link = (
        console_links.resolve(session, token=lifein_console, now=now)
        if lifein_console
        else None
    )
    if link is None:
        return _page("LifeIn", "<p>这个链接过期了。在 App 里重新点一次。</p>")

    base_url = settings.public_base_url if settings else None
    if not base_url:
        # **不猜一个地址。** 那个值会变成手机里"我的服务端在哪",
        # 而猜错的后果是他把自己的通知报到了别处(07 §2.1 那段)
        log.warning("没配 PUBLIC_BASE_URL,配不了码")
        return _page(
            "添加设备",
            "<p>服务端还没配 <code>PUBLIC_BASE_URL</code>,出不了配码。</p>"
            "<p class='hint'>那个值是手机要连的地址,它会被写进二维码。"
            "**不能从这次请求里推出来** —— 请求头是发请求的人说了算的,"
            "照着它生成的码可能把手机指到别人的服务器上。</p>",
        )

    code, issued = enrollment.issue(
        link.user_id, session, base_url=base_url, now=now, ttl=INVITE_TTL
    )
    payload = json.dumps(
        {"v": enrollment.INVITE_VERSION, "claim": code, "base_url": base_url}, ensure_ascii=False
    )
    minutes = int(INVITE_TTL.total_seconds() // 60)
    log.info("控制台出了一张配码:user=%s code_id=%s", link.user_id, issued.id)

    return _page(
        "添加设备",
        f"""
        <p>在新手机的 LifeIn 里扫它。<strong>{minutes} 分钟内有效,只能用一次。</strong></p>
        <div class="qr">{qr.render_qr_svg(payload)}</div>
        <p class="hint">扫不动的话,把下面这串粘进 App 的配码框:</p>
        <code>{html.escape(payload)}</code>
        <p class="hint"><strong>这一串只显示这一次。</strong>
        库里存的是它的哈希,服务端自己也说不出它是什么 ——
        关掉这一页就只能再出一张。</p>
        <p class="hint">图里没有密钥,所以它可以直接发给对方。
        真被别人截图拿到也只有两种结局:要么你已经换过了(他换不了),
        要么你还没换(你会发现自己换不了)。</p>
        <p><a href="/console">回到首页</a></p>
        """,
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


def _redirect_with_cookie(token: str, *, expires_in: timedelta) -> Response:
    """把 token 收进 cookie,然后跳到没有 query 的 `/console`。

    **303 而不是 302**:303 明确要求跳过去用 GET,而这条本来就是 GET ——
    写死它是为了不依赖各家浏览器对 302 的历史习惯。
    """
    response = RedirectResponse(url="/console", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE,
        value=token,
        # **cookie 不该活得比 token 长**(见 `console_links.Resolved`)
        max_age=max(int(expires_in.total_seconds()), 0),
        httponly=True,   # 页面脚本读不到
        secure=True,     # 不走明文 —— 控制台必须在 TLS 后面
        samesite="strict",  # 别的站点点过来时不带上
        path="/console",  # 别的接口拿不到它
    )
    return response


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
 code {{ display: block; word-break: break-all; background: #f6f6f6;
         padding: .6rem; border-radius: 4px; font-size: .85rem; }}
 .qr svg {{ width: 280px; height: 280px; }}
 button {{ font: inherit; padding: .5rem 1rem; }}
</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>""",
        media_type="text/html; charset=utf-8",
    )
