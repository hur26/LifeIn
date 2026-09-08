"""装配 —— 把散落的组件按配置拼起来。

**只有这一个模块知道"谁依赖谁"。** 其余模块一律靠构造函数收依赖,
所以它们在测试里可以被单独替换掉(前面每个测试文件都是这么做的)。

分两层拼:

- `build_services()` 拼**进程级**的东西:配置、LLM 客户端、企微客户端。
  它们无状态、可复用,建一次用到进程结束。
- `build_adapters()` 拼**用户级**的东西:邮箱适配器要用那个用户的 IMAP
  授权码,日历适配器要用那个用户的企微 userid。它们每次跑任务时现建。

这条分界不是洁癖:P4 来第二个用户时,进程级的部分完全不用动。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from lifein.agents.contract import validate_all
from lifein.alerts import Alerter, EmailAlerter, LoggingAlerter
from lifein.channels.base import Channel, InboundMessage
from lifein.channels.email import EmailChannel, SmtpConfig, SmtpTransport
from lifein.channels.fallback import FallbackChannel
from lifein.channels.wecom import WecomChannel
from lifein.channels.wecom_callback import WecomCallback
from lifein.channels.wecom_client import WecomClient
from lifein.channels.weixin import BASE_URL as WEIXIN_BASE_URL
from lifein.channels.weixin import WeixinChannel, WeixinSession
from lifein.config import Settings, get_settings
from lifein.db import session_scope
from lifein.llm.client import LLMClient
from lifein.repos import credentials, users
from lifein.sources.base import PullAdapter
from lifein.sources.calendar_source import CalendarConfig, WecomCalendarAdapter
from lifein.sources.email_adapter import EmailAdapter
from lifein.sources.imap_client import ImapConfig, ImapMailbox

log = logging.getLogger(__name__)


def register_tools() -> None:
    """把能力层的工具挂上注册表,并校验每个 agent 的白名单都指向真实存在的工具。

    **导入即注册**(架构 §9.2),所以"哪些工具在线"取决于谁被 import 过 ——
    这种事不能碰运气,放在装配这一步显式做一次。

    `validate_all` 放在这里而不是注册时,是因为导入顺序不可控:agent 模块
    可能先于工具模块加载。晚几十毫秒,换来的是不用关心谁先谁后
    (contract.py 里写着同一件事)。
    """
    from lifein.agents import (  # noqa: F401
        bookkeeper,
        digest,
        memory,
        monthly_report,
        planner,
        qa,
    )
    from lifein.tools import memory as memory_tools  # noqa: F401
    from lifein.tools import todo as todo_tools  # noqa: F401
    from lifein.tools import transaction as transaction_tools  # noqa: F401

    validate_all()


@dataclass(frozen=True)
class Services:
    settings: Settings
    llm: LLMClient
    channel: Channel
    """推送出口。配了企微就是"微信优先、企微兜底";没配就只有微信(ADR-018)。"""

    alerter: Alerter
    resolve_user: Callable[[Session, InboundMessage], users.User | None] = field(
        default=lambda session, message: resolve_user_for_message(session, message)
    )
    """把通道内的发送者换成本系统用户。放在这里,入站通道和 HTTP 入口拿的是同一个。"""

    wecom: WecomClient | None = None
    callback: WecomCallback | None = None
    """没配企微时是 None。企微要配可信 IP 得先有公网域名,而 iLink 让这件事
    在 P0 变成可选的 —— 但代价要说清:没有兜底通道,也没有日历数据源。"""


def build_services(settings: Settings | None = None) -> Services:
    """建进程级组件。配置有问题会在这里炸,而不是等第一次用到。"""
    s = settings or get_settings()

    register_tools()

    llm = LLMClient(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        model=s.llm_model,
        timeout_s=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        price_prompt_per_1k=Decimal(str(s.llm_price_prompt_per_1k)),
        price_completion_per_1k=Decimal(str(s.llm_price_completion_per_1k)),
        embedding_model=s.embedding_model,
        embedding_dim=s.embedding_dim,
    )

    # ADR-018:微信优先,企微兜底。微信没配过会话时 WeixinChannel 会抛错,
    # 于是自动落到企微 —— 所以"还没配微信"和"微信坏了"走的是同一条路径,
    # 不需要在这里判断配没配
    channels: list[Channel] = [WeixinChannel(load_session=_load_weixin_session)]

    wecom: WecomClient | None = None
    callback: WecomCallback | None = None
    if s.wecom_enabled:
        wecom = WecomClient(
            corp_id=s.wecom_corp_id,
            secret=s.wecom_secret.get_secret_value(),
            agent_id=s.wecom_agent_id,
        )
        channels.append(WecomChannel(wecom, resolve_userid=_resolve_wecom_userid))
        callback = WecomCallback(
            token=s.wecom_callback_token.get_secret_value(),
            aes_key=s.wecom_callback_aes_key.get_secret_value(),
            corp_id=s.wecom_corp_id,
        )
    else:
        # 只剩一条通道的时候必须说出来。"没有兜底"和"兜底没生效"表现一样,
        # 而前者是你自己选的、后者是故障 —— 启动时说一次,免得以后分不清
        log.warning("未配置企业微信:没有兜底推送通道,也没有日历数据源")

    email_channel = _build_email_channel(s)
    if email_channel is not None:
        # 排在最后:前面两条的共同失效方式是平台(会话过期、接口改版、
        # 账号受限),而邮件不依赖任何平台政策(R6)
        channels.append(email_channel)
    else:
        log.warning("未配置 SMTP:降级链最后没有邮件兜底,告警也只写日志")

    channel = FallbackChannel(channels, alerter=LoggingAlerter())
    alerter: Alerter = (
        # 告警走邮件而不是主通道:告警最需要发出去的时刻,正是主通道挂了的时刻。
        # 降级链自己的告警仍然只写日志 —— 否则"邮件发不出去"会试着用邮件告诉你
        EmailAlerter(email_channel, lambda: _first_active_user())
        if email_channel is not None
        else LoggingAlerter()
    )

    return Services(
        settings=s,
        llm=llm,
        wecom=wecom,
        channel=channel,
        callback=callback,
        alerter=alerter,
    )


def _build_email_channel(s: Settings) -> EmailChannel | None:
    """建邮件通道。**先看环境变量,没有就复用邮箱采集的那把授权码。**

    第二条是有理由的:QQ / 163 / 126 的 IMAP 与 SMTP **是同一个授权码**,
    而它已经加密躺在 `credentials` 里了。再要人往 `.env` 里抄一份明文,
    正好撞上 [07 §1](../docs/07-config.md) 那句"最容易做错的是把 IMAP 授权码
    写进环境变量",也和 [ADR-009](../docs/04-tech-decisions.md) 冲突 ——
    同一份秘密不该同时存在于加密的库里和明文的文件里。

    环境变量那条路留着:换一个专门发信的邮箱(不被采集的那个)时用它。
    """
    config = _smtp_from_env(s) or _smtp_from_mailbox(s)
    if config is None:
        return None

    # 没单独配收件人就发给自己。**不会回环** —— 发出去的信带 X-LifeIn-Push,
    # 采集侧见到就跳过(email.py 那条),而告警发到你天天看的那个邮箱正是要的
    to_address = s.smtp_to or config.sender
    return EmailChannel(
        SmtpTransport(config),
        from_address=config.sender,
        resolve_address=lambda _user_id: to_address,
    )


def _smtp_from_env(s: Settings) -> SmtpConfig | None:
    if not s.smtp_enabled:
        return None
    return SmtpConfig(
        host=s.smtp_host or "",
        port=s.smtp_port,
        username=s.smtp_username or "",
        password=(s.smtp_password.get_secret_value() if s.smtp_password else ""),
        sender=s.smtp_sender_address,
        use_ssl=s.smtp_use_ssl,
    )


def _smtp_from_mailbox(s: Settings) -> SmtpConfig | None:
    """从 `imap` 凭据派生发信配置。取不到就返回 None,不猜。"""
    user_id = _first_active_user()
    if user_id is None:
        return None

    try:
        with session_scope() as session:
            imap = credentials.get_credential(user_id, session, kind="imap", settings=s)
    except Exception:  # noqa: BLE001 —— 解不开或连不上库,都只是"没有邮件通道"
        log.exception("读邮箱凭据失败,邮件通道不可用")
        return None

    if not imap:
        return None

    host = _smtp_host_for(str(imap.get("host", "")))
    if host is None:
        # 猜不出来就不猜:发信主机猜错的表现是每次告警都失败,
        # 而告警失败本身是不会被告警的
        log.warning("从 %s 推不出发信主机,要用邮件通道请配 SMTP_*", imap.get("host"))
        return None

    username = str(imap.get("username", ""))
    return SmtpConfig(
        host=host,
        # 465 + SSL:QQ / 163 / 126 都支持,而 587 在部分网络上被封
        port=465,
        username=username,
        password=str(imap.get("auth_code", "")),
        sender=username,
        use_ssl=True,
    )


def _smtp_host_for(imap_host: str) -> str | None:
    """`imap.qq.com` → `smtp.qq.com`。

    只认这一种形状。国内几家邮箱都是这么排的,而**猜不出来时宁可不启用** ——
    一个连不上的发信主机会让降级链多出一条必然失败的通道(07 §2.7 那条理由)。
    """
    host = imap_host.strip().lower()
    return "smtp." + host[len("imap.") :] if host.startswith("imap.") else None


def _first_active_user() -> str | None:
    """告警发给谁。P0/P1 只有一个用户,取第一个就是他。

    每次现查而不是启动时缓存:进程可能起在建用户之前,而那时缓存下来的
    "没有用户"会让整个告警通道在余下的运行期里静默。
    """
    try:
        with session_scope() as session:
            ids = users.list_active_users(session)
        return ids[0] if ids else None
    except Exception:  # noqa: BLE001
        log.exception("查不到告警收件用户")
        return None


def _load_weixin_session(user_id: str) -> WeixinSession | None:
    """从加密的 credentials 表取微信会话。没配过就返回 None,由降级接手。"""
    with session_scope() as session:
        stored = credentials.get_credential(
            user_id, session, kind="weixin", settings=get_settings()
        )
    if not stored:
        return None
    return WeixinSession(
        token=stored["token"],
        to_user_id=stored["to_user_id"],
        base_url=stored.get("base_url") or WEIXIN_BASE_URL,
        context_token=stored.get("context_token"),
    )


def resolve_user_for_message(session: Session, message: InboundMessage) -> users.User | None:
    """按通道把发送者换成本系统用户。

    企微给的是成员 UserID,直接查 `users.wecom_userid`。
    iLink 给的是对方在 bot 会话里的 user id —— 那个值存在微信凭据里,
    所以反过来遍历用户去比对。**P0 只有一个用户**,这个"遍历"就是一次比较;
    P4 要换成一张索引表,到时候只改这个函数。
    """
    if message.channel == "wecom":
        return users.find_by_wecom_userid(session, wecom_userid=message.sender)

    if message.channel == "weixin":
        settings = get_settings()
        for user_id in users.list_active_users(session):
            stored = credentials.get_credential(user_id, session, kind="weixin", settings=settings)
            if stored and stored.get("to_user_id") == message.sender:
                return users.get_user(user_id, session)
        return None

    log.warning("不认识的入站通道:%s", message.channel)
    return None


def _resolve_wecom_userid(user_id: str) -> str:
    """user_id → 企微 userid。

    自己开事务:推送可能发生在任何上下文里(定时任务、回调、手动触发),
    让调用方传 session 会把这个映射的存在扩散到每一处调用点。
    """
    with session_scope() as session:
        user = users.get_user(user_id, session)
        if user is None:
            raise LookupError(f"用户不存在:{user_id}")
        return user.wecom_userid


def build_own_identifiers(user_id: str, session: Session, services: Services) -> list[str]:
    """这个用户"自己"是谁。记忆抽取拿它把用户本人排除在实体之外。

    目前只有邮箱地址一项 —— 企微 userid 由记忆 job 自己从 `users` 里取,
    那张表它本来就要读。凭据只在这个模块里被读,别处拿不到明文。
    """
    imap = credentials.get_credential(user_id, session, kind="imap", settings=services.settings)
    username = (imap or {}).get("username", "")
    return [username] if username else []


def build_adapters(user_id: str, session: Session, services: Services) -> list[PullAdapter]:
    """建这个用户的数据源适配器。

    **拿不到凭据就不建那个适配器**,而不是建一个必然失败的。少一个数据源的
    摘要仍然有用,而每天失败一次的适配器只会把告警变成背景噪音
    (jobs/daily_digest.py 里那条"一个源挂了不影响另一个")。
    """
    adapters: list[PullAdapter] = []
    s = services.settings

    imap = credentials.get_credential(user_id, session, kind="imap", settings=s)
    if imap:
        adapters.append(
            EmailAdapter(
                ImapMailbox(
                    ImapConfig(
                        host=imap["host"],
                        username=imap["username"],
                        auth_code=imap["auth_code"],
                        port=int(imap.get("port", 993)),
                    )
                )
            )
        )
    else:
        log.warning("用户 %s 没有配 IMAP 凭据,跳过邮箱采集", user_id)

    user = users.get_user(user_id, session)
    if user and s.wecom_calendar_id and services.wecom is not None:
        adapters.append(
            WecomCalendarAdapter(
                services.wecom,
                CalendarConfig(cal_id=s.wecom_calendar_id, owner_wecom_userid=user.wecom_userid),
            )
        )
    else:
        log.warning("未配置企微日历(需要 WECOM_* 与 WECOM_CALENDAR_ID),跳过日历采集")

    return adapters
