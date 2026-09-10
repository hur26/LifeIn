"""Web 控制台的**运营层**([ADR-029](../../docs/04-tech-decisions.md),2026-09-10)。

用户层在 [`console.py`](console.py),不登录。这一层要口令,而它**只做 App 上
没有、也不该有的事**:跨用户总览、用户开停、设备吊销、任务与配额、密钥轮换状态。

## 它替换掉的是 SSH

「哪台设备掉线了」「这个月模型花了多少」「哪个窗口一直在失败」「这个朋友的
账号体检过没有」 —— 这些原来全在 `python -m lifein.admin` 的三十多条子命令里,
而那三十多条的入口是 SSH。

**用一把能改一切的钥匙,去做每天都要做一次的只读动作** —— 这一层修的是这个。

所以真正的比较对象不是「零个认证面」,是 SSH:一个只读为主、能被单独吊销、
`Path` 限死在 `/admin` 的口令,比那把小。

## 有三样东西刻意留在终端里

- **注销一个用户**(`admin purge-user`)。它擦掉一个人的全部存在,
  而且不可撤销。**一个能擦掉一个人的按钮,该要求你先打开终端** ——
  那几秒钟正是这个动作应得的
- **导出别人的数据**(`admin export`)。本人在自己的控制台上点一下就有;
  运营者要替他导,那是一次需要解释的动作,不该做成一个随手能点的按钮
- **配凭据**(`set-imap`、`login-weixin`)。授权码一律从交互输入读 ——
  命令行参数会进 history、进 `ps`、进会话录制,而一个 HTTP 表单会进反代日志

**这张表是这一层的边界。** 往里加东西之前先问一句:它属于「每天要看一眼」,
还是属于「一年做一次而且做错了很贵」?后者留在终端里。

## 跨用户的读取都写审计日志

[09 §4](../../docs/09-privacy.md) 承诺的是「白杨在技术上读得到全部」,
而**读得到不等于读过之后没有痕迹**。那一行日志是这句承诺和「随便看」之间
唯一的区别。

它不是一道保障 —— 日志在白杨自己的机器上,他删得掉。它是一道**痕迹**:
「顺手看一眼」从此不再是零成本的。

## 铁律 1 一个字都没让

跨用户的数字是**一个个用户加起来的**,不是一条 `GROUP BY user_id` 的 SQL。
多跑几十次查询,换来的是这一层**没有任何一个函数能一次读到所有人的数据** ——
而那正是铁律 1 想挡住的形状。用户上百之后这里会慢,那时该做的是分页,
不是开一个跨用户的口子。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from fastapi.responses import RedirectResponse

from lifein.api import ui
from lifein.api.deps import NowDep, SessionDep, SettingsDep, form_values
from lifein.config import Settings
from lifein.console_auth import LoginGate, issue_session, verify_password, verify_session
from lifein.repos import (
    approvals,
    collector,
    console_links,
    credentials,
    data_control,
    job_runs,
    pending,
    quota,
    users,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin-console"])

COOKIE = "lifein_admin"
"""运营层的会话 cookie。**和用户层那张不是同一个名字,也不是同一个 `Path`** ——
一个 `Path=/admin`,一个 `Path=/console`,于是这两层在浏览器里从不互相看见。"""

STUCK_AFTER = timedelta(minutes=30)
"""审批卡在 `executing` 多久算卡住。**不自动重试也不自动标失败** ——
重试可能发第二条,标失败会让人以为没发出去。这个数唯一的用途是让人看见它。"""

FormDep = Annotated[dict, Depends(form_values)]

_GATE = LoginGate(max_attempts=5, lockout=timedelta(minutes=15))
"""失败闸门。**一个进程一个,不进库。**

它拦的是「拿着一本字典对着登录页跑」,而那种事发生在分钟级 ——
重启一次进程就重置对它没有帮助,因为重启是运维动作,不是攻击者能触发的。
真正的防线是 scrypt 那几十毫秒,闸门只是让那几十毫秒不必被付上一万次。
"""

_NAV = (
    ui.NavGroup(
        "运行",
        (
            ui.NavItem("overview", "总览", "/admin", "gauge"),
            ui.NavItem("ops", "运维", "/admin/ops", "clock"),
        ),
    ),
    ui.NavGroup("账号", (ui.NavItem("users", "用户", "/admin/users", "users"),)),
)


# ---------------------------------------------------------------- 进门


@router.get("/admin")
def admin_home(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """总览:**所有人加起来,现在有没有事。**

    这一页要在十秒内回答完「今天要不要管」。它上面每一个数字都对应一个动作,
    没有任何一个是「看着好看」的。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    rows = [_snapshot(uid, session, settings, now) for uid in users.list_all_users(session)]
    _audit("总览", count=len(rows))

    active = [r for r in rows if r.user.active]
    silent = sum(r.silent for r in rows)
    backlog = sum(r.pending for r in rows)
    spent = sum((r.spent for r in rows), Decimal(0))
    over = [r for r in rows if r.over_quota]
    stuck = sum(r.stuck for r in rows)

    cap = settings.monthly_cost_cap_cny if settings else 0.0
    body = ui.stats(
        [
            (
                "用户",
                f'{len(active)}<span class="unit">人</span>',
                f"另有 {len(rows) - len(active)} 人已停用"
                if len(rows) != len(active)
                else "都在用",
                "",
            ),
            (
                "掉线设备",
                f'{silent}<span class="unit">台</span>',
                "超过心跳时限没上报" if silent else "都在按时上报",
                "danger" if silent else "ok",
            ),
            (
                "待确认积压",
                f'{backlog}<span class="unit">条</span>',
                "他们在 App 里点头之前不会入账" if backlog else "都处理完了",
                "warn" if backlog else "",
            ),
            (
                "本月模型开销",
                f'<span class="unit">¥</span>{spent:,.2f}',
                f"每人上限 ¥{cap:,.2f}" if cap else "没设上限",
                "danger" if over else "",
            ),
        ]
    )

    if over:
        names = ui.esc("、".join(r.user.display_name for r in over))
        body += ui.banner(
            f"<strong>{names}</strong> 这个月的模型额度已经用完。"
            "他们的采集还在跑(采集几乎不花钱),停的是模型调用 —— "
            "原文都还在,加回额度之后重跑一遍就补上了。",
            tone="danger",
        )
    if stuck:
        body += ui.banner(
            f"有 <strong>{stuck}</strong> 条审批卡在「正在执行」。"
            "它的含义是「开始发了,但不知道发出去没有」 —— "
            "系统不会自动重试也不会自动标失败,去「运维」看一眼。",
            tone="warn",
        )

    body += ui.card(
        "每个人现在怎么样",
        ui.table(
            ["用户", "状态", "采集", "设备", "待确认", "本月开销", ""],
            [_overview_row(r) for r in rows],
            empty="库里一个用户都没有。跑 admin create-user 建一个",
        ),
        hint_text="这张表按<strong>运行状况</strong>排,不是按账号。"
        "要开停账号或者改标记,去「用户」。",
    )

    return _html(_page(session, active="overview", title="总览", heading="总览", body=body,
                       lede="所有人加起来,现在有没有事。"))


@router.post("/admin/login")
async def login(
    settings: SettingsDep, now: NowDep, payload: FormDep
) -> Response:
    """收下一次登录。**成功与失败之间,响应里唯一的差别是有没有那张 cookie。**

    错的口令、没配的运营层、闸门落下 —— 三种情况在页面上说的话不一样
    (它们对着的是运营者本人,而不是探测者),但**没有一种会告诉对方
    「这个口令差一点就对了」**。
    """
    blocked = _GATE.blocked(now)
    if blocked is not None:
        minutes = max(int(blocked.total_seconds() // 60) + 1, 1)
        return _login_page(settings, now, note=f"失败太多次了,{minutes} 分钟之后再试。")

    encoded = settings.console_admin_password_hash if settings else None
    if not encoded:
        return _login_page(settings, now)

    if not verify_password(str(payload.get("password", "")), encoded):
        _GATE.record_failure(now)
        log.warning("运营台登录失败")
        return _login_page(settings, now, note="口令不对。")

    _GATE.record_success()
    log.info("运营台登录成功")
    ttl = timedelta(hours=settings.console_admin_session_h)
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=COOKIE,
        value=issue_session(settings=settings, now=now, ttl=ttl),
        max_age=int(ttl.total_seconds()),
        httponly=True,
        secure=True,
        samesite="strict",
        path="/admin",
    )
    return response


@router.post("/admin/logout")
def logout() -> Response:
    """退出。**删 cookie 就够了** —— 会话是签出来的,库里没有对应的行,
    所以没有第二个地方需要清(`console_auth` 开头那段)。

    代价是一张已经被复制走的 cookie 在过期之前仍然有效。这是无状态会话的
    固有形状,而它的对价是「不存在一份看起来已登出、实际还能用的会话表」。
    """
    response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE, path="/admin")
    return response


@router.post("/admin/switch")
def switch_to_user_layer(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """切回普通用户版 —— **切到运营者自己的账号,不是别人的**。

    走的是用户层本来就有的那扇门:签一张一次性链接,跳过去,那边照常
    把 token 换成 cookie。**没有为这个按钮发明第二条认证路径** ——
    发明一条就多一处会出错的地方,而这一条已经被测试盯了很久。

    `users.is_admin` 那一列在这里被用到:它回答的正是「切到哪个账号」。
    一行都没标的时候这个按钮不显示,而直接 POST 过来会得到一句解释。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    admin = users.find_admin(session)
    if admin is None:
        return _html(
            _page(
                session,
                active="overview",
                title="切回用户版",
                heading="还没有哪一行标成运营者",
                body=ui.card(
                    "先标一行",
                    ui.hint(
                        "运营者自己也用这套东西,而这个按钮要知道该切到哪个账号。"
                        '在「用户」里点一下「标成运营者」,或者跑 <code class="mono">'
                        "admin set-admin --user &lt;uuid&gt;</code>。"
                        "<br><strong>这一列不是权限位</strong> —— 标了不会让谁登得进这里。"
                    ),
                    actions=ui.link_button("去用户列表", "/admin/users"),
                ),
            )
        )

    token, _link = console_links.issue(admin.id, session, now=now)
    log.info("运营台切回用户版:user=%s", admin.id)
    return RedirectResponse(url=f"/console?t={token}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------- 用户


@router.get("/admin/users")
def users_page(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """账号本身:谁是谁、开着还是停了、哪一行是运营者。

    **和总览那张表分开是有意的。** 一张按运行状况排的表回答「今天要不要管」,
    一张按账号排的表回答「这个人是谁」 —— 合成一张的话,两个问题都要
    在同一堆列里找答案。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    rows = []
    for user_id in users.list_all_users(session):
        user = users.get_user(user_id, session)
        rows.append(
            [
                f'<a href="/admin/users/{ui.esc(user.id)}">{ui.esc(user.display_name)}</a>'
                + (" " + ui.badge("运营者", tone="accent") if user.is_admin else ""),
                ui.mono(user.id),
                ui.badge("在用", tone="ok") if user.active else ui.badge("已停用", tone="neutral"),
                ui.when(user.created_at, empty="—"),
                '<div class="row-actions">' + _user_actions(user) + "</div>",
            ]
        )
    _audit("用户列表", count=len(rows))

    body = ui.card(
        "全部账号",
        ui.table(
            ["用户", "id", "状态", "建于", ""],
            rows,
            empty="库里一个用户都没有",
        ),
        hint_text="<strong>停用不删数据</strong> —— 历史事件仍要能追溯到人。"
        "停用之后定时任务不再给他跑,而<strong>已经签发的设备凭据不受影响</strong>:"
        "「先别再花钱扫他的邮箱」和「这台手机丢了」不是同一件事,要分开做。",
    )

    body += ui.card(
        "建号与注销都在终端里",
        ui.hint(
            '建一个人:<code class="mono">admin create-user --name … --wecom-userid …</code>,'
            '加 <code class="mono">--admin</code> 就顺便标成运营者。'
            '<br>彻底注销:<code class="mono">admin purge-user</code>。'
            "<strong>它擦掉一个人的全部存在,而且不可撤销</strong> —— "
            "一个能擦掉一个人的按钮,该要求你先打开终端,那几秒钟正是这个动作应得的。"
        ),
    )

    return _html(
        _page(session, active="users", title="用户", heading="用户", body=body,
              lede="谁是谁、开着还是停了。<strong>运行状况在「总览」。</strong>")
    )


@router.get("/admin/users/{user_id}")
def user_detail(
    user_id: str,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """一个人的全貌:设备、采集、配额、最近的任务与审批。

    **这一页是 `admin check-user` 的界面版。** 那条命令逐条对一遍
    「接一个朋友之前该确认什么」,而它的输出只在终端里存在几秒钟。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    user = users.get_user(user_id, session)
    if user is None:
        return _html(
            _page(session, active="users", title="用户", heading="没有这个用户",
                  body=ui.card("", ui.hint("这个 id 在库里找不到。"),
                               actions=ui.link_button("回到用户列表", "/admin/users")))
        )

    _audit("用户详情", user_id=user_id)
    snap = _snapshot(user_id, session, settings, now)
    stale_after = now - timedelta(minutes=settings.collector_heartbeat_timeout_m)
    beats = {b.device_id: b for b in collector.list_heartbeats(user_id, session)}

    body = ui.stats(
        [
            (
                "采集",
                "在采" if snap.collecting else "没在采",
                f"{snap.live} 条凭据 · {snap.rules} 条来源",
                "ok" if snap.collecting else "warn",
            ),
            (
                "待确认",
                f'{snap.pending}<span class="unit">条</span>',
                "他点头之前不会入账" if snap.pending else "都处理完了",
                "warn" if snap.pending else "",
            ),
            (
                "本月模型开销",
                f'<span class="unit">¥</span>{snap.spent:,.2f}',
                f"{snap.calls} 次调用",
                "danger" if snap.over_quota else "",
            ),
            (
                "掉线设备",
                f'{snap.silent}<span class="unit">台</span>',
                "看看手机上的后台限制" if snap.silent else "都在按时上报",
                "danger" if snap.silent else "",
            ),
        ]
    )

    device_rows = []
    for device in credentials.list_device_credentials(user_id, session):
        beat = beats.get(device.device_id)
        device_rows.append(
            [
                ui.mono(device.device_id or "(没有名字)"),
                {"collector": "采集(只写)", "app_device": "查询(只读)"}.get(
                    device.kind, device.kind
                ),
                ui.badge("已吊销", tone="neutral")
                if device.revoked_at
                else _beat_badge(beat, stale_after),
                ui.when(beat.last_seen_at if beat else None),
                f'{ui.esc(beat.app_version)} · Android {ui.esc(beat.android_version)}'
                if beat and beat.app_version
                else '<span class="faint">—</span>',
            ]
        )

    body += ui.card(
        "设备",
        ui.table(["设备", "用途", "状态", "最后心跳", "版本"], device_rows, empty="还没有配过设备"),
        actions=_revoke_form(user, device_rows),
        hint_text="**手机丢了**才吊销,而且吊销的是那台设备的两条凭据。"
        "本人自己也能在他的控制台上吊 —— 这里是给「他打不开控制台」那种情况准备的。",
    )

    rule_rows = [
        [
            ui.mono(rule.pattern),
            {"message": "消息", "transaction": "账单"}.get(rule.purpose, rule.purpose),
            ui.badge("放行中", tone="ok") if rule.enabled else ui.badge("已停用", tone="neutral"),
            rule.phase,
        ]
        for rule in collector.list_whitelist(user_id, session)
    ]
    body += ui.card(
        "放行的来源",
        ui.table(["匹配", "用途", "状态", "期"], rule_rows,
                 empty="一条都没有 —— 他的采集器送上去的东西全会被丢掉"),
        hint_text="<strong>这里只看,不改。</strong>白名单是他自己的东西,"
        "而运营者替他放行一个包名,等于替他决定采什么 —— 那要他自己点。",
    )

    body += ui.card(
        "最近的任务",
        ui.table(
            ["任务", "窗口", "状态", "开始", "错误"],
            [_run_row(run) for run in job_runs.recent(user_id, session, limit=12)],
            empty="还没跑过任何任务",
        ),
        hint_text="失败三次之后不再重跑。<strong>无限重试比放弃更糟</strong> —— "
        "一个永远失败的窗口会把后面每一天的补偿额度都吃掉。",
    )

    return _html(
        _page(
            session,
            active="users",
            title=f"{user.display_name} · 用户",
            heading=user.display_name,
            lede=f"{ui.mono(user.id)} · 时区 {ui.esc(user.tz)}",
            body=body,
            actions=ui.link_button("回到用户列表", "/admin/users"),
        )
    )


@router.post("/admin/users/{user_id}/disabled")
async def set_disabled(
    user_id: str,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    payload: FormDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """停用或恢复一个人。**停用要点两次,恢复不用。**

    不对称是有意的:停用会让他的定时任务当晚就不跑,而恢复只是把那件事
    还原回去 —— **要一个人对无害的动作再确认一次,只会训练他闭着眼点确认**。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    user = users.get_user(user_id, session)
    if user is None:
        return _back("/admin/users")

    wants_off = payload.get("disabled") == "1"
    if wants_off and payload.get("confirm") != "yes":
        return _html(
            _page(
                session,
                active="users",
                title="停用用户",
                heading=f"停用 {user.display_name}?",
                body=ui.card(
                    "定时任务当晚就不再给他跑",
                    ui.hint(
                        "<strong>数据一条都不会删</strong> —— 历史事件仍要能追溯到人。"
                        "<br><strong>设备凭据也不受影响</strong>:他的手机还在往上送东西,"
                        "只是没有任务去处理。要连采集一起停,得单独吊销他的采集凭据。"
                        "<br>随时能恢复,恢复不需要再确认。"
                    )
                    + ui.form(
                        f"/admin/users/{user.id}/disabled",
                        ui.hidden("disabled", "1")
                        + ui.hidden("confirm", "yes")
                        + ui.button("停用", tone="danger"),
                        cls="page-actions",
                    ),
                    actions=ui.link_button("算了", "/admin/users"),
                    tone="danger",
                ),
            )
        )

    users.set_disabled(user_id, session, disabled_at=now if wants_off else None)
    log.info("运营台%s了一个用户:user=%s", "停用" if wants_off else "恢复", user_id)
    return _back("/admin/users")


@router.post("/admin/users/{user_id}/operator")
async def set_operator(
    user_id: str,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    payload: FormDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """标记(或取消标记)运营者本人那一行。

    **这一列不是权限位。** 口令在环境变量里,和它没关系 —— 改这一行不会让谁
    登得进来,它只回答「那个切回普通用户版的按钮该切到哪个账号」(06 §2.10)。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    users.set_admin(user_id, session, is_admin=payload.get("operator") == "1")
    log.info("运营台改了运营者标记:user=%s -> %s", user_id, payload.get("operator"))
    return _back("/admin/users")


@router.post("/admin/users/{user_id}/devices/revoke")
async def revoke_device(
    user_id: str,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    payload: FormDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """替一个人吊销一台设备。**要点两次。**

    本人在自己的控制台上也能吊。这一条是给「他打不开控制台」那种情况准备的 ——
    最典型的就是手机本身丢了,而那部手机正是他打开控制台的地方。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    device_id = _clean_device_id(payload.get("device_id"))
    user = users.get_user(user_id, session)
    if user is None or not device_id:
        return _back(f"/admin/users/{user_id}")

    if payload.get("confirm") != "yes":
        return _html(
            _page(
                session,
                active="users",
                title="吊销设备",
                heading=f"吊销 {user.display_name} 的 {device_id}?",
                body=ui.card(
                    "那台手机从此既送不进来也读不出去",
                    ui.hint(
                        "两条凭据一起作废。<strong>已经采到的数据不受影响</strong>,"
                        "别的设备也不受影响。"
                        "<br>他换回这台手机时,重新出一张配码扫一次就行。"
                    )
                    + ui.form(
                        f"/admin/users/{user.id}/devices/revoke",
                        ui.hidden("device_id", device_id)
                        + ui.hidden("confirm", "yes")
                        + ui.button("吊销", tone="danger"),
                        cls="page-actions",
                    ),
                    actions=ui.link_button("算了", f"/admin/users/{user.id}"),
                    tone="danger",
                ),
            )
        )

    count = credentials.revoke_device(user_id, session, device_id=device_id)
    log.warning("运营台吊销了别人的设备:user=%s device=%s 条数=%s", user_id, device_id, count)
    return _back(f"/admin/users/{user_id}")


# ---------------------------------------------------------------- 运维


@router.get("/admin/ops")
def ops_page(
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
    lifein_admin: Annotated[str | None, Cookie()] = None,
) -> Response:
    """跨用户的运维面:失败的任务、卡住的审批、还停在旧主密钥上的凭据。

    **三样都是「不看就会静静烂掉」的那一类。** 一个永远失败的窗口不会报警
    (它只是每天少一天的数据),一条卡住的审批不会自己动,而轮换到一半的
    凭据在旧密钥被删掉之前一直都能用 —— 然后某天全都解不开。
    """
    if not _signed_in(settings, lifein_admin, now):
        return _login_page(settings, now)

    failed_rows: list[list[str]] = []
    stuck_rows: list[list[str]] = []
    stale_rows: list[list[str]] = []
    for user_id in users.list_all_users(session):
        user = users.get_user(user_id, session)
        name = ui.esc(user.display_name)
        for run in job_runs.recent(user_id, session, limit=30):
            if run.failed:
                failed_rows.append([name, *_run_row(run)[:-1], ui.esc((run.error or "")[:120])])
        for item in approvals.list_stuck(user_id, session, cutoff=now - STUCK_AFTER):
            stuck_rows.append(
                [name, ui.esc(item.agent), ui.mono(item.tool_name), ui.when(item.created_at)]
            )
        for cred_id, kind, version in credentials.stale_key_versions(
            user_id, session, settings=settings
        ):
            stale_rows.append([name, ui.mono(kind), str(version), ui.mono(cred_id[:8])])

    _audit("运维页", failed=len(failed_rows), stuck=len(stuck_rows), stale=len(stale_rows))

    body = ui.stats(
        [
            (
                "失败的窗口",
                f'{len(failed_rows)}<span class="unit">个</span>',
                "失败三次之后就不再重跑了",
                "danger" if failed_rows else "ok",
            ),
            (
                "卡住的审批",
                f'{len(stuck_rows)}<span class="unit">条</span>',
                "开始发了,不知道发出去没有",
                "warn" if stuck_rows else "ok",
            ),
            (
                "旧密钥残留",
                f'{len(stale_rows)}<span class="unit">条</span>',
                f"当前是第 {settings.master_key_version} 版",
                "warn" if stale_rows else "ok",
            ),
        ]
    )

    body += ui.card(
        "失败的任务窗口",
        ui.table(
            ["用户", "任务", "窗口", "状态", "开始", "错误"],
            failed_rows,
            empty="最近没有失败的窗口",
        ),
        hint_text="<strong>丢的是那一整天的事件</strong> —— 记账、日程、记忆一起,"
        "而它不报错:日志里只有一行早已被滚掉的 WARNING,账本上只是"
        "「这天没花钱」。重跑跑 <code class=\"mono\">admin digest</code>。",
    )

    body += ui.card(
        "卡在「正在执行」的审批",
        ui.table(["用户", "agent", "工具", "提交于"], stuck_rows, empty="没有卡住的"),
        hint_text="<strong>不自动重试,也不自动标失败。</strong>"
        "重试可能发第二条,标失败会让人以为没发出去 —— "
        "那是唯一不能替用户猜的情况,所以它只出现在这里等你看一眼。",
    )

    body += ui.card(
        "还停在旧主密钥上的凭据",
        ui.table(["用户", "kind", "版本", "凭据"], stale_rows, empty="全都在当前版本上"),
        hint_text="轮换的收尾是「确认无残留旧版本」(07 §2.2)。"
        "<strong>这一栏不空就不能删 <code class=\"mono\">MASTER_KEY_PREVIOUS</code></strong> —— "
        "删了那几条就再也解不开了。收尾跑 <code class=\"mono\">admin rotate-keys</code>。",
    )

    return _html(
        _page(session, active="ops", title="运维", heading="运维", body=body,
              lede="三样<strong>不看就会静静烂掉</strong>的东西。")
    )


# ---------------------------------------------------------------- 内部


class _Snapshot:
    """一个用户此刻的样子。**只在这一层用,所以不放进 repos。**"""

    __slots__ = ("user", "live", "silent", "rules", "collecting", "pending", "spent",
                 "calls", "over_quota", "stuck")

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _snapshot(user_id: str, session, settings: Settings, now: datetime) -> _Snapshot:
    """把一个人的现状凑齐。**一个用户一次,不跨用户查**(见模块开头)。"""
    user = users.get_user(user_id, session)
    devices = credentials.list_device_credentials(user_id, session)
    stale_after = now - timedelta(minutes=settings.collector_heartbeat_timeout_m)
    beats = collector.list_heartbeats(user_id, session)
    state = data_control.state(user_id, session)
    cap = Decimal(str(settings.monthly_cost_cap_cny)) if settings.monthly_cost_cap_cny else None
    usage = quota.usage(user_id, session, now=now, cap=cap)

    return _Snapshot(
        user=user,
        live=len([d for d in devices if d.revoked_at is None]),
        silent=len([b for b in beats if b.is_stale(cutoff=stale_after)]),
        rules=state.enabled_rules,
        collecting=state.enabled,
        pending=pending.count_pending(user_id, session, now=now),
        spent=usage.spent,
        calls=usage.calls,
        over_quota=usage.over,
        stuck=len(approvals.list_stuck(user_id, session, cutoff=now - STUCK_AFTER)),
    )


def _overview_row(snap: _Snapshot) -> list[str]:
    return [
        f'<a href="/admin/users/{ui.esc(snap.user.id)}">{ui.esc(snap.user.display_name)}</a>'
        + (" " + ui.badge("运营者", tone="accent") if snap.user.is_admin else ""),
        ui.badge("在用", tone="ok") if snap.user.active else ui.badge("已停用", tone="neutral"),
        ui.badge("在采", tone="ok") if snap.collecting else ui.badge("没在采", tone="warn"),
        f'<span class="num">{snap.live}</span>'
        + (" " + ui.badge(f"掉线 {snap.silent}", tone="danger") if snap.silent else ""),
        f'<span class="num">{snap.pending}</span>',
        f'<span class="num">¥{snap.spent:,.2f}</span>'
        + (" " + ui.badge("超额", tone="danger") if snap.over_quota else ""),
        '<div class="row-actions">'
        + ui.link_button("详情", f"/admin/users/{snap.user.id}", tone="quiet")
        + "</div>",
    ]


def _user_actions(user) -> str:
    out = ui.form(
        f"/admin/users/{user.id}/disabled",
        ui.hidden("disabled", "0" if not user.active else "1")
        + ui.button("恢复" if not user.active else "停用", tone="quiet"),
    )
    out += ui.form(
        f"/admin/users/{user.id}/operator",
        ui.hidden("operator", "0" if user.is_admin else "1")
        + ui.button("取消运营者" if user.is_admin else "标成运营者", tone="quiet"),
    )
    return out


def _revoke_form(user, device_rows: list) -> str:
    """详情页上那个吊销表单。**没有设备时不显示** —— 一个点了什么都不会发生的
    输入框,只会让人怀疑自己填错了。"""
    if not device_rows:
        return ""
    return ui.form(
        f"/admin/users/{user.id}/devices/revoke",
        ui.text_field("device_id", "吊销哪一台", placeholder="pixel-7a")
        + ui.button("吊销", tone="danger"),
        cls="inline-form",
    )


def _clean_device_id(raw: object) -> str:
    """表单里那个设备名。**先剪掉换行再进日志。**

    它最后会进一条参数化的 SQL(注入不了),但也会进 `log.warning` ——
    而一个带换行的值能在日志里伪造出一整行看起来像是系统写的记录。
    审计日志是这一层唯一的痕迹(模块开头那段),**能被伪造的痕迹不如没有**。
    """
    return "".join(c for c in str(raw or "") if c.isprintable()).strip()[:128]


def _beat_badge(beat, stale_after: datetime) -> str:
    if beat is None:
        return ui.badge("还没上报过", tone="warn")
    if beat.is_stale(cutoff=stale_after):
        return ui.badge("掉线", tone="danger")
    return ui.badge("正常", tone="ok")


def _run_row(run) -> list[str]:
    tone = {"succeeded": "ok", "failed": "danger", "running": "warn"}.get(run.status, "neutral")
    label = {"succeeded": "成了", "failed": "失败", "running": "在跑"}.get(run.status, run.status)
    window = f"{str(run.window_start)[:10]} → {str(run.window_end)[:10]}"
    return [
        ui.mono(run.job_name),
        f'<span class="faint">{ui.esc(window)}</span>',
        ui.badge(f"{label} · {run.attempts} 次" if run.attempts else label, tone=tone),
        ui.when(run.started_at),
        ui.esc((run.error or "")[:80]) or '<span class="faint">—</span>',
    ]


def _signed_in(settings: Settings, cookie: str | None, now: datetime) -> bool:
    """**没配口令时一律进不去。**

    `CONSOLE_ADMIN_PASSWORD_HASH` 留空是一个正经的部署选择(07 §2.8),
    而它的含义必须是「这一层不存在」,不能是「这一层不设防」。
    """
    if not settings or not settings.console_admin_password_hash:
        return False
    return verify_session(cookie, settings=settings, now=now)


def _login_page(settings: Settings, now: datetime, *, note: str = "") -> Response:
    """登录页。**没有导航,也没有用户名。**

    没导航:一份列着「用户」「运维」的侧边栏,对着一个还没进门的人,
    等于把这台机器上有什么先说了一遍。

    没用户名:运营者只有一个,一个用户名不增加任何保护 ——
    它只增加一格要填的东西(ADR-029 那张否决表)。
    """
    if not settings or not settings.console_admin_password_hash:
        return _html(
            ui.bare_page(
                title="运营台 · LifeIn",
                heading="这台机器上没有运营台",
                body=ui.hint(
                    "服务端没配 <code class=\"mono\">CONSOLE_ADMIN_PASSWORD_HASH</code>,"
                    "所以这一层整个不存在。"
                    "<br><strong>不存在的认证面是攻不破的</strong> —— "
                    "一个人自己用的时候留空就是对的。"
                )
                + ui.hint(
                    '要开:在服务器上跑 <code class="mono">python -m lifein.admin '
                    "console-password</code>,把它打出来的那一行放进 <code class=\"mono\">"
                    ".env</code>,然后重启。"
                )
                + '<p class="hint"><a href="/console">回到用户版</a></p>',
            )
        )

    blocked = _GATE.blocked(now)
    if blocked is not None and not note:
        minutes = max(int(blocked.total_seconds() // 60) + 1, 1)
        note = f"失败太多次了,{minutes} 分钟之后再试。"

    body = ""
    if note:
        body += ui.banner(ui.esc(note), tone="warn")
    body += ui.form(
        "/admin/login",
        ui.text_field("password", "口令", kind="password")
        + '<button class="btn btn-primary btn-block" type="submit">进去</button>',
    )
    body += ui.hint(
        "这一层管的是<strong>所有人</strong>的运行状况。"
        "普通用户不需要登录 —— 从 App 里点「在浏览器打开」就进得去自己那一份。"
    )
    body += '<p class="hint"><a href="/console/privacy">隐私说明</a></p>'
    return _html(ui.bare_page(title="运营台 · LifeIn", heading="运营台", body=body))


def _page(
    session,
    *,
    active: str,
    title: str,
    heading: str,
    body: str,
    lede: str = "",
    actions: str = "",
) -> str:
    """运营层的页面壳。右上角永远有「切回用户版」和「退出」。

    **「切回用户版」不是一个链接,是一个表单。** 它会签发一张一次性链接,
    而签发是一个会改变库的动作 —— GET 会被浏览器预取,而预取一次就多一张
    有效的链接躺在那儿。
    """
    has_admin_row = users.find_admin(session) is not None
    identity = ui.Identity(
        name="运营者",
        layer="运营",
        layer_kind="admin",
        switch_label="切回用户版" if has_admin_row else None,
        switch_href=None,
        sign_out_href="/admin/logout",
    )
    switch = (
        ui.form("/admin/switch", ui.button("切回用户版", tone="ghost", icon_name="back"))
        if has_admin_row
        else ""
    )
    markup = ui.page(
        title=f"{title} · 运营台",
        heading=heading,
        lede=lede,
        body=body,
        nav=_NAV,
        active=active,
        identity=identity,
        actions=actions,
    )
    # 切换那个按钮要在顶栏里,而它是一个表单 —— `ui.Identity` 只放得下链接
    slot = '<div class="topbar-actions">'
    return markup.replace(slot, slot + switch, 1)


def _audit(what: str, **fields) -> None:
    """一次跨用户读取。**这一行是 09 §4 那句承诺和「随便看」之间唯一的区别。**

    只记「看了什么、看了谁」,不记内容 —— 记内容的话这条日志本身
    就变成了第二份数据集中点(AGENTS.md §3 那条「审计日志记入参摘要,不记原文」)。
    """
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    log.info("运营台读取:%s %s", what, detail)


def _back(where: str) -> Response:
    return RedirectResponse(url=where, status_code=status.HTTP_303_SEE_OTHER)


def _html(markup) -> Response:
    if isinstance(markup, Response):
        return markup
    return Response(content=markup, media_type="text/html; charset=utf-8")
