"""运维命令:`python -m lifein.admin <子命令>`。

存在的理由很直接:凭据只能通过 `credentials` 表进系统(加密在仓储层),
而 P0 没有 Web 界面 —— 没有这个 CLI,部署完的系统根本配不起来。

**授权码一律从交互输入读,不走命令行参数。** 命令行参数会进 shell history、
会出现在 `ps` 的输出里、会被跳板机的会话录制录下来。这三处都不是你能事后
清干净的地方。

    python -m lifein.admin create-user --name 白杨 --wecom-userid BaiYang
    python -m lifein.admin set-imap --user <uuid> --host imap.163.com --username me@163.com
    python -m lifein.admin test-imap --user <uuid>       # 07 §6 那条"IMAP 实测能登录"
    python -m lifein.admin key-status --user <uuid>      # 轮换收尾用
    python -m lifein.admin rotate-keys --user <uuid>
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import UTC, datetime, timedelta

from lifein.config import get_settings
from lifein.db import session_scope
from lifein.repos import credentials, users
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
