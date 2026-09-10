"""Web 控制台的**用户层**(P4 第 9 片,2026-09-10 按 ADR-029 重做)。

[03 的 P4](../../docs/03-roadmap.md#p4--多用户托管) 现在把 Web 分成两层。
这个文件是不登录的那一层:

> **用户层**(不登录,入口是 App 里点出来的一次性链接):数据导出与删除、
> 设备与配码、自己的采集开关与白名单、隐私说明页

运营层在 [`admin_console.py`](admin_console.py),那一层要口令。

**账本、待办、记忆仍然不进来。** 总览上只有数字,点它跳去 App ——
一个数字不会长出第二套确认逻辑,而一份可编辑的账本列表会
([ADR-029](../../docs/04-tech-decisions.md))。

## 认证:一次性链接,不是登录

这里有一个真实的设计问题:**浏览器打开一个链接时带不了 `Authorization` 头**。
所以 App 里那套 Bearer token 在 Web 上直接用不了。

三条路,选了第三条:

| 方案 | 为什么不 |
| --- | --- |
| 让人粘贴 token | 非技术背景的朋友做不到,而这个控制台正是给他用的 |
| 用户名密码 + cookie | **多一个认证面就多一处会被攻破的地方**,而凭据面已经够多(R11) |
| **一次性链接**(选它) | App 里点一下「在浏览器打开」,拿一个短命的 token,跳过去 |

一次性链接和[配码换取码](enroll.py)是同一个形状 —— 而**同一个形状用两次,
比为第二次发明一套新的更可靠**。

**链接失效之后回到的是首页,不是报错页。** 一个过期的链接对用户来说
不是「出错了」,是「再点一次」 —— 而报错页会让人以为自己做错了什么。

## token 进了 URL,所以它只在门口出现一次

URL 里的 token 会进浏览器历史、进反代的访问日志、可能进 Referer。
原来页面里每个链接都带着它(`/console/export?t=…`),于是那串东西**在整个
会话里反复出现**,而**导出那一条是全部个人数据**。

现在是两步:

1. `/console?t=…` 认出人之后 **换成一个 HttpOnly 的 cookie,然后 303 跳到
   干净的 `/console`** —— 地址栏和后面每一次请求里都不再有 token
2. 页面里的链接一个都不带 token。所有动作读 cookie,不读 query

cookie 上的四样都不是可选的:`HttpOnly`(页面脚本读不到)、`Secure`
(不走明文)、`SameSite=Strict`(别的站点点过来时不带上)、
`Path=/console`(别的接口拿不到它)。

**`SameSite=Strict` 同时是这一层的 CSRF 防线。** 别的站点上的一张表单提交到
这里时,浏览器不会带上会话 cookie —— 于是它落到「这个链接过期了」那一页,
而不是删掉谁的数据。所以这里没有 CSRF token 字段:
**一道靠浏览器强制的防线,比一个要每张表单都记得加的隐藏字段更难漏掉。**

## 会改变什么的动作一律是 POST,而危险的那些要点两次

GET 会被浏览器预取、被 Referer 带走、被「重新打开上次的标签页」重放。
删数据、吊销设备、签发配码 —— 每重放一次都真的发生一次。

删除和吊销还要**再点一次**:第一次 POST 渲染一张说清后果的确认页,
第二次才动手。**没有 JavaScript 的 confirm 弹窗**(这套界面里一个字节的
脚本都没有),而一张写清后果的页面比一个弹窗说得更明白。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from lifein import qr
from lifein.api import ui
from lifein.api.deps import AppCaller, NowDep, SessionDep, SettingsDep, form_values
from lifein.repos import (
    collector,
    console_links,
    credentials,
    data_control,
    enrollment,
    export,
    pending,
    reports,
    todos,
    users,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["console"])

INVITE_TTL = timedelta(minutes=10)
"""配码活多久。**和 `enrollment.DEFAULT_TTL` 一样,而且是有意重复写一遍的** ——
控制台上那句「十分钟内有效」是给人看的,它必须和真正的过期时间是同一个数,
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

FormDep = Annotated[dict, Depends(form_values)]

_NAV = (
    ui.NavGroup(
        "你的账号",
        (
            ui.NavItem("overview", "总览", "/console", "gauge"),
            ui.NavItem("devices", "设备", "/console/devices", "phone"),
            ui.NavItem("collection", "采集", "/console/collection", "antenna"),
            ui.NavItem("data", "数据", "/console/data", "box"),
        ),
    ),
    ui.NavGroup("说明", (ui.NavItem("privacy", "隐私说明", "/console/privacy", "shield"),)),
)


class ConsoleLink(BaseModel):
    url: str
    expires_at: datetime


# ---------------------------------------------------------------- 进门


@router.post("/app/console/link")
def make_link(caller: AppCaller, session: SessionDep, now: NowDep) -> ConsoleLink:
    """从 App 换一个能在浏览器里打开的链接。**一次性、十五分钟。**

    这是用户层唯一的入口 —— 没有登录页,也没有密码。
    """
    token, link = console_links.issue(caller.user_id, session, now=now, ttl=LINK_TTL)
    return ConsoleLink(url=f"/console?t={token}", expires_at=link.expires_at)


# ---------------------------------------------------------------- 总览


@router.get("/console")
def console_home(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    t: str | None = None,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """总览:这套东西还活着吗、有什么等着我。

    **带 `?t=` 进来的那一次不渲染页面,而是换 cookie 之后跳到干净的 URL。**
    见模块开头:token 只在门口出现一次。
    """
    if t:
        link = console_links.resolve(session, token=t, now=now)
        if link is not None:
            return _redirect_with_cookie(t, expires_in=link.expires_at - now)
        # token 不认识就当没带,落到下面那张「再点一次」的页面

    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    user = users.get_user(user_id, session)
    devices = credentials.list_device_credentials(user_id, session)
    beats = {b.device_id: b for b in collector.list_heartbeats(user_id, session)}
    state = data_control.state(user_id, session)
    stale_after = now - timedelta(minutes=settings.collector_heartbeat_timeout_m)

    open_pending = pending.count_pending(user_id, session, now=now)
    open_todos = len(todos.list_open(user_id, session, until=now + timedelta(days=7)))
    report = reports.monthly(user_id, session, now=now)

    live = [d for d in devices if d.revoked_at is None]
    silent = [b for b in beats.values() if b.is_stale(cutoff=stale_after)]

    body = ui.stats(
        [
            (
                "采集",
                "正在采集" if state.enabled else "没有在采集",
                f"{state.active_devices} 台设备 · {state.enabled_rules} 条来源",
                "ok" if state.enabled else "warn",
            ),
            (
                "设备",
                f'{len(live)}<span class="unit">条凭据</span>',
                f"掉线 {len(silent)} 台" if silent else "都在按时上报",
                "danger" if silent else "",
            ),
            (
                "待确认",
                f'{open_pending}<span class="unit">条</span>',
                "在 App 里点头之后才会入账" if open_pending else "都处理完了",
                "warn" if open_pending else "",
            ),
            (
                f"{report.period} 支出",
                f'<span class="unit">¥</span>{_money(report.total)}',
                f"{report.count} 笔 · 七天内待办 {open_todos} 条",
                "",
            ),
        ]
    )

    body += _warnings(state=state, silent=silent, devices=live)

    body += ui.card(
        "你的设备",
        _device_table(devices, beats, stale_after),
        actions=ui.link_button("管理设备", "/console/devices"),
        hint_text="两条一台:采集一把密钥、查询一把,分开签发。"
        "手机丢了在「设备」里吊销这一台,别的设备不受影响。",
    )

    body += ui.card(
        "账本、待办、记忆在 App 里",
        ui.hint(
            "这三样这个网页上没有,<strong>而且是有意的</strong> —— 它们在手机上"
            "已经有一整套界面,而一份两处实现的界面会有两套 bug 和两次要改。"
            "上面那几个数字是给你判断「要不要现在打开 App」用的。"
        ),
    )

    name = user.display_name if user else ""
    return _html(
        _page(
            user,
            active="overview",
            title=f"LifeIn · {name}",
            heading=f"你好,{name}",
            lede="这一页回答一件事:<strong>这套东西还活着吗,有什么等着你</strong>。",
            body=body,
        )
    )


def _warnings(*, state, silent: list, devices: list) -> str:
    """页面顶上那几条。**只在真出事时出现。**

    常驻的提示会被读成装饰,而装饰是不会被读的 —— 于是真出事那次也被略过。
    """
    out = []
    if not devices:
        out.append(
            ui.banner(
                "<strong>还没有配过设备。</strong>去「设备」点一下「添加设备」出一张二维码,"
                "在手机的 LifeIn 里扫它。",
                tone="info",
                icon_name="spark",
            )
        )
    elif not state.enabled:
        why = (
            "一条来源都没放行 —— 采集器送上去的东西全会被丢掉。"
            if not state.enabled_rules
            else "采集密钥全被吊销了,手机上那台送不进来。"
        )
        out.append(ui.banner(f"<strong>没有在采集。</strong>{why}", tone="warn"))
    if silent:
        names = ui.esc("、".join(b.device_id for b in silent))
        out.append(
            ui.banner(
                f"<strong>{names}</strong> 已经超过心跳时限没上报了。"
                "多半是手机上把后台限制打开了,或者通知使用权被系统收回。",
                tone="danger",
            )
        )
    return "".join(out)


# ---------------------------------------------------------------- 设备


@router.get("/console/devices")
def devices_page(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """设备与配码 —— 03 的 P4 那句「账号与授权管理」。"""
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    user = users.get_user(user_id, session)
    devices = credentials.list_device_credentials(user_id, session)
    beats = {b.device_id: b for b in collector.list_heartbeats(user_id, session)}
    stale_after = now - timedelta(minutes=settings.collector_heartbeat_timeout_m)
    minutes = int(INVITE_TTL.total_seconds() // 60)

    body = ui.card(
        "你的设备",
        _device_table(devices, beats, stale_after, with_actions=True),
        actions=ui.form("/console/devices/invite", ui.button("添加设备", icon_name="plus")),
        hint_text="「添加设备」会出一张二维码,在新手机的 LifeIn 里扫它。"
        f"<strong>图里没有密钥</strong> —— 只有一张 {minutes} 分钟内、只能用一次的"
        "换取码,所以它可以直接发过去。",
    )

    body += ui.card(
        "手机丢了怎么办",
        ui.hint(
            "在上面吊销那台设备。<strong>吊销是服务端这边的动作,不需要手机配合</strong> —— "
            "那台手机从此既送不进来也读不出去,而别的设备一点都不受影响。"
            "<br>已经吊销的那几行不会消失:哪台设备什么时候被吊销的,是排查"
            "「丢了之后还有没有人在用」时唯一的线索。"
        ),
    )

    return _html(
        _page(
            user,
            active="devices",
            title="设备 · LifeIn",
            heading="设备",
            lede="每台手机两条凭据:<strong>采集一把、查询一把,分开签发</strong> —— "
            "采集端只能写,读不到你的任何东西。",
            body=body,
        )
    )


@router.post("/console/devices/invite")
def add_device(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """出一张配码二维码(03 的 P4:「Web 控制台里点添加设备 → 页面显示二维码」)。

    **这一条是那句「现在的配码要人在服务器上跑 `issue-device`,非技术背景的人
    做不到」的答案。** 二维码的内容在 P4 第 1 片已经修对了(里面是一次性换取码,
    不是密钥),而出码这个动作在这之前还留在终端里。

    **POST 不是 GET。** 它签发的是一张能换走两把密钥的码,而 GET 会被浏览器
    预取、被「重新打开上次的标签页」重放 —— **每重放一次就多一张有效的码**。

    **`user_id` 从会话里取,页面上没有任何地方能指定别人。** 这张码只能配到
    点它的那个人自己的账号上;给朋友开账号是另一件事,那要跑 `admin create-user`。
    """
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()
    user = users.get_user(user_id, session)

    base_url = settings.public_base_url if settings else None
    if not base_url:
        # **不猜一个地址。** 那个值会变成手机里「我的服务端在哪」,
        # 而猜错的后果是他把自己的通知报到了别处(07 §2.1 那段)
        log.warning("没配 PUBLIC_BASE_URL,配不了码")
        return _html(
            _page(
                user,
                active="devices",
                title="添加设备 · LifeIn",
                heading="出不了配码",
                body=ui.card(
                    "服务端还没配 PUBLIC_BASE_URL",
                    ui.hint(
                        "那个值是手机要连的地址,它会被写进二维码。"
                        "<strong>不能从这次请求里推出来</strong> —— 请求头是发请求的人"
                        "说了算的,照着它生成的码可能把手机指到别人的服务器上。"
                    ),
                    actions=ui.link_button("回到设备", "/console/devices"),
                    tone="danger",
                ),
            )
        )

    code, issued = enrollment.issue(user_id, session, base_url=base_url, now=now, ttl=INVITE_TTL)
    payload = json.dumps(
        {"v": enrollment.INVITE_VERSION, "claim": code, "base_url": base_url},
        ensure_ascii=False,
    )
    minutes = int(INVITE_TTL.total_seconds() // 60)
    log.info("控制台出了一张配码:user=%s code_id=%s", user_id, issued.id)

    body = ui.card(
        "在新手机的 LifeIn 里扫它",
        f'<div class="qr">{qr.render_qr_svg(payload)}</div>'
        f'<p class="hint"><strong>{minutes} 分钟内有效,只能用一次。</strong>'
        "扫不动的话,把下面这一串粘进 App 的配码框:</p>"
        f'<code class="payload">{ui.esc(payload)}</code>',
        actions=ui.link_button("回到设备", "/console/devices"),
        hint_text="<strong>这一串只显示这一次。</strong>库里存的是它的哈希,"
        "服务端自己也说不出它是什么 —— 关掉这一页就只能再出一张。",
    )
    body += ui.card(
        "为什么这张图可以直接发出去",
        ui.hint(
            "图里没有密钥,只有一张换取码。真被别人截图拿到也只有两种结局:"
            "要么你已经换过了(他换不了),要么你还没换(你会发现自己换不了)。"
        ),
    )

    return _html(
        _page(user, active="devices", title="添加设备 · LifeIn", heading="添加设备", body=body)
    )


@router.post("/console/devices/revoke")
async def revoke_device(
    session: SessionDep,
    now: NowDep,
    payload: FormDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """吊销一台设备的**全部**凭据。**要点两次。**

    默认吊销两条而不是一条,因为触发它的场景是「手机丢了」 ——
    那时只吊销其中一种,等于把另一种留在别人手里(`credentials.revoke_device`)。
    """
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    device_id = _clean_device_id(payload.get("device_id"))
    if not device_id:
        return _back("/console/devices")

    if payload.get("confirm") != "yes":
        return _html(
            _page(
                users.get_user(user_id, session),
                active="devices",
                title="吊销设备 · LifeIn",
                heading="吊销这台设备?",
                body=ui.card(
                    f"{device_id} 的两条凭据都会作废",
                    ui.hint(
                        "那台手机从此既送不进来也读不出去。"
                        "<strong>已经采到的数据不受影响</strong> —— 要删数据是另一件事,"
                        "在「数据」里。"
                        "<br>吊销之后想再用这台手机,重新出一张配码扫一次就行。"
                    )
                    + ui.form(
                        "/console/devices/revoke",
                        ui.hidden("device_id", device_id)
                        + ui.hidden("confirm", "yes")
                        + ui.button("吊销", tone="danger"),
                        cls="page-actions",
                    ),
                    actions=ui.link_button("算了", "/console/devices"),
                    tone="danger",
                ),
            )
        )

    count = credentials.revoke_device(user_id, session, device_id=device_id)
    log.info("控制台吊销了一台设备:user=%s device=%s 条数=%s", user_id, device_id, count)
    return _back("/console/devices")


# ---------------------------------------------------------------- 采集


@router.get("/console/collection")
def collection_page(
    session: SessionDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """采集开关与白名单。

    **这一页是 [R10 改判](../../docs/05-risks.md#r10--手机端采集器的越权读取)
    四个前提里第 2 件在 Web 上的样子**:朋友要能自己关掉采集,不是「找你帮忙」。
    App 里已经有,而**一个人坐在电脑前的时候不该被要求先去掏手机**。
    """
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    user = users.get_user(user_id, session)
    state = data_control.state(user_id, session)
    rules = collector.list_whitelist(user_id, session)

    mark = ui.badge("正在采集", tone="ok") if state.enabled else ui.badge("没有在采集", tone="warn")
    stop = (
        ui.form("/console/collection/stop", ui.button("关掉采集", tone="danger"))
        if state.enabled
        else ""
    )
    body = ui.card(
        "现在的状态",
        f"<p>{mark} &nbsp; {state.active_devices} 台设备在采、"
        f"{state.enabled_rules} 条来源放行中</p>",
        actions=stop,
        hint_text="<strong>三层里任何一层关着就算关。</strong>关掉采集会吊销全部采集密钥"
        "并停用所有来源,服务端这边做完就算数,<strong>不等 App 配合</strong>。"
        "<br>它不删任何已经采到的数据 —— 那在「数据」里。两件事分开是有意的:"
        "合成一个的话,「我想先停下来想想」就变成了「要么继续采要么全删」。",
    )

    rows = [
        [
            ui.mono(rule.pattern),
            _purpose_label(rule.purpose),
            ui.badge("放行中", tone="ok") if rule.enabled else ui.badge("已停用", tone="neutral"),
            '<div class="row-actions">'
            + ui.form(
                "/console/collection/sources/toggle",
                ui.hidden("rule_id", rule.id)
                + ui.hidden("enabled", "0" if rule.enabled else "1")
                + ui.button("停用" if rule.enabled else "放行", tone="quiet"),
            )
            + "</div>",
        ]
        for rule in rules
    ]

    body += ui.card(
        "放行的来源",
        ui.table(
            ["匹配", "用途", "状态", ""],
            rows,
            empty="一条都没有 —— 采集器送上去的东西全会被丢掉",
        )
        + ui.form(
            "/console/collection/sources",
            ui.text_field("pattern", "加一个包名", placeholder="com.tencent.mm")
            + ui.button("放行", icon_name="plus"),
            cls="inline-form",
        ),
        hint_text="<strong>默认拒绝</strong>:不在这份名单上的一律丢掉。"
        "<br><strong>没有删除,只有停用</strong> —— 留着那一行才回答得了"
        "「曾经放行过谁」,而排查越权读取时那是唯一的线索。"
        '<br>这里只放消息类。银行与支付类要在服务器上跑 <code class="mono">'
        "admin allow-source</code>,好让「我知道我在打开什么」至少发生过一次。",
    )

    body += ui.details(
        "验证码呢?",
        ui.hint(
            "<strong>验证码类内容双重丢弃</strong>:手机端过滤一次,服务端入库前再过"
            "一次正则(铁律 11)。放行了微信不等于验证码会被存下来 —— "
            "它在那两道正则之间就没了。"
        ),
    )

    return _html(
        _page(
            user,
            active="collection",
            title="采集 · LifeIn",
            heading="采集",
            lede="决定<strong>哪些 App 的通知会被送上来</strong>,以及要不要整个停下来。",
            body=body,
        )
    )


@router.post("/console/collection/sources")
async def allow_source(
    session: SessionDep,
    now: NowDep,
    payload: FormDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """放行一个包名。**只能放消息类。**

    `purpose` 写死成 `message`,页面上没有地方能选 —— 银行与支付类会直接进
    记账链路,而那一档要在终端上做一次显式的动作(`admin allow-source`)。
    """
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    pattern = str(payload.get("pattern", "")).strip()
    if pattern:
        collector.add_whitelist(
            user_id,
            session,
            match_type=collector.MATCH_PACKAGE,
            pattern=pattern,
            purpose=collector.PURPOSE_MESSAGE,
            phase="P1",
        )
        log.info("控制台放行了一条来源:user=%s pattern=%s", user_id, pattern)
    return _back("/console/collection")


@router.post("/console/collection/sources/toggle")
async def toggle_source(
    session: SessionDep,
    now: NowDep,
    payload: FormDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """开关一条来源。**没有删除**(06 §6.9)。"""
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    try:
        rule_id = int(payload.get("rule_id", ""))
    except ValueError:
        return _back("/console/collection")

    collector.set_whitelist_enabled(
        user_id, session, rule_id=rule_id, enabled=payload.get("enabled") == "1"
    )
    return _back("/console/collection")


@router.post("/console/collection/stop")
async def stop_collection(
    session: SessionDep,
    now: NowDep,
    payload: FormDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """关掉采集。**要点两次。**"""
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    if payload.get("confirm") != "yes":
        return _html(
            _page(
                users.get_user(user_id, session),
                active="collection",
                title="关掉采集 · LifeIn",
                heading="关掉采集?",
                body=ui.card(
                    "全部采集密钥会被吊销,全部来源会被停用",
                    ui.hint(
                        "服务端这边做完就算数,<strong>不等 App 配合</strong>。"
                        "<br><strong>已经采到的数据一条都不会少</strong> —— 要删是另一件事。"
                        "<br>想开回来:重新出一张配码配上设备,再把来源逐条放行回去。"
                        "停用的规则还在,不用重新一条条加。"
                    )
                    + ui.form(
                        "/console/collection/stop",
                        ui.hidden("confirm", "yes") + ui.button("关掉", tone="danger"),
                        cls="page-actions",
                    ),
                    actions=ui.link_button("算了", "/console/collection"),
                    tone="danger",
                ),
            )
        )

    state = data_control.stop_collecting(user_id, session)
    log.info("控制台关掉了采集:user=%s 剩余采集设备=%s", user_id, state.active_devices)
    return _back("/console/collection")


# ---------------------------------------------------------------- 数据


@router.get("/console/data")
def data_page(
    session: SessionDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """导出与删除 —— 03 的 P4 那句「数据导出」,以及 R10 改判的第 2 件。"""
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

    body = ui.card(
        "导出一份",
        ui.hint(
            "一个 JSON 文件,里面是你的<strong>全部内容</strong>:事件、账目、待办、"
            "记忆,以及它们各自的出处。"
        ),
        actions=ui.form("/console/export", ui.button("下载导出", icon_name="box")),
        hint_text="<strong>凭据不在里面</strong>(邮箱授权码、设备密钥都不在)。"
        "这份文件会躺在你的下载目录,而如果里面有邮箱授权码,"
        "那份文件就比它保护的东西还危险。",
    )

    body += ui.card(
        "删掉已采的数据",
        ui.hint(
            "通知原文、由它们记下的账、待确认的条目,以及<strong>出处只剩这些的</strong>"
            "记忆,会一起删掉。<strong>是真删,不是标记。</strong>"
        )
        + ui.form(
            "/console/data/delete", ui.button("删掉已采的数据", tone="danger"), cls="page-actions"
        ),
        hint_text="删掉数据<strong>不会</strong>顺手关掉采集 —— 两件事分开。"
        "只想停下来的话去「采集」。",
        tone="danger",
    )

    body += ui.details(
        "有两样东西删不掉,而这是有意的",
        ui.hint(
            "<strong>审计日志</strong>:哪个工具在什么时候被调用过。它记的是入参摘要"
            "不是原文,而删掉它等于让「这个系统对我做过什么」变得无法回答。"
            "<br><strong>被拒绝过的待确认</strong>:你拒绝过什么,正是那个 agent 最该"
            "学会不做的事。删了它,明天同一条会被重新推断出来。"
        ),
    )

    return _html(
        _page(
            users.get_user(user_id, session),
            active="data",
            title="数据 · LifeIn",
            heading="你的数据",
            lede="导出一份带走,或者把已经采到的删掉。<strong>两件事都不需要问任何人。</strong>",
            body=body,
        )
    )


@router.post("/console/export")
def download_export(
    session: SessionDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """把导出文件直接下下来。**只认 cookie,而且只认 POST。**

    GET 会被浏览器预取、被 Referer 带走、被「重新打开上次的标签页」重放,
    而这一条下下来的是**全部个人数据** —— 不该是一个能被顺手重放的动作。
    """
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()

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


@router.post("/console/data/delete")
async def delete_collected(
    session: SessionDep,
    now: NowDep,
    payload: FormDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """删掉采集来的数据。**要点两次,而且第二页要把后果写全。**"""
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _expired()
    user = users.get_user(user_id, session)

    if payload.get("confirm") != "yes":
        return _html(
            _page(
                user,
                active="data",
                title="删除数据 · LifeIn",
                heading="删掉已采的数据?",
                body=ui.card(
                    "删了就找不回来了",
                    ui.hint(
                        "会删掉:通知原文、由它们记下的账、待确认的条目、"
                        "出处只剩这些的记忆和它们的向量。"
                        "<br>不会删掉:审计日志、你拒绝过的那些待确认,"
                        "以及还有别的出处的记忆。"
                        "<br><strong>采集不会因此停下来</strong> —— 明天的通知照样会进来。"
                    )
                    + ui.form(
                        "/console/data/delete",
                        ui.hidden("confirm", "yes") + ui.button("删掉", tone="danger"),
                        cls="page-actions",
                    ),
                    actions=ui.link_button("算了", "/console/data"),
                    tone="danger",
                ),
            )
        )

    deleted = data_control.delete_collected(user_id, session)
    log.info("控制台删掉了采集数据:user=%s 事件=%s", user_id, deleted.raw_events)

    body = ui.card(
        "删完了",
        ui.stats(
            [
                ("事件", str(deleted.raw_events), "通知原文", ""),
                ("账目", str(deleted.transactions), "由它们记下的", ""),
                ("待确认", str(deleted.pending), "还没点头的", ""),
                ("记忆", str(deleted.facts), "出处只剩这些的", ""),
            ]
        ),
        actions=ui.link_button("回到数据", "/console/data"),
        hint_text="采集<strong>没有</strong>因此停下来。要停在「采集」里关。",
    )
    return _html(
        _page(user, active="data", title="删除完成 · LifeIn", heading="已经删掉了", body=body)
    )


# ---------------------------------------------------------------- 隐私说明


@router.get("/console/privacy")
def privacy(
    session: SessionDep,
    now: NowDep,
    lifein_console: Annotated[str | None, Cookie()] = None,
) -> Response:
    """隐私说明。**这一页不需要任何认证** —— 一份要先登录才能看的隐私说明,
    等于没有隐私说明:接入之前的人恰恰是最该读它的那个。

    有会话时带上导航,没有时是一张干净的长文页。**同一份内容,两种壳** ——
    内容不因为「认不认识你」而变,那正是一份隐私说明该有的样子。
    """
    source = Path(__file__).resolve().parents[2] / "docs" / "09-privacy.md"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        # 文件没跟着部署上来。**说出来而不是显示一个空页** ——
        # 空页看起来像「这个系统不收集什么」,而那是一句假话
        log.error("隐私说明文件读不到:%s", source)
        return _html(
            ui.bare_page(
                title="隐私说明 · LifeIn",
                heading="隐私说明暂时读不到",
                body=ui.hint("在接入之前请先问白杨要一份。"),
            )
        )

    body = f'<div class="card"><div class="card-body prose">{ui.markdown(text)}</div></div>'
    user_id = _resolve(session, now, lifein_console)
    if user_id is None:
        return _html(ui.bare_page(title="隐私说明 · LifeIn", heading="隐私说明", body=body))

    return _html(
        _page(
            users.get_user(user_id, session),
            active="privacy",
            title="隐私说明 · LifeIn",
            heading="隐私说明",
            lede="这一页<strong>不需要登录也能看</strong> —— 接入之前的人恰恰是最该读它的那个。",
            body=body,
        )
    )


# ---------------------------------------------------------------- 内部


def _resolve(session, now: datetime, cookie: str | None) -> str | None:
    """这次请求是谁。认不出来返回 None,调用方渲染「再点一次」。

    **每个处理函数各自调一次,而不是做成一个抛异常的依赖。** 抛出去的话它会撞上
    `create_app` 里那个「401 且响应体是空的」处理器 —— 那对 App 是对的
    (不告诉探测者他哪一步错了),对浏览器是一张白屏。
    """
    if not cookie:
        return None
    link = console_links.resolve(session, token=cookie, now=now)
    return link.user_id if link else None


def _page(user, *, active: str, title: str, heading: str, body: str, lede: str = "") -> str:
    """用户层的页面壳。**「运营台」那个入口只对运营者本人显示。**

    对别人显示它没有安全问题(那边照样要口令),但它会让一个朋友以为这里有一块
    他打不开的地方 —— 而这个控制台要给人的感觉恰恰相反。
    """
    is_admin = bool(user and user.is_admin)
    identity = ui.Identity(
        name=user.display_name if user else "你",
        layer="用户",
        layer_kind="user",
        switch_label="运营台" if is_admin else None,
        switch_href="/admin" if is_admin else None,
    )
    return ui.page(
        title=title,
        heading=heading,
        lede=lede,
        body=body,
        nav=_NAV,
        active=active,
        identity=identity,
    )


def _device_table(
    devices, beats: dict, stale_after: datetime, *, with_actions: bool = False
) -> str:
    """设备表。总览和设备页共用 —— **两处各写一份的话,「已吊销」会在其中一处
    显示成「有效」,而那是最不该出错的一格。**
    """
    rows = []
    for device in devices:
        beat = beats.get(device.device_id)
        if device.revoked_at is not None:
            state = ui.badge("已吊销", tone="neutral")
        elif beat is None:
            state = ui.badge("还没上报过", tone="warn")
        elif beat.is_stale(cutoff=stale_after):
            state = ui.badge("掉线", tone="danger")
        else:
            state = ui.badge("正常", tone="ok")

        row = [
            ui.mono(device.device_id or "(没有名字)"),
            _kind_label(device.kind),
            state,
            ui.when(beat.last_seen_at if beat else None),
        ]
        if with_actions:
            action = (
                ui.form(
                    "/console/devices/revoke",
                    ui.hidden("device_id", device.device_id) + ui.button("吊销", tone="quiet"),
                )
                if device.revoked_at is None
                else '<span class="faint">—</span>'
            )
            row.append(f'<div class="row-actions">{action}</div>')
        rows.append(row)

    headers = ["设备", "用途", "状态", "最后心跳"]
    if with_actions:
        headers.append("")
    return ui.table(headers, rows, empty="还没有配过设备")


def _clean_device_id(raw: object) -> str:
    """表单里那个设备名。**先剪掉不可打印字符再进日志。**

    它进的是一条参数化的 SQL(注入不了),但也进 `log.info` —— 而一个带换行的
    值能在日志里伪造出一整行看起来像是系统写的记录。
    """
    return "".join(c for c in str(raw or "") if c.isprintable()).strip()[:128]


def _kind_label(kind: str) -> str:
    return {"collector": "采集(只写)", "app_device": "查询(只读)"}.get(kind, kind)


def _purpose_label(purpose: str) -> str:
    return {"message": "消息", "transaction": "账单"}.get(purpose, purpose)


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _back(where: str) -> Response:
    """**改完之后 303 回列表,不是直接渲染。**

    直接渲染的话那一页的 URL 还是那个 POST —— 刷新一下就再执行一次,
    而这些动作里有吊销和删除。
    """
    return RedirectResponse(url=where, status_code=status.HTTP_303_SEE_OTHER)


def _expired() -> Response:
    """**过期的链接不是错误,是「再点一次」** —— 报错页会让人以为自己做错了什么。"""
    return _html(
        ui.bare_page(
            title="LifeIn",
            heading="这个链接过期了",
            body=ui.hint("这个链接过期了,或者已经用过。在 App 里重新点一次「在浏览器打开」就好。")
            + '<p class="hint"><a href="/console/privacy">先看看隐私说明</a></p>',
        )
    )


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
        httponly=True,  # 页面脚本读不到
        secure=True,  # 不走明文 —— 控制台必须在 TLS 后面
        samesite="strict",  # 别的站点点过来时不带上,同时是这一层的 CSRF 防线
        path="/console",  # 别的接口拿不到它
    )
    return response


def _html(markup: str) -> Response:
    return Response(content=markup, media_type="text/html; charset=utf-8")
