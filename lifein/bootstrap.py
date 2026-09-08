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
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from lifein.alerts import Alerter, LoggingAlerter
from lifein.channels.base import Channel, InboundMessage
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


@dataclass(frozen=True)
class Services:
    settings: Settings
    llm: LLMClient
    wecom: WecomClient
    channel: Channel
    """推送出口。默认是"微信优先、企微兜底"的组合(ADR-018)。"""

    callback: WecomCallback
    alerter: Alerter


def build_services(settings: Settings | None = None) -> Services:
    """建进程级组件。配置有问题会在这里炸,而不是等第一次用到。"""
    s = settings or get_settings()

    llm = LLMClient(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key.get_secret_value(),
        model=s.llm_model,
        timeout_s=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        price_prompt_per_1k=Decimal(str(s.llm_price_prompt_per_1k)),
        price_completion_per_1k=Decimal(str(s.llm_price_completion_per_1k)),
    )

    wecom = WecomClient(
        corp_id=s.wecom_corp_id,
        secret=s.wecom_secret.get_secret_value(),
        agent_id=s.wecom_agent_id,
    )

    alerter = LoggingAlerter()

    # ADR-018:微信优先,企微兜底。微信没配过会话时 WeixinChannel 会抛错,
    # 于是自动落到企微 —— 所以"还没配微信"和"微信坏了"走的是同一条路径,
    # 不需要在这里判断配没配
    wecom_channel = WecomChannel(wecom, resolve_userid=_resolve_wecom_userid)
    channel = FallbackChannel(
        [WeixinChannel(load_session=_load_weixin_session), wecom_channel],
        alerter=alerter,
    )

    callback = WecomCallback(
        token=s.wecom_callback_token.get_secret_value(),
        aes_key=s.wecom_callback_aes_key.get_secret_value(),
        corp_id=s.wecom_corp_id,
    )

    return Services(
        settings=s,
        llm=llm,
        wecom=wecom,
        channel=channel,
        callback=callback,
        alerter=alerter,
    )


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
    if user and s.wecom_calendar_id:
        adapters.append(
            WecomCalendarAdapter(
                services.wecom,
                CalendarConfig(cal_id=s.wecom_calendar_id, owner_wecom_userid=user.wecom_userid),
            )
        )
    else:
        log.warning("未配置 WECOM_CALENDAR_ID,跳过日历采集")

    return adapters
