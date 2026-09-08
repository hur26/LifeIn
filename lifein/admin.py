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
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lifein.channels.base import Card
from lifein.channels.weixin import BASE_URL as WEIXIN_BASE_URL
from lifein.config import get_settings
from lifein.db import session_scope
from lifein.repos import channel_state, credentials, users
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

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
