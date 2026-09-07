"""归一化骨架 —— docs/06-data-model.md §1 的可执行版本。

**一切数据源都要落到这一个结构上。** 邮件、日历、通知、短信、账单 CSV、
PDF 对账单没有例外。落不上去的字段进 `raw_events.raw`,不要往骨架上加字段 ——
加字段意味着所有已有适配器都要重新审视一遍。

这个模块刻意不 import 数据库、不 import 任何数据源。它是一份契约,
适配器依赖它,它不依赖任何人。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TITLE_MAX = 120


class EventKind(StrEnum):
    MESSAGE = "message"
    CALENDAR_EVENT = "calendar_event"
    TRANSACTION = "transaction"
    TASK = "task"
    DOCUMENT = "document"


class Trust(StrEnum):
    """trust 是安全机制,不是元数据(06 §1.2)。

    取值只有两个,不要加第三个 —— 一旦出现"半可信",调用方就会开始猜。
    """

    USER_INPUT = "user_input"
    """用户本人主动输入:企微里发的消息、App 里手动补的一笔。"""

    EXTERNAL = "external"
    """其他一切:邮件、群消息、通知、转账备注、日历邀请、账单文件。"""


class PartyRole(StrEnum):
    FROM = "from"
    TO = "to"
    CC = "cc"
    ORGANIZER = "organizer"
    ATTENDEE = "attendee"
    MERCHANT = "merchant"
    PAYER = "payer"
    PAYEE = "payee"


class IdentifierType(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    WECOM_USERID = "wecom_userid"
    CARD_LAST4 = "card_last4"
    MERCHANT_CODE = "merchant_code"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class Flag(StrEnum):
    """归一化过程中发生了什么,给下游看的。

    这些不是错误 —— 错误走 `normalize_error` 并告警(06 §1.4)。
    这里记的是"入库了,但你得知道它不完整"。
    """

    TRUNCATED = "truncated"
    AGGREGATED = "aggregated"
    PARTIAL = "partial"
    PARSE_DEGRADED = "parse_degraded"


class Party(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: PartyRole
    display_name: str
    identifier: str | None = None
    identifier_type: IdentifierType | None = None

    @model_validator(mode="after")
    def _identifier_needs_type(self) -> Party:
        # 有值没类型的标识符没法用来归并实体,等于白存
        if self.identifier is not None and self.identifier_type is None:
            raise ValueError("给了 identifier 就必须给 identifier_type")
        return self


class Amount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Decimal = Field(max_digits=14, decimal_places=2)
    currency: str = "CNY"
    direction: Direction


class ExternalRef(BaseModel):
    """去重与溯源的依据。与 `raw_events (user_id, source, external_id)` 唯一键对应。"""

    model_config = ConfigDict(extra="forbid")

    source: str
    external_id: str


class Attachment(BaseModel):
    """只存引用与元信息,**不存内容**。

    附件内容进库等于又建一份全量副本,而它多半还是最敏感的那部分(账单 PDF)。
    """

    model_config = ConfigDict(extra="forbid")

    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    external_ref: str | None = None


class NormalizedEvent(BaseModel):
    """所有数据源的统一落点。字段含义见 06 §1.1。"""

    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    title: str
    occurred_at: datetime
    """事件**真实发生**时间,不是摄入时间。必须带时区。"""

    external_ref: ExternalRef
    trust: Trust
    confidence: float = Field(ge=0.0, le=1.0)
    """归一化本身的置信度,**不是内容的**。别拿它当"这条消息可不可信"用。"""

    parties: list[Party] = Field(default_factory=list)
    amount: Amount | None = None
    body: str | None = None
    location: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)

    @field_validator("occurred_at")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        # 无时区的时间戳会在"昨天的邮件"这种窗口计算上悄悄错一整天
        if v.tzinfo is None:
            raise ValueError("occurred_at 必须带时区")
        return v

    @model_validator(mode="after")
    def _apply_skeleton_rules(self) -> NormalizedEvent:
        # 标题超长自动截断,并留下 truncated —— 下游要能看出这是截过的
        if len(self.title) > TITLE_MAX:
            object.__setattr__(self, "title", self.title[:TITLE_MAX])
            self._add_flag(Flag.TRUNCATED)

        if not self.title.strip():
            raise ValueError("title 不能为空:摘要和列表全靠它")

        # 交易类必须有金额。没有金额的"交易"不该走到这一层,
        # 它属于归一化失败,要写 normalize_error 并告警(06 §1.4)
        if self.kind is EventKind.TRANSACTION and self.amount is None:
            raise ValueError("kind=transaction 时 amount 必填")
        if self.kind is not EventKind.TRANSACTION and self.amount is not None:
            raise ValueError("只有 kind=transaction 才允许带 amount")

        return self

    def _add_flag(self, flag: Flag) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    @property
    def is_external(self) -> bool:
        """外部内容进 prompt 必须结构化隔离,且不得触发 L3(06 §1.2)。"""
        return self.trust is Trust.EXTERNAL
