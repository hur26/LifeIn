"""运维命令:`python -m lifein.admin <子命令>`。

存在的理由很直接:凭据只能通过 `credentials` 表进系统(加密在仓储层),
而 P0 没有 Web 界面 —— 没有这个 CLI,部署完的系统根本配不起来。

**授权码一律从交互输入读,不走命令行参数。** 命令行参数会进 shell history、
会出现在 `ps` 的输出里、会被跳板机的会话录制录下来。这三处都不是你能事后
清干净的地方。

    python -m lifein.admin create-user --name 白杨 --wecom-userid BaiYang
    python -m lifein.admin set-imap --user <uuid> --host imap.163.com --username me@163.com
    python -m lifein.admin login-weixin --user <uuid>    # 扫码连微信(ADR-018)
    python -m lifein.admin test-push --user <uuid>       # 真发一条，看走的哪个通道
    python -m lifein.admin test-alert --user <uuid>      # 真发一条告警(07 §6 要求实测)
    python -m lifein.admin test-imap --user <uuid>       # 07 §6 那条"IMAP 实测能登录"
    python -m lifein.admin key-status --user <uuid>      # 轮换收尾用
    python -m lifein.admin rotate-keys --user <uuid>
    python -m lifein.admin issue-device --user <uuid>   # 设备名不给就自动生成
    python -m lifein.admin revoke-device --user <uuid> --device-id pixel-7a  # 手机丢了
    python -m lifein.admin list-devices --user <uuid>
    python -m lifein.admin allow-source --user <uuid> --package com.tencent.mm
    python -m lifein.admin list-sources --user <uuid>   # 白名单 + 采集器心跳
    python -m lifein.admin rules --user <uuid> --detail # 影子期数据,判断误报率
    python -m lifein.admin rule-mode --user <uuid> --rule upcoming_schedule --mode active
    python -m lifein.admin import-statement --user <uuid> --file 账单.pdf --issuer cmb
    python -m lifein.admin budget --user <uuid> --amount 5000            # 总预算
    python -m lifein.admin budget --user <uuid> --category 餐饮 --amount 1500
    python -m lifein.admin budgets --user <uuid>                         # 看进度
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import secrets as secrets_module
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from lifein.agents.digest import MAX_EVENTS
from lifein.channels.base import Card
from lifein.channels.weixin import BASE_URL as WEIXIN_BASE_URL
from lifein.config import get_settings
from lifein.crypto import new_shared_secret
from lifein.db import session_scope
from lifein.repos import channel_state, collector, credentials, users
from lifein.sources.imap_client import ImapConfig, ImapError, ImapMailbox

log = logging.getLogger("lifein.admin")


def cmd_create_user(args: argparse.Namespace) -> int:
    with session_scope() as session:
        user_id = users.create_user(
            session, display_name=args.name, wecom_userid=args.wecom_userid, tz=args.tz
        )
    print(f"已创建用户 {user_id}")
    print("下一步:set-imap 配邮箱凭据,然后 test-imap 实测")
    return 0


def cmd_list_users(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        for user_id in users.list_active_users(session):
            user = users.get_user(user_id, session)
            print(f"{user.id}  {user.display_name}  wecom={user.wecom_userid}  tz={user.tz}")
    return 0


def cmd_set_imap(args: argparse.Namespace) -> int:
    # 从交互读:命令行参数会进 history、进 ps、进会话录制
    auth_code = getpass.getpass("IMAP 授权码(不是登录密码,输入不回显):")
    if not auth_code.strip():
        print("授权码为空,没有改动", file=sys.stderr)
        return 1

    settings = get_settings()
    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1
        # 先吊销旧的:同一类凭据留着两条有效的,读到哪条取决于排序,
        # 那是将来某天"改了密码怎么还是旧的"的根源
        credentials.revoke_credential(args.user, session, kind="imap")
        credentials.put_credential(
            args.user,
            session,
            kind="imap",
            scope="query",
            payload={
                "host": args.host,
                "username": args.username,
                "auth_code": auth_code,
                "port": args.port,
            },
            settings=settings,
        )
    print("已加密写入。建议立刻跑 test-imap 确认能登录")
    return 0


def cmd_login_weixin(args: argparse.Namespace) -> int:
    """扫码连微信。

    **给 LifeIn 单独扫一个 bot。** 如果这个微信账号上还跑着别的 iLink 客户端
    (比如另一个 agent),两边长轮询会互相抢消息。

    扫完只拿到 token,**还不知道该把摘要推给谁** —— 推送目标要等你给 bot
    发第一条消息才知道。所以最后会等你发一句话。
    """
    import httpx

    from lifein.channels import weixin_inbound, weixin_login

    settings = get_settings()
    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1

    qr_path = Path("weixin-qr.html").resolve()

    def show(qr: weixin_login.QrCode) -> None:
        # 先落地成文件。终端字符画依赖控制台编码,而文件不依赖任何终端能力 ——
        # 中文 Windows 上这是唯一稳的路
        try:
            weixin_login.write_qr_html(qr, qr_path)
            print("\n二维码已生成,用浏览器打开它扫:")
            print(f"  {qr_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(生成二维码文件失败:{exc})")
        print("\n或者直接扫这个链接:")
        print(f"  {qr.url or qr.value}")

        ascii_art = weixin_login.render_qr_ascii(qr)
        if not ascii_art:
            return
        try:
            print()
            print(ascii_art)
        except UnicodeEncodeError:
            # 控制台是 GBK,编不出方块字符。不影响 —— 上面的文件照样能扫
            print("(终端编码画不出字符版二维码,用上面的文件)")

    client = httpx.Client(timeout=40.0)
    try:
        result = weixin_login.login(
            client,
            show_qr=show,
            on_scanned=lambda: print("已扫码,请在手机上点确认…"),
            timeout_s=args.timeout,
        )
    except weixin_login.LoginFailed as exc:
        print(f"登录失败:{exc}", file=sys.stderr)
        return 1

    print(f"\n连接成功,bot id = {result.account_id}")
    print("现在在微信里给这个 bot 发一句话(随便什么),用来确定推送目标…")

    poller = weixin_inbound.WeixinPoller(client=client)
    sync_buf = ""
    deadline = time.monotonic() + args.timeout
    peer: str | None = None
    context_token: str | None = None

    while peer is None and time.monotonic() < deadline:
        try:
            polled = poller.poll_once(
                base_url=result.base_url, token=result.token, sync_buf=sync_buf
            )
        except Exception as exc:  # noqa: BLE001
            print(f"等待消息时出错:{exc}", file=sys.stderr)
            return 1
        sync_buf = polled.sync_buf
        for message in polled.messages:
            peer = message.sender
            context_token = message.channel_ref
            break

    if peer is None:
        print("没等到消息。会话已建立,稍后可以用 set-weixin 手工补上推送目标", file=sys.stderr)
        return 1

    with session_scope() as session:
        credentials.revoke_credential(args.user, session, kind="weixin")
        payload = {
            "token": result.token,
            "to_user_id": peer,
            "base_url": result.base_url,
            "account_id": result.account_id,
        }
        if context_token:
            payload["context_token"] = context_token
        credentials.put_credential(
            args.user,
            session,
            kind="weixin",
            scope="query",
            payload=payload,
            settings=settings,
        )
        # 会话是新的,旧游标对它没有意义
        channel_state.clear_state(
            args.user,
            session,
            channel=weixin_inbound.CHANNEL,
            key=weixin_inbound.SYNC_BUF_KEY,
        )

    print(f"已加密写入,推送目标 = {peer}")
    print("跑 test-push 确认能发到微信")
    return 0


def cmd_set_weixin(args: argparse.Namespace) -> int:
    """配微信 iLink 推送会话(ADR-018)。

    P0 不做扫码登录 —— 用已有客户端登录后拿到的 token 导进来。
    token 和授权码一样从交互输入读,不走命令行参数。
    """
    token = getpass.getpass("iLink token(输入不回显):")
    if not token.strip():
        print("token 为空,没有改动", file=sys.stderr)
        return 1

    settings = get_settings()
    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1
        credentials.revoke_credential(args.user, session, kind="weixin")
        payload = {
            "token": token,
            "to_user_id": args.to,
            "base_url": args.base_url,
        }
        if args.context_token:
            payload["context_token"] = args.context_token
        credentials.put_credential(
            args.user,
            session,
            kind="weixin",
            scope="query",
            payload=payload,
            settings=settings,
        )
    print("已加密写入。跑 test-push 确认能发到微信")
    return 0


def cmd_test_push(args: argparse.Namespace) -> int:
    """真发一条测试消息,确认通道能用。

    走的是完整的降级链路,所以它同时回答两个问题:能不能发出去,
    以及**是从哪个通道发出去的** —— 后者更重要,微信没配好会静默落到企微。
    """
    from lifein.bootstrap import build_services

    services = build_services()
    card = Card(
        title="LifeIn 测试消息",
        summary="看到这条说明推送通道是通的。",
        footer="来自 admin test-push",
    )
    try:
        delivery = services.channel.send(args.user, card)
    except Exception as exc:  # noqa: BLE001
        print(f"全部通道都失败:{exc}", file=sys.stderr)
        return 1

    print(f"发送成功,走的是 {delivery.channel} 通道")
    if delivery.channel != "weixin":
        print("注意:没走微信 —— 微信会话没配或已过期,上面的告警日志里有原因")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """一次性往回补一段时间的邮件。

    **只入库,不推送。** 刚开始用的时候手上没有任何历史,而"最近有什么安排"
    这种问题需要素材。补完之后 digest 命令可以在这批素材上出一份总览。

    补跑是幂等的:去重靠 (user_id, source, external_id),多跑几次不会重复。
    """
    from datetime import UTC, datetime, timedelta

    from lifein.bootstrap import build_adapters, build_services
    from lifein.repos import raw_events

    services = build_services()
    since = datetime.now(UTC) - timedelta(days=args.days)
    print(f"回溯 {args.days} 天(自 {since.date()})…")

    total_inserted = total_failed = 0
    with session_scope() as session:
        adapters = build_adapters(args.user, session, services)
        if not adapters:
            print("没有可用的数据源,先配 IMAP", file=sys.stderr)
            return 1
        for adapter in adapters:
            try:
                events = list(adapter.fetch(since))
            except Exception as exc:  # noqa: BLE001
                print(f"  {adapter.source}: 采集失败 {exc}", file=sys.stderr)
                continue
            result = raw_events.insert_events(args.user, session, events)
            line = (
                f"  {adapter.source}: 取到 {len(events)}，新增 {result.inserted}，"
                f"已有 {result.duplicates}，解析失败 {result.failed}"
            )
            if args.reparse and result.duplicates:
                # 解析器改过之后,已有的那些要用新结果覆盖 —— 否则修了跟没修一样
                updated = raw_events.reparse_events(args.user, session, events)
                line += f"，重新解析 {updated}"
            print(line)
            total_inserted += result.inserted
            total_failed += result.failed

    print(f"\n共新增 {total_inserted} 条")
    if total_failed:
        print(f"其中 {total_failed} 条没解析出来,查 raw_events.normalize_error")
    print(f"下一步:digest --user {args.user} --days {args.days} 看一份总览")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """在指定窗口上生成一份摘要。

    默认只打印不推送 —— 想看看效果、调 prompt 的时候不该往微信里发东西。
    加 --push 才真发。
    """
    from datetime import UTC, datetime, timedelta

    from lifein.agents.digest import DigestFailed, DigestInput, run_digest, to_card
    from lifein.bootstrap import build_services
    from lifein.channels.weixin import render_text
    from lifein.repos import raw_events

    services = build_services()
    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)

    with session_scope() as session:
        events = raw_events.fetch_normalized_between(
            args.user, session, start=since, end=now, limit=args.limit
        )

    if not events:
        print(f"最近 {args.days} 天没有事件,先跑 backfill", file=sys.stderr)
        return 1

    cap = args.max_events
    print(f"窗口内 {len(events)} 条事件", end="")
    if len(events) > cap:
        # 说出来。悄悄截断会让人以为模型漏了东西
        print(f",按时间倒序只送最新的 {cap} 条给模型")
    else:
        print()

    try:
        result = run_digest(
            DigestInput(day=now.date(), events=events),
            llm=services.llm,
            max_events=cap,
        )
    except DigestFailed as exc:
        print(f"生成失败:{exc}", file=sys.stderr)
        return 1

    out = result.output
    card = to_card(out, now.date())
    print("\n" + "=" * 52)
    print(render_text(card)[0])
    print("=" * 52)
    print(
        f"\n条目 {len(out.items)} | 丢弃无法溯源 {out.dropped_hallucinated} | "
        f"没有引用 {sum(1 for i in out.items if i.unverified)} | "
        f"token {result.prompt_tokens}/{result.completion_tokens}"
    )

    if args.push:
        delivery = services.channel.send(args.user, card)
        print(f"已推送,走的是 {delivery.channel} 通道")
    return 0


def cmd_test_imap(args: argparse.Namespace) -> int:
    """真连一次。163 的 ID 握手对不对,只有这一步能证明。"""
    settings = get_settings()
    with session_scope() as session:
        creds = credentials.get_credential(args.user, session, kind="imap", settings=settings)

    if not creds:
        print("这个用户还没有 IMAP 凭据", file=sys.stderr)
        return 1

    mailbox = ImapMailbox(
        ImapConfig(
            host=creds["host"],
            username=creds["username"],
            auth_code=creds["auth_code"],
            port=int(creds.get("port", 993)),
        )
    )
    since = datetime.now(UTC) - timedelta(days=args.days)
    try:
        count = sum(1 for _ in mailbox.fetch_raw_since(since))
    except ImapError as exc:
        print(f"失败:{exc}", file=sys.stderr)
        return 1

    print(f"登录成功,最近 {args.days} 天取到 {count} 封")
    return 0


def cmd_key_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    with session_scope() as session:
        stale = credentials.stale_key_versions(args.user, session, settings=settings)

    if not stale:
        print(f"没有残留旧版本,当前主密钥版本 {settings.master_key_version}")
        return 0

    print(f"有 {len(stale)} 条凭据还停在旧密钥上:")
    for cred_id, kind, version in stale:
        print(f"  {cred_id}  kind={kind}  key_version={version}")
    print("跑 rotate-keys 重新加密。全部处理完之前不要删 MASTER_KEY_PREVIOUS")
    return 1  # 非零:轮换没做完是个待办状态,方便脚本判断


def cmd_rotate_keys(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.master_key_previous is None:
        print("没有配 MASTER_KEY_PREVIOUS,旧密文解不开", file=sys.stderr)
        return 1

    with session_scope() as session:
        count = credentials.rotate_credentials(args.user, session, settings=settings)
    print(f"已重新加密 {count} 条。跑 key-status 确认无残留后再删 MASTER_KEY_PREVIOUS")
    return 0


def cmd_issue_device(args: argparse.Namespace) -> int:
    """给一台手机签发设备密钥(06 §6.1)。

    **采集与查询是两条独立的行、两把独立的密钥**,这是铁律 12。
    `--purpose all` 只是省一次来回,不是把它们合成一条 ——
    绝不写 `scope=both`:那等于把"采集端只能写"这条作废。

    密钥**只在这里显示一次**。丢了就重新签发,旧的同时作废 ——
    库里存的是密文,服务端自己也读不出来给你看第二遍(那正是加密的意义)。
    """
    from lifein import qr as qr_render

    device_id = args.device_id or _default_device_id()
    purposes = (
        [("collector", "ingest"), ("app_device", "query")]
        if args.purpose == "all"
        else [("collector", "ingest")]
        if args.purpose == "collect"
        else [("app_device", "query")]
    )

    settings = get_settings()
    provisioning: dict[str, str | int] = {
        "v": 1,
        "base_url": args.base_url,
        "user_id": args.user,
        "device_id": device_id,
    }

    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1

        for kind, scope in purposes:
            # 先吊销这台设备同类的旧凭据:留着两把有效的,验签用哪把取决于
            # 排序,而"换了密钥但旧的还能用"是最难发现的一类问题
            credentials.revoke_device(args.user, session, device_id=device_id, kind=kind)
            secret = new_shared_secret()
            credentials.put_credential(
                args.user,
                session,
                kind=kind,
                scope=scope,
                payload={"secret": secret},
                settings=settings,
                device_id=device_id,
            )
            provisioning["collector_secret" if scope == "ingest" else "query_secret"] = secret

    blob = json.dumps(provisioning, ensure_ascii=False, separators=(",", ":"))
    print(f"\n已签发 {len(purposes)} 条设备凭据,device_id={device_id}")
    print("下面这串只显示这一次,配进 App 之后就把窗口关了:\n")
    print(blob)

    qr_path = Path(f"device-{device_id}.html").resolve()
    try:
        qr_render.write_qr_html(
            blob,
            qr_path,
            title=f"配置 LifeIn 采集端 · {device_id}",
            hint="在 App 的扫码配置页里扫它。密钥只显示这一次",
        )
        print(f"\n二维码:{qr_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(生成二维码文件失败,手抄上面那串也一样:{exc})")

    ascii_art = qr_render.render_qr_ascii(blob)
    if ascii_art:
        try:
            print()
            print(ascii_art)
        except UnicodeEncodeError:
            # 控制台是 GBK,画不出方块字符。上面的文件照样能扫
            print("(终端编码画不出字符版二维码,用上面的文件)")
    return 0


def _default_device_id() -> str:
    """没给名字就生成一个。

    **人不该被迫现编一个设备名。** 它只在两个地方露面:吊销时你要指认哪一台,
    以及状态页上那一行 —— 两处都只要求"能认出来",不要求好听。

    P4 会把这件事整个挪走:那时 `device_id` 由 App 自己生成并在配码时上报
    (03 的 P4 范围),因为朋友连服务器都不会碰,更不会给设备起名。
    """
    return "phone-" + secrets_module.token_hex(2)


def cmd_revoke_device(args: argparse.Namespace) -> int:
    """手机丢了。**默认把这台设备的全部凭据一起吊销** —— 只停一种等于没停。"""
    with session_scope() as session:
        count = credentials.revoke_device(
            args.user, session, device_id=args.device_id, kind=args.kind
        )
    print(f"已吊销 {count} 条凭据。那台设备手上的 token 下一次请求就失效")
    return 0


def cmd_list_devices(args: argparse.Namespace) -> int:
    with session_scope() as session:
        rows = credentials.list_device_credentials(args.user, session)

    if not rows:
        print("还没给任何设备签发过凭据。跑 issue-device")
        return 0

    for row in rows:
        state = "有效" if row.active else f"已吊销 {row.revoked_at:%Y-%m-%d %H:%M}"
        print(f"{row.device_id:<20} {row.kind:<12} scope={row.scope:<7} {state}")
    return 0


def cmd_allow_source(args: argparse.Namespace) -> int:
    """给采集白名单加一条来源(07 §4)。

    **默认拒绝**,所以新装的采集器在这条跑之前一个字都送不进来。
    P1 只该放行微信;银行与支付类是 P2 的事,提前加了也会被 purpose 闸门挡住。
    """
    match_type = collector.MATCH_PACKAGE if args.package else collector.MATCH_SMS_SENDER
    pattern = args.package or args.sms_sender

    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1
        rule = collector.add_whitelist(
            args.user,
            session,
            match_type=match_type,
            pattern=pattern,
            purpose=args.purpose,
            phase=args.phase,
        )
    print(f"已放行 {rule.match_type}={rule.pattern}(purpose={rule.purpose}, {rule.phase})")
    if rule.purpose == collector.PURPOSE_TRANSACTION:
        print("注意:P1 不放行 transaction,这条要等记账链路打开才生效")
    return 0


def cmd_list_sources(args: argparse.Namespace) -> int:
    with session_scope() as session:
        rules = collector.list_whitelist(args.user, session)
        beats = collector.list_heartbeats(args.user, session)

    if not rules:
        print("白名单是空的 —— 采集器送上来的东西一条都不会入库")
    for rule in rules:
        state = "启用" if rule.enabled else "停用"
        print(f"#{rule.id:<4} {rule.match_type:<13} {rule.pattern:<28} {rule.purpose:<12} {state}")

    print()
    if not beats:
        print("还没有任何设备上报过心跳")
    for beat in beats:
        listener = "监听正常" if beat.listener_enabled else "监听权限被关"
        print(f"{beat.device_id:<20} 最后心跳 {beat.last_seen_at:%Y-%m-%d %H:%M}  {listener}")
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    """真发一条告警,验证那条路走得通(07 §6 那条"告警通道实测能收到")。

    **这条比 `test-push` 更要紧。** 推送发不出去你当天就会发现;
    告警发不出去,你是在采集器已经掉线两周之后才发现 ——
    而那正是这个系统最危险的失效方式(R8 / R9)。

    走的是和真实告警完全相同的路径:`Alerter` → 邮件通道 → SMTP,
    不是另写一段发信代码。**另写一段就只能证明那段代码能跑。**
    """
    from lifein.bootstrap import build_services

    services = build_services()
    channel = _email_channel_of(services)
    if channel is None:
        print("没有邮件通道:既没配 SMTP_*,也没有能派生出发信主机的邮箱凭据", file=sys.stderr)
        print("先跑 set-imap(QQ/163/126 的授权码 IMAP 与 SMTP 通用),或配 SMTP_*", file=sys.stderr)
        return 1

    print(f"发信:{channel._from} → {channel._resolve(args.user)}")

    # 告警器**按设计吞掉发送异常**(不许拖垮调用方),所以这里挂个耳朵去听 ——
    # 否则这条命令只能靠"我没看见报错"来判断成功,那和没验一样
    failures: list[logging.LogRecord] = []
    listener = logging.Handler()
    listener.setLevel(logging.ERROR)
    listener.emit = lambda record: (  # type: ignore[method-assign]
        failures.append(record) if record.exc_info else None
    )
    alert_log = logging.getLogger("lifein.alert")
    alert_log.addHandler(listener)
    try:
        services.alerter.alert(
            "这是一条测试告警",
            "看到它说明告警通道是通的。真实告警长这样:采集器掉线、解析失败、待确认积压。",
        )
    finally:
        alert_log.removeHandler(listener)

    if failures:
        print("\n发送失败,告警现在只剩日志:", file=sys.stderr)
        print(f"  {failures[0].exc_info[1]}", file=sys.stderr)
        return 1

    print("已发出。去邮箱看一眼 —— 收不到就查垃圾箱,QQ 常把自己发给自己的信归进去")
    return 0


def _email_channel_of(services) -> object | None:
    """从降级链里把邮件那一环找出来。找不到就是没配。"""
    from lifein.channels.email import EmailChannel

    channels = getattr(services.channel, "_channels", [])
    return next((c for c in channels if isinstance(c, EmailChannel)), None)


def cmd_rules(args: argparse.Namespace) -> int:
    """看主动规则的状态与**影子期的数据**。

    03 要求"先跑一周影子模式统计,再决定是否转 active",而统计的前提是
    看得见 —— 在这条命令之前,影子记录只进得去、出不来。

    误报率要人自己判:命令把影子期那些"要是真发出来会长什么样"逐条列出来,
    你数一下其中几条是你不想收的。**低于 20% 才允许转 active**
    ([R4](../docs/05-risks.md#r4--主动推送误报摧毁信任))。
    """
    from lifein.repos import push_log, rule_state
    from lifein.rules import builtin

    since = datetime.now(UTC) - timedelta(days=args.days)

    with session_scope() as session:
        modes = rule_state.all_modes(args.user, session)
        records = push_log.list_since(args.user, session, since=since, limit=500)

    print(f"最近 {args.days} 天\n")
    for rule in builtin.ALL_RULES:
        mode = modes.get(rule.rule_id, rule_state.RuleMode.SHADOW)
        mine = [r for r in records if r.rule_id == rule.rule_id]
        shadowed = [r for r in mine if r.mode == "shadow"]
        active = [r for r in mine if r.mode == "active"]
        print(
            f"{rule.rule_id:<24} {mode.value:<7} "
            f"影子 {len(shadowed):>3} 条,真推 {len(active):>3} 条"
        )

    if not args.detail:
        print("\n加 --detail 逐条看影子期的内容(判断误报率要看这个)")
        return 0

    print("\n影子期逐条(判断哪些是你不想收的):")
    for record in records:
        if record.mode != "shadow":
            continue
        print(f"  {record.created_at:%m-%d %H:%M}  [{record.rule_id}]  {record.title}")
    return 0


def cmd_rule_mode(args: argparse.Namespace) -> int:
    """开关一条规则(产品定义 §5:每条主动推送都要能一键关闭该类规则)。

    **`off` 和 `shadow` 不是一回事**(06 §2.12):`off` 是你主动关掉的那一档,
    不该再被自动转回 active;`shadow` 是还在观察期,迟早要转。
    """
    from lifein.repos import rule_state

    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1
        rule_state.set_mode(
            args.user, session, rule_id=args.rule, mode=rule_state.RuleMode(args.mode)
        )

    print(f"{args.rule} → {args.mode}")
    if args.mode == "active":
        # 不拦,但要问一句:R4 说误报两次就足够让人关掉通知
        print("转 active 之前看过影子期的数据了吗?误报率要低于 20%(rules --detail)")
    return 0


def cmd_import_statement(args: argparse.Namespace) -> int:
    """导一份月度对账单进 `raw_events`(P2 第 6/7/8 片的入口)。

    **是手动的,而且按 [ADR-012](../docs/04-tech-decisions.md) 本来就该是手动的** ——
    那张表把支付宝/微信导出标成"半自动,每月手动触发"。信用卡对账单邮件
    标的是"完全自动",那条路要走邮箱附件,还没接上(见 AGENTS §9)。

    这条命令只负责**入库**,不负责入账。落进 `raw_events` 之后,对账 job
    下一次跑起来会去匹配实时那些记录、回填真实商户名,匹配不上的补成新记录。
    分开的理由是那一步有幂等键,而这一步的幂等靠 `UNIQUE (user_id, source,
    external_id)` —— **两道各守一边,同一份账单导十次和导一次一样**。

    密码走交互输入,和 `set-imap` 一个理由:命令行参数会进 shell history、
    会出现在 `ps` 的输出里。
    """
    from lifein.repos import raw_events as raw_events_repo
    from lifein.sources import statement_csv, statement_ingest, statement_pdf

    path = Path(args.file)
    if not path.exists():
        print(f"文件不存在:{path}", file=sys.stderr)
        return 1
    data = path.read_bytes()

    password = None
    if args.password_prompt:
        password = getpass.getpass("打开密码(直接回车表示没有):") or None

    settings = get_settings()
    year = args.year or datetime.now(settings.tzinfo).year

    try:
        rows = _parse_statement(
            data,
            password=password,
            year=year,
            path=path,
            pdf=statement_pdf,
            csv_source=statement_csv,
        )
    except ValueError as exc:
        # 三种失败都在这里落地:密码错、AES 包、读不开。它们各自的信息
        # 已经写成了人话,直接打出来 —— 密码错那一种用户自己就能修
        print(str(exc), file=sys.stderr)
        return 1

    if not rows:
        print("这份文件里一行交易都没认出来。", file=sys.stderr)
        print("PDF 的话多半是表格定位没认出来(见 ADR-023 的触发条件),", file=sys.stderr)
        print("导出的话看看是不是拿错了文件。", file=sys.stderr)
        return 1

    period = statement_ingest.period_of(rows)
    events = statement_ingest.to_events(
        rows, issuer=args.issuer, tz=settings.tzinfo, period=period
    )
    counts = statement_ingest.summarize(events)

    if args.dry_run:
        print(f"解出 {counts['lines']} 行(支出 {counts['outbound']},进账 {counts['inbound']})")
        for row in rows[:10]:
            print(
                f"  {row.occurred_on}  {row.amount:>10}  {row.direction.value:<6}"
                f"  {row.kind.value if row.kind else '(类型不明)':<10}  {row.merchant_raw or ''}"
            )
        if len(rows) > 10:
            print(f"  ...(还有 {len(rows) - 10} 行)")
        print("这是 --dry-run,没有入库。")
        return 0

    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1
        result = raw_events_repo.insert_events(args.user, session, events)

    print(f"入库 {result.inserted} 行,重复跳过 {result.duplicates} 行(周期 {period or '跨月'})")
    if result.duplicates and not result.inserted:
        # 不是错误,是幂等在起作用 —— 但要说出来,否则看起来像什么都没干
        print("这一份之前已经导过了。重导不会变成两份账。")
    print("对账 job 下一次跑起来会把它们和实时记录对上。")
    return 0


def _parse_statement(
    data: bytes,
    *,
    password: str | None,
    year: int,
    path: Path,
    pdf,
    csv_source,
):
    """按文件内容挑解析器,**不按扩展名**。

    扩展名是用户改得动的,而 PDF 和 ZIP 的魔数改不动。挑错了的表现是
    "一行都认不出来",而那和"这份账单格式不认识"长得一模一样 ——
    排查时会往完全错误的方向走。
    """
    if data[:4] == b"%PDF":
        rows = []
        for table in pdf.open_tables(data, password=password):
            rows.extend(pdf.rows_from_table(table, year=year))
        return rows

    parsed_rows = csv_source.open_rows(data, password=password)
    return csv_source.rows_from_rows(parsed_rows, year=year)



def cmd_budget(args: argparse.Namespace) -> int:
    """设或删一条预算(P2 第 9 片)。

    **不设预算就一条预警都不会发。** 这是刻意的:猜出来的额度一定是错的,
    而一条错的预算发出的每一次提醒都是误报,而 R4 说误报两次就足够
    让人永久关掉通知。
    """
    from lifein.repos import budgets

    category = args.category
    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1

        if args.delete:
            removed = budgets.delete_budget(args.user, session, category=category)
            print("已删除" if removed else "本来就没有这条预算")
            return 0

        try:
            budget = budgets.set_budget(
                args.user,
                session,
                amount=Decimal(str(args.amount)),
                category=category,
                alert_threshold=Decimal(str(args.threshold)),
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    name = budget.category or "总预算"
    threshold = int(budget.alert_threshold * 100)
    print(f"{name}:每月 {budget.amount} 元,用到 {threshold}% 时提醒一次")
    print("超支当天还会再提醒一次 —— 这条规则默认在影子模式,")
    print("看过几天再用 rule-mode --rule budget_alert --mode active 打开。")
    return 0


def cmd_budgets(args: argparse.Namespace) -> int:
    """看每条预算当期花到哪儿了。**算的是"到现在为止",不是上个月。**"""
    from lifein.repos import budgets

    now = datetime.now(get_settings().tzinfo)
    with session_scope() as session:
        rows = budgets.progress(args.user, session, now=now)

    if not rows:
        print("还没设任何预算 —— 没有预算就不会有超支预警")
        return 0

    print(f"{now:%Y-%m} 到今天为止:")
    for item in rows:
        name = item.budget.category or "总预算"
        mark = "已超支" if item.over else ("快到了" if item.near else "")
        print(
            f"  {name:<8} {item.spent:>10.2f} / {item.budget.amount:>10.2f}"
            f"  {int(item.ratio * 100):>3}%  {mark}"
        )
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lifein.admin")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="建用户")
    create.add_argument("--name", required=True)
    create.add_argument("--wecom-userid", required=True, help="企微成员 UserID,推送目标")
    create.add_argument("--tz", default="Asia/Shanghai")
    create.set_defaults(func=cmd_create_user)

    listing = sub.add_parser("list-users", help="列出未停用的用户")
    listing.set_defaults(func=cmd_list_users)

    imap = sub.add_parser("set-imap", help="配邮箱凭据(授权码交互输入)")
    imap.add_argument("--user", required=True)
    imap.add_argument("--host", required=True, help="imap.qq.com / imap.163.com / imap.126.com")
    imap.add_argument("--username", required=True)
    imap.add_argument("--port", type=int, default=993)
    imap.set_defaults(func=cmd_set_imap)

    login_wx = sub.add_parser("login-weixin", help="扫码连微信(推荐)")
    login_wx.add_argument("--user", required=True)
    login_wx.add_argument("--timeout", type=int, default=480, help="等扫码/等消息的秒数")
    login_wx.set_defaults(func=cmd_login_weixin)

    weixin = sub.add_parser("set-weixin", help="手工填微信凭据(已有会话时用)")
    weixin.add_argument("--user", required=True)
    weixin.add_argument("--to", required=True, help="推送目标的 iLink user id")
    weixin.add_argument("--base-url", default=WEIXIN_BASE_URL)
    weixin.add_argument("--context-token", default=None)
    weixin.set_defaults(func=cmd_set_weixin)

    push = sub.add_parser("test-push", help="真发一条测试消息,并告诉你走的哪个通道")
    push.add_argument("--user", required=True)
    push.set_defaults(func=cmd_test_push)

    back = sub.add_parser("backfill", help="一次性往回补一段时间的邮件(只入库不推送)")
    back.add_argument("--user", required=True)
    back.add_argument("--days", type=int, default=14, help="回溯多少天")
    back.add_argument(
        "--reparse",
        action="store_true",
        help="用当前解析器覆盖已有事件的 normalized(改了归一化之后用)",
    )
    back.set_defaults(func=cmd_backfill)

    dg = sub.add_parser("digest", help="在指定窗口上生成摘要(默认只打印)")
    dg.add_argument("--user", required=True)
    dg.add_argument("--days", type=int, default=1)
    dg.add_argument("--limit", type=int, default=500, help="从库里最多取多少条")
    dg.add_argument(
        "--max-events",
        type=int,
        default=MAX_EVENTS,
        help=f"最多送多少条给模型(默认 {MAX_EVENTS})。补历史时可以调大",
    )
    dg.add_argument("--push", action="store_true", help="真发到微信,不加就只打印")
    dg.set_defaults(func=cmd_digest)

    test = sub.add_parser("test-imap", help="实测能否登录并取信")
    test.add_argument("--user", required=True)
    test.add_argument("--days", type=int, default=1)
    test.set_defaults(func=cmd_test_imap)

    status = sub.add_parser("key-status", help="查还有多少凭据停在旧主密钥上")
    status.add_argument("--user", required=True)
    status.set_defaults(func=cmd_key_status)

    rotate = sub.add_parser("rotate-keys", help="用当前主密钥重新加密全部凭据")
    rotate.add_argument("--user", required=True)
    rotate.set_defaults(func=cmd_rotate_keys)

    issue = sub.add_parser("issue-device", help="给一台手机签发采集/查询密钥(分开签发)")
    issue.add_argument("--user", required=True)
    issue.add_argument(
        "--device-id",
        help="这台手机叫什么,吊销时按它指认。不给就自动生成一个(phone-xxxx)",
    )
    issue.add_argument(
        "--purpose",
        choices=("all", "collect", "query"),
        default="all",
        help="签哪几把。all 是两条独立的行、两把独立的密钥,不是 scope=both",
    )
    issue.add_argument(
        "--base-url",
        default="https://example.com",
        help="App 要连的地址(反代之后的),写进配码里省得手输",
    )
    issue.set_defaults(func=cmd_issue_device)

    revoke_dev = sub.add_parser("revoke-device", help="手机丢了:吊销这台设备的凭据")
    revoke_dev.add_argument("--user", required=True)
    revoke_dev.add_argument("--device-id", required=True)
    revoke_dev.add_argument(
        "--kind",
        choices=("collector", "app_device"),
        default=None,
        help="只吊销其中一种。不给就两种都吊销 —— 手机丢了该走这条",
    )
    revoke_dev.set_defaults(func=cmd_revoke_device)

    devices = sub.add_parser("list-devices", help="列出签发过的设备凭据(含已吊销)")
    devices.add_argument("--user", required=True)
    devices.set_defaults(func=cmd_list_devices)

    allow = sub.add_parser("allow-source", help="给采集白名单加一条来源(默认拒绝)")
    allow.add_argument("--user", required=True)
    source = allow.add_mutually_exclusive_group(required=True)
    source.add_argument("--package", help="安卓包名,全等匹配,如 com.tencent.mm")
    source.add_argument("--sms-sender", help="短信发件号,前缀匹配(号段)")
    allow.add_argument(
        "--purpose",
        choices=(collector.PURPOSE_MESSAGE, collector.PURPOSE_TRANSACTION),
        default=collector.PURPOSE_MESSAGE,
        help="P1 只有 message 会被放行",
    )
    allow.add_argument("--phase", default="P1")
    allow.set_defaults(func=cmd_allow_source)

    sources = sub.add_parser("list-sources", help="看白名单与采集器心跳")
    sources.add_argument("--user", required=True)
    sources.set_defaults(func=cmd_list_sources)

    alert = sub.add_parser("test-alert", help="真发一条告警,验证那条路走得通")
    alert.add_argument("--user", required=True)
    alert.set_defaults(func=cmd_test_alert)

    rules = sub.add_parser("rules", help="看主动规则的状态与影子期数据")
    rules.add_argument("--user", required=True)
    rules.add_argument("--days", type=int, default=7, help="看最近几天,默认一周")
    rules.add_argument("--detail", action="store_true", help="逐条列影子期的内容")
    rules.set_defaults(func=cmd_rules)

    stmt = sub.add_parser("import-statement", help="导一份月度对账单(PDF 或支付宝/微信导出)")
    stmt.add_argument("--user", required=True)
    stmt.add_argument("--file", required=True, help="对账单文件。PDF 还是压缩包按内容认,不看扩展名")
    stmt.add_argument(
        "--issuer",
        required=True,
        help="发卡行或平台,如 cmb / alipay / wechat。它进 external_id,同一家要一直用同一个词",
    )
    stmt.add_argument(
        "--year",
        type=int,
        help="账单周期的年份。日期列只有月日时用得上 —— 不给就用今年,而一月导上个月的账单要显式给",
    )
    stmt.add_argument(
        "--password-prompt",
        action="store_true",
        help="交互输入打开密码(PDF 的密码或压缩包密码)。不走命令行参数",
    )
    stmt.add_argument("--dry-run", action="store_true", help="只解析打印,不入库")
    stmt.set_defaults(func=cmd_import_statement)

    budget = sub.add_parser("budget", help="设一条月度预算(不设就不会有超支预警)")
    budget.add_argument("--user", required=True)
    budget.add_argument(
        "--category",
        default=None,
        help="类目,不给就是总预算。必须在记账用的那个枚举内",
    )
    budget.add_argument("--amount", type=float, help="每月多少钱")
    budget.add_argument(
        "--threshold",
        type=float,
        default=0.9,
        help="用到几成时提醒一次(默认 0.9)。超支当天会另外再提醒一次",
    )
    budget.add_argument("--delete", action="store_true", help="删掉这条预算")
    budget.set_defaults(func=cmd_budget)

    budget_list = sub.add_parser("budgets", help="看每条预算当期花到哪儿了")
    budget_list.add_argument("--user", required=True)
    budget_list.set_defaults(func=cmd_budgets)

    mode = sub.add_parser("rule-mode", help="开关一条规则(off 是你主动关的那一档)")
    mode.add_argument("--user", required=True)
    mode.add_argument("--rule", required=True)
    mode.add_argument("--mode", required=True, choices=("shadow", "active", "off"))
    mode.set_defaults(func=cmd_rule_mode)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
