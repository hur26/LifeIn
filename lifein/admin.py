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
    python -m lifein.admin test-imap --user <uuid>       # 07 §6 那条"IMAP 实测能登录"
    python -m lifein.admin key-status --user <uuid>      # 轮换收尾用
    python -m lifein.admin rotate-keys --user <uuid>
    python -m lifein.admin issue-device --user <uuid> --device-id pixel-7a
    python -m lifein.admin revoke-device --user <uuid> --device-id pixel-7a  # 手机丢了
    python -m lifein.admin list-devices --user <uuid>
    python -m lifein.admin allow-source --user <uuid> --package com.tencent.mm
    python -m lifein.admin list-sources --user <uuid>   # 白名单 + 采集器心跳
    python -m lifein.admin rules --user <uuid> --detail # 影子期数据,判断误报率
    python -m lifein.admin rule-mode --user <uuid> --rule upcoming_schedule --mode active
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
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
        "device_id": args.device_id,
    }

    with session_scope() as session:
        if users.get_user(args.user, session) is None:
            print(f"用户不存在:{args.user}", file=sys.stderr)
            return 1

        for kind, scope in purposes:
            # 先吊销这台设备同类的旧凭据:留着两把有效的,验签用哪把取决于
            # 排序,而"换了密钥但旧的还能用"是最难发现的一类问题
            credentials.revoke_device(args.user, session, device_id=args.device_id, kind=kind)
            secret = new_shared_secret()
            credentials.put_credential(
                args.user,
                session,
                kind=kind,
                scope=scope,
                payload={"secret": secret},
                settings=settings,
                device_id=args.device_id,
            )
            provisioning["collector_secret" if scope == "ingest" else "query_secret"] = secret

    blob = json.dumps(provisioning, ensure_ascii=False, separators=(",", ":"))
    print(f"\n已签发 {len(purposes)} 条设备凭据,device_id={args.device_id}")
    print("下面这串只显示这一次,配进 App 之后就把窗口关了:\n")
    print(blob)

    qr_path = Path(f"device-{args.device_id}.html").resolve()
    try:
        qr_render.write_qr_html(
            blob,
            qr_path,
            title=f"配置 LifeIn 采集端 · {args.device_id}",
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
    issue.add_argument("--device-id", required=True, help="自己起,比如 pixel-7a。吊销按它")
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

    rules = sub.add_parser("rules", help="看主动规则的状态与影子期数据")
    rules.add_argument("--user", required=True)
    rules.add_argument("--days", type=int, default=7, help="看最近几天,默认一周")
    rules.add_argument("--detail", action="store_true", help="逐条列影子期的内容")
    rules.set_defaults(func=cmd_rules)

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
