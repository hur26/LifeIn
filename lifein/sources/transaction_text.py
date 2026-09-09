"""从银行短信与支付通知里**用规则**抠出金额、卡号后四位、方向。

[铁律 9](../../AGENTS.md#1-铁律):**能用规则拿到的字段不许交给 LLM。**
这既是省钱,更是隐私([R12](../../docs/05-risks.md#r12--外部-llm-供应商侧的数据暴露))——
金额和卡号是这条链路上最敏感的两样,它们根本不该离开服务器。

**这一层不判断"是不是真实支出"。** 那是四层防误判里的第 3 层(LLM)和第 4 层
(代码复核)的事([ADR-012](../../docs/04-tech-decisions.md#adr-012--账单采集以实时通知为主导出账单为辅))。
这里只回答"这段文字里的钱是多少、哪张卡、看起来是进还是出"。

`matched_amount_text` 是留给第 4 层的:那一层要求
**金额必须能在原文里逐字找到**,而"逐字"需要知道当初匹配到的是哪一段。

**格式会变,而且没有任何提前通知**([R8](../../docs/05-risks.md#r8--数据源格式变动))。
所以这里的规则宁可漏也不要猜:抠不出金额就返回 None,让整条进待确认,
而不是猜一个数字写进账本。

## 一件这一层必须判、而且只有这一层判得了的事:**钱还没动**

上面写着"这一层不判断是不是真实支出"。那句话对"这是消费还是还款"成立,
**但对"这笔钱到底发生了没有"不成立** —— 而后者是规则能硬判的:

    【南方基金】……定投计划将于近期扣款,扣款时间2026年09月10日,
    预计扣款金额50元。请保证定投扣款日前银行卡内有足够金额……

这是一条**预告**。抠出"50 元 + 扣款(流出)"完全正确,而把它当成一笔交易
送下去,后果是四层里只剩模型那一层能拦 —— 拦不住的话账本上凭空多一笔 50,
**而下个月真扣款时还会再记一次**。第 4 层复核也拦不住它:金额逐字找得到、
分类在枚举内、置信度也可以很高。

所以"未发生"在这里就判掉,和营销短信走同一条路:**丢弃,不进待确认**
(进队列的话你会收到一堆"您的账单即将出账")。

判据是**动词跟着未来的标记**,不是文本里出现了"预计"两个字 ——
"消费38.50元,预计3天后出账单"是一笔已经发生的消费。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from lifein.repos.transactions import Direction

log = logging.getLogger(__name__)

_AMOUNT_WITH_MARKER = re.compile(
    r"(?:人民币|RMB|CNY|¥|￥)\s*(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(?P<value2>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*元"
)
"""金额:**必须带货币标记或"元"**。

不写成"抓第一个数字" —— 短信里"尾号1234"和"9月8日"都是数字,
而抓错的后果是账本上出现一笔 1234 元的消费。宁可漏,不可猜。
"""

_CARD_WORDS = "尾号|卡号|账号|末四位|尾数|储蓄卡|信用卡|借记卡|银行卡|储值卡|工资卡"
"""卡号前面可能出现的词。

**后面那几个是拿真实短信补的**:招行写的是"您的招商银行**储蓄卡0361**于……",
一个"尾号"都没有。原来抠不到卡号的后果不显眼但很实在 ——
跨渠道合并和对账匹配都少了一维,而那正是"两张卡里各有一笔同额"时
唯一能区分它们的东西。
"""

_ACCOUNT_HINT = re.compile(rf"(?:{_CARD_WORDS})\s*[:：]?\s*\d*(\d{{4}})(?!\d)")
r"""卡号后四位。

`\d*(\d{4})(?!\d)` 而不是 `(\d{4})`:短信里偶尔出现完整卡号
(`卡号6222021234567890`),取前四位会得到一个**看起来像后四位、
实际是发卡行标识**的东西 —— 而那个值会让对账把两张不同的卡当成同一张。
"""

_FUTURE_PAYMENT = re.compile(
    r"(?:将于|将在|即将|预计|拟于|预约).{0,16}?(?:扣款|扣费|支付|付款|还款|到账|入账)"
    r"|(?:待|需|应)(?:支付|付款|还款|缴纳|缴费)"
    r"|(?:请|须)(?:于|在|保证|确保).{0,24}?(?:前|之前|足够|充足|余额)"
    r"|(?:到期应还|应还金额|最低还款额|账单日|还款日)"
)
"""**钱还没动**的标志。命中就当它不是交易(见模块开头)。

每一条都要求"未来的标记"和"动词"挨在一起,不是看见"预计"就丢:
`消费38.50元,预计3天后出账单` 是一笔已经发生的消费,而
`将于近期扣款` `预计扣款金额50元` `请保证卡内有足够金额` 都不是。

`.{0,16}?` 是非贪婪且有上限的:没有上限的话,一条真实消费短信末尾的
"详情请登录APP查询"会跟句首的动词连上,把整条误判成预告。

**动词表里没有"出账",这是拿真实短信试出来的:**
`消费38.50元,预计3天后出账单` 是一笔**已经发生**的消费,
"出账"说的是它什么时候进账单,不是钱什么时候动。留着它的话,
这条规则会开始吃掉真实的消费 —— 而**少一笔比多一笔更难发现**
(06 §2.6),那是比它要挡的问题更糟的方向。
"""

_OUTBOUND = ("消费", "支出", "付款", "支付", "扣款", "扣费", "转出", "还款")
_INBOUND = ("收入", "转入", "到账", "收款", "退款", "退回", "存入")

MASK = "****"

_MASK_BALANCE = re.compile(
    r"(余额|可用余额|账户余额|本次余额|可用额度|剩余额度)\s*[:：]?\s*"
    r"(?:人民币|RMB|CNY)?\s*[¥￥$]?\s*[\d,]+(?:\.\d+)?\s*(?:元)?"
)
"""余额。**这条短信里最敏感的一个数字**,而它甚至不在要存的清单里
(R10:交易类只保留金额、时间、卡号后四位、商户)。"""

_MASK_ACCOUNT = re.compile(rf"({_CARD_WORDS})\s*[:：]?\s*\d{{4,}}")
"""卡号。已经作为 `account_hint` 单独抠出来了,而**模型判断类型不需要它** ——
给模型的结构化字段里本来就没有它。

**关键词清单和 `_ACCOUNT_HINT` 共用一份。** 分成两份的后果是某天有人给
抽取那边加了"储蓄卡",遮罩这边忘了加 —— 于是那个卡号既被抠出来了
**又原样发给了外部供应商**。那正是 2026-09 用真实短信试出来的样子。
"""

_MASK_LONG_DIGITS = re.compile(r"(?<![\d.])\d{6,}(?!\d)(?!\.\d)(?!\s*元)")
r"""连续 6 位以上的裸数字:完整卡号、账号、手机号、订单号。

**两条例外是为了不误伤金额**,而误伤金额比漏遮一个号码更糟:
模型看到"支出****元"会把这条判成看不懂,于是整条进待确认队列,
而淹掉的队列等于没有队列。

- `(?!\d)`:**这一条不能省。** `\d{6,}` 会回退 —— 没有它的话
  `1234567.89` 里前六位会被单独匹配上,遮出一个 `****7.89元`,
  而那比不遮更糟:金额看起来还在,只是变成了另一个数
- `(?!\.\d)`:后面跟着小数部分的是金额(`1234567.89`)
- `(?!\s*元)`:后面跟着"元"的是金额

`12,345.67` 不需要例外 —— 它被逗号断成 `12`/`345`/`67`,每一段都不到六位。

**这条规则宁可漏遮也不误伤,是有意的取舍。** 真正的卡号有上面那条
`尾号|卡号|账号` 兜着,而那条不看长度。
"""


def redact_for_model(text: str | None) -> str:
    """把要送进 prompt 的正文遮一道(06 §5 第 5 条)。

    **遮的是送出去的那一份,不是库里那一份。** 第 4 层复核要拿原文做
    "金额逐字比对",而那一步读的是 `raw["parsed"]["matched"]` 与
    `normalized.body` —— 两者都不经过这里。

    铁律 9 那条"能用规则拿到的字段不进正文"原来只做了一半:结构化字段抽出来了,
    正文照样原样送出去,于是卡号和余额一起去了外部供应商那里。
    """
    if not text:
        return ""
    masked = _MASK_BALANCE.sub(lambda m: f"{m.group(1)}{MASK}", text)
    masked = _MASK_ACCOUNT.sub(lambda m: f"{m.group(1)}{MASK}", masked)
    return _MASK_LONG_DIGITS.sub(MASK, masked)


_MERCHANT_PATTERNS = (
    # 支付宝 / 微信的通知里商户常常跟在"向"或"在"后面
    re.compile(r"(?:向|在)\s*(?P<merchant>[^,,。;;\s]{2,20})\s*(?:付款|消费|支付|转账)"),
    re.compile(r"(?:商户|收款方|对方)\s*[:：]\s*(?P<merchant>[^,,。;;\s]{2,20})"),
)


@dataclass(frozen=True)
class ParsedTransaction:
    """规则抠出来的那部分。**没有 kind** —— 那是 LLM 和代码复核的事。"""

    amount: Decimal
    currency: str
    direction: Direction
    account_hint: str | None
    merchant_raw: str | None

    matched_amount_text: str
    """金额在原文里的原样片段。第 4 层复核要拿它做逐字比对。"""

    def redacted_fields(self) -> dict[str, object]:
        """入库时留下的**就是这几样**。

        [R10](../../docs/05-risks.md#r10--手机端采集器的越权读取) 要求
        "交易类只保留金额、时间、卡号后四位、商户,不存整条报文" ——
        整条报文里可能有余额、可能有姓名,而记账一样都用不上。
        """
        return {
            "amount": str(self.amount),
            "currency": self.currency,
            "direction": self.direction.value,
            "account_hint": self.account_hint,
            "merchant_raw": self.merchant_raw,
            "text_redacted": True,
        }


def looks_unpaid_yet(text: str) -> bool:
    """这条说的是**还没发生**的钱吗(扣款预告、账单提醒、待支付)。

    单独一个函数是为了它能被直接测:这是"账本上凭空多一笔"里最容易发生的
    那一种,而它在真实短信里非常常见(基金定投、信用卡账单、水电缴费)。
    """
    return _FUTURE_PAYMENT.search(text) is not None


def parse(title: str | None, body: str | None) -> ParsedTransaction | None:
    """把一条交易通知解成结构。**抠不出金额就返回 None。**

    返回 None 不是错误,是"这条看起来不是交易" —— 调用方应当让它走普通消息,
    或者丢弃,**绝不该猜一个金额**。

    两种返回 None:抠不出金额,以及**钱还没动**(见模块开头那段)。
    """
    text = " ".join(part for part in (title, body) if part).strip()
    if not text:
        return None

    if looks_unpaid_yet(text):
        # 预告、账单提醒、待支付。**记下来的话下个月真扣款时会记第二笔**
        log.info("这条说的是还没发生的钱,不当交易:%s", text[:40])
        return None

    amount, matched = _find_amount(text)
    if amount is None:
        return None

    return ParsedTransaction(
        amount=amount,
        currency="CNY",
        direction=_direction_of(text),
        account_hint=_first_group(_ACCOUNT_HINT, text),
        merchant_raw=_find_merchant(text),
        matched_amount_text=matched,
    )


def _find_amount(text: str) -> tuple[Decimal | None, str]:
    """先找带标记的金额,找不到再退回裸数字 + 元。

    **顺序很要紧**:"尾号1234的卡消费人民币38.50元"里,裸数字优先会抓到 1234。
    """
    match = _AMOUNT_WITH_MARKER.search(text)
    if match is None:
        return None, ""

    raw = match.group("value") or match.group("value2")
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None, ""

    if value <= 0:
        # 0 元的"交易"是营销短信里的常客("0元领取")
        return None, ""
    return value, match.group(0).strip()


def _direction_of(text: str) -> Direction:
    """进还是出。**这只是初判**,最终由第 3 层的 LLM 给 kind。

    先看流出的词:"还款"既有"还"也可能出现"到账",而它是流出。
    """
    if any(word in text for word in _OUTBOUND):
        return Direction.DEBIT
    if any(word in text for word in _INBOUND):
        return Direction.CREDIT
    # 认不出来时按流出:多记一笔支出会被你在账本上看见并改掉,
    # 而漏记一笔支出不会有任何人提醒你
    return Direction.DEBIT


def _find_merchant(text: str) -> str | None:
    for pattern in _MERCHANT_PATTERNS:
        found = _first_group(pattern, text, group="merchant")
        if found:
            return found
    return None


def _first_group(pattern: re.Pattern[str], text: str, *, group: int | str = 1) -> str | None:
    match = pattern.search(text)
    return match.group(group) if match else None
