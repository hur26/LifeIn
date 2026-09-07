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
from lifein.channels.wecom import WecomChannel
from lifein.channels.wecom_callback import WecomCallback
from lifein.channels.wecom_client import WecomClient
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
    channel: WecomChannel
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

    channel = WecomChannel(wecom, resolve_userid=_resolve_wecom_userid)

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
        alerter=LoggingAlerter(),
    )


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
