"""安卓采集器上报的通知 —— 架构 §9.1 里的**推送型**数据源,第一个。

前面所有数据源都是"我们去拉";这一个是"它送上门"。差别只在事件从哪来,
产出完全一样,都是 `IngestedEvent`。

**这一层不判断内容,只筛选和归一化。** 采集器不解析、不归类(架构 §8.1),
服务端这一步也不做语义判断 —— 谁在群里说了什么、要不要变成日程,
那是 planner agent 的事,它读 `raw_events`。

筛选的三道顺序写死(06 §6.4),顺序本身是安全机制:

    白名单(默认拒绝) → purpose 闸门(P1 只放消息) → 验证码正则 → 归一化

验证码那道放在最后,是因为前两道已经把绝大多数东西挡在外面了 ——
而**它挡的那一类,漏一条的代价是资金损失**(R10),所以它必须是
"就算前面全放行了也还在"的那一道。

**这个模块不碰数据库。** 白名单规则由调用方查好传进来,
所以整条筛选链在测试里不需要真库就能逐条验 —— 而它正是最该被逐条验的地方。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lifein.models.normalized import (
    Amount,
    Direction,
    EventKind,
    ExternalRef,
    Flag,
    IdentifierType,
    NormalizedEvent,
    Party,
    PartyRole,
    Trust,
)
from lifein.repos.collector import (
    MATCH_PACKAGE,
    MATCH_SMS_SENDER,
    MATCH_SMS_SIGNATURE,
    PURPOSE_MESSAGE,
    PURPOSE_TRANSACTION,
    WhitelistRule,
)
from lifein.repos.transactions import Direction as TxnDirection
from lifein.sources import transaction_text
from lifein.sources.base import IngestedEvent
from lifein.sources.sms_signature import signature_of
from lifein.sources.verification_code import looks_like_verification_code

log = logging.getLogger(__name__)

SOURCE = "notification"
"""`raw_events.source`。**短信也用这个值**:架构 §2.1 的来源枚举里没有 `sms`,
而两者对下游是同一类东西(手机上展示过的一段文字)。
是通知还是短信记在 `raw.channel` 里,P2 记账要分开处理时按它分。"""

CHANNEL_NOTIFICATION = "notification"
CHANNEL_SMS = "sms"

OPEN_PURPOSES = frozenset({PURPOSE_MESSAGE, PURPOSE_TRANSACTION})
"""放行的 purpose。**改这一行就是打开或关掉记账链路** ——
放在这里而不是散在判断里,是为了让那件事只有一个开关。

`transaction` 是 P2 第 12 片加进来的,而**它排在那一期的倒数第三片是刻意的**:
闸门一开,真钱的数据就开始流进来,那之后再出的错发生在真实账本上。
所以四层防误判(白名单 → 5 分钟去重 → LLM 判定 → 代码复核)、
两阶段入账、覆盖率巡检全部先建好了,这一行才动。

**关掉它是安全的**:去掉 `PURPOSE_TRANSACTION`,交易类通知会退回
`phase_not_open`,已经入账的一笔都不动。03 的退出条件写着
"出现错记 → 停下来补防误判层",这一行就是"停下来"的那个动作。
"""

_AGGREGATED = re.compile(r"^\s*\[\s*\d+\s*条\s*\]")
"""系统把多条折叠成"[3 条] 张三: ……"。归一化留不下被折叠掉的那些,
所以要打标 —— 下游看到这条得知道自己看的是残缺的。"""

TITLE_FALLBACK_CHARS = 40


class DropReason:
    """丢弃原因。**和响应体里的键名是同一份**(06 §6.4),不要各写各的。"""

    NOT_WHITELISTED = "not_whitelisted"
    PHASE_NOT_OPEN = "phase_not_open"
    VERIFICATION_CODE = "verification_code"
    MALFORMED = "malformed"
    NOT_A_TRANSACTION = "not_a_transaction"
    """来源是放行的交易类,但文字里抠不出金额 —— 多半是银行的营销短信。

    **丢弃而不是进待确认**:ADR-012 的第 3 层本来就要显式区分"营销",
    而能用规则认出来的东西不该花一次模型调用(铁律 9)。
    进待确认的话,你的队列里会堆满"您有一张优惠券待领取"。
    """


@dataclass
class Screened:
    """一次上报筛完的结果。

    `events` 是要入库的,`dropped` 是**为什么没入库** —— 后者必须回给设备,
    否则采集端只能看到"发出去了",看不到"被收下了没有",
    而静默丢弃正是这条链路最该防的失效方式(R8)。
    """

    events: list[IngestedEvent] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


class NotificationAdapter:
    """把一次上报解成事件。架构 §9.1 的 `PushAdapter`。

    `device_id` 由**调用方按签名认出来的那个**传进来,不取 body 里的 ——
    body 是对方说了算的,而 `external_id` 要拼上它做去重键。
    """

    source = SOURCE

    def __init__(self, rules: Sequence[WhitelistRule], *, device_id: str) -> None:
        self._rules = [rule for rule in rules if rule.enabled]
        self._device_id = device_id

    def handle(self, payload: Mapping[str, Any]) -> list[IngestedEvent]:
        """`PushAdapter` 协议要的形状。丢弃原因要看 `screen()`。"""
        return self.screen(payload).events

    def screen(self, payload: Mapping[str, Any]) -> Screened:
        result = Screened()
        for item in payload.get("events") or []:
            self._screen_one(dict(item), result)
        return result

    def _screen_one(self, item: dict[str, Any], result: Screened) -> None:
        package_name = _text(item.get("source_app"))
        sender = _text(item.get("sender"))
        title = _text(item.get("title"))
        body = _text(item.get("text"))
        # **正文要在白名单之前读出来**:银行按短信签名匹配,而签名在正文开头
        # (ADR-034)。读出来只用于这一次判断,没放行的一个字都不入库
        signature = signature_of(body)

        rule = self._match(package_name=package_name, sender=sender, signature=signature)
        if rule is None:
            # 默认拒绝。手机端已经过滤过一次,这里不假设那次是对的
            result.drop(DropReason.NOT_WHITELISTED)
            return

        if rule.purpose not in OPEN_PURPOSES:
            # 白名单可以提前配好,放行是另一回事 —— 记账链路 P2 才打开
            result.drop(DropReason.PHASE_NOT_OPEN)
            return

        if looks_like_verification_code(title, body):
            # 整条丢弃。日志只记条数不记原文 —— 记下来等于把刚拦住的东西
            # 写进另一个更少人看管的地方
            log.info("丢弃一条疑似验证码的通知,来源 %s", package_name or sender or "未知")
            result.drop(DropReason.VERIFICATION_CODE)
            return

        external_id = _text(item.get("external_id"))
        occurred_at = _parse_time(item.get("posted_at"))
        if not external_id or occurred_at is None or not (title or body):
            # 形状不对的**不入库**:没有 external_id 就没有去重键,重发一次
            # 就多一条;没有时间就落不进任何窗口。这两样缺一条都不是
            # "归一化失败"那一档,而是"这条根本没法处理"
            result.drop(DropReason.MALFORMED)
            return

        if rule.purpose == PURPOSE_TRANSACTION:
            event = self._to_transaction_event(
                item,
                external_id=external_id,
                occurred_at=occurred_at,
                title=title,
                body=body,
                sender=sender,
            )
            if event is None:
                # 放行的来源,但不是一笔交易(营销短信、额度调整通知……)
                result.drop(DropReason.NOT_A_TRANSACTION)
                return
            result.events.append(event)
            return

        result.events.append(
            self._to_event(
                item,
                external_id=external_id,
                occurred_at=occurred_at,
                title=title,
                body=body,
                sender=sender,
            )
        )

    def _to_transaction_event(
        self,
        item: dict[str, Any],
        *,
        external_id: str,
        occurred_at: datetime,
        title: str,
        body: str,
        sender: str,
    ) -> IngestedEvent | None:
        """交易类的归一化。**金额、卡号、方向全走正则**(铁律 9)。

        抠不出金额就返回 None —— 那多半根本不是交易,而**猜一个金额写进账本**
        是这条链路上最不能犯的错(03 的退出条件:出现错记就停下来)。

        `body` 暂时保留:ADR-012 的第 3 层要靠它判断是不是真实支出。
        [R10](../../docs/05-risks.md#r10--手机端采集器的越权读取) 要求交易类
        "不存整条报文",所以**记账 agent 判完之后要立刻把正文换成脱敏字段**
        (`ParsedTransaction.redacted_fields()`)—— 那是第 4 片的事,
        在它做完之前,交易正文的留存时间由通知保留期兜着。
        """
        parsed = transaction_text.parse(title, body)
        if parsed is None:
            return None

        normalized = NormalizedEvent(
            kind=EventKind.TRANSACTION,
            title=title or (parsed.merchant_raw or "交易"),
            occurred_at=occurred_at,
            external_ref=ExternalRef(source=SOURCE, external_id=self._scoped(external_id)),
            trust=Trust.EXTERNAL,
            confidence=1.0,
            amount=Amount(
                value=parsed.amount,
                currency=parsed.currency,
                direction=(
                    Direction.DEBIT
                    if parsed.direction is TxnDirection.DEBIT
                    else Direction.CREDIT
                ),
            ),
            parties=_merchant_parties(parsed.merchant_raw),
            body=body or None,
        )
        return IngestedEvent(
            source=SOURCE,
            external_id=self._scoped(external_id),
            occurred_at=occurred_at,
            trust=Trust.EXTERNAL,
            raw={
                "device_id": self._device_id,
                "channel": _text(item.get("channel")) or CHANNEL_NOTIFICATION,
                "source_app": _text(item.get("source_app")),
                "sender": sender,
                "title": title,
                "text": body,
                "posted_at": occurred_at.isoformat(),
                # 规则抠出来的那几样一起存:第 4 层复核要拿 matched_amount_text
                # 做"金额必须能在原文逐字找到"的比对
                "parsed": {**parsed.redacted_fields(), "matched": parsed.matched_amount_text},
            },
            normalized=normalized,
        )

    def _match(
        self, *, package_name: str, sender: str, signature: str | None = None
    ) -> WhitelistRule | None:
        """命中哪一条规则。**越具体的越先** —— 不是"谁先加的谁赢"。

        按加入顺序找的话,一条"放行整个短信应用"会把所有更精确的规则永久
        吞掉:用户没法表达"全部短信当消息,但招行那些当交易",而他配出来的
        东西看起来是对的(两条规则都在、都启用着)。2026-09-11 真踩了这个。

        顺序就是具体程度:签名(一家机构)> 发件人 > 包名(一整个应用)。
        同一档之内仍然按加入顺序,那时先来的确实更该赢。
        """
        for match_type in (MATCH_SMS_SIGNATURE, MATCH_SMS_SENDER, MATCH_PACKAGE):
            for rule in self._rules:
                if rule.match_type != match_type:
                    continue
                if rule.matches(
                    package_name=package_name or None,
                    sender=sender or None,
                    signature=signature,
                ):
                    return rule
        return None

    def _to_event(
        self,
        item: dict[str, Any],
        *,
        external_id: str,
        occurred_at: datetime,
        title: str,
        body: str,
        sender: str,
    ) -> IngestedEvent:
        display = title or body[:TITLE_FALLBACK_CHARS]
        # 标题和正文都看:折叠标记("[3 条] 张三: ……")落在哪一边由 ROM 决定,
        # 各家不一样。只看一边就会在换手机之后悄悄漏掉这个标
        aggregated = _AGGREGATED.search(title) or _AGGREGATED.search(body)
        flags = [Flag.AGGREGATED] if aggregated else []

        normalized = NormalizedEvent(
            kind=EventKind.MESSAGE,
            title=display,
            occurred_at=occurred_at,
            external_ref=ExternalRef(source=SOURCE, external_id=self._scoped(external_id)),
            # 通知一律不可信:群里任何一个人、任何一个商户都能构造它的内容(R3)
            trust=Trust.EXTERNAL,
            # 字段是设备原样送来的,归一化本身没有猜的成分。
            # 这个 1.0 说的是"解析没走样",不是"内容可信"
            confidence=1.0,
            parties=_parties(title=title, sender=sender),
            body=body or None,
            flags=flags,
        )
        return IngestedEvent(
            source=SOURCE,
            external_id=self._scoped(external_id),
            occurred_at=occurred_at,
            trust=Trust.EXTERNAL,
            # 原文进 raw:解析器改好之后要能按同一个键重跑(06 §1.4)。
            # 它也是通知留存期那件事作用的地方(R10)
            raw={
                "device_id": self._device_id,
                "channel": _text(item.get("channel")) or CHANNEL_NOTIFICATION,
                "source_app": _text(item.get("source_app")),
                "sender": sender,
                "title": title,
                "text": body,
                "posted_at": occurred_at.isoformat(),
            },
            normalized=normalized,
        )

    def _scoped(self, external_id: str) -> str:
        """去重键拼上设备 id。

        两台手机可能各自生成同一个通知 id(安卓的 key 只在本机唯一),
        不拼的话第二台上报的会被当成重复静默丢掉。
        """
        return f"{self._device_id}:{external_id}"


def _merchant_parties(merchant_raw: str | None) -> list[Party]:
    """商户当作参与方。抠不出来就空着 —— **不拿标题冒充商户**:
    实时通知里的"标题"常常是 App 名(支付宝、微信支付),
    把它当商户会让归类规则表里堆满"支付宝"这种没有意义的条目(ADR-008)。"""
    if not merchant_raw:
        return []
    return [Party(role=PartyRole.MERCHANT, display_name=merchant_raw)]


def _parties(*, title: str, sender: str) -> list[Party]:
    """谁发的。

    通知的标题就是群名或联系人名 —— 这是通知监听能拿到的全部身份信息,
    没有更结构化的东西可抽(ADR-010 的能力边界)。短信多一个号码,
    那个能当标识符用。
    """
    if sender:
        return [
            Party(
                role=PartyRole.FROM,
                display_name=sender,
                identifier=sender,
                identifier_type=IdentifierType.PHONE,
            )
        ]
    if title:
        return [Party(role=PartyRole.FROM, display_name=title)]
    return []


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _parse_time(value: Any) -> datetime | None:
    """解 `posted_at`。**必须带时区** —— 没有时区的时间戳会在窗口计算上
    悄悄错整整八小时,而错进日历的日程比没有这个功能糟得多。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
