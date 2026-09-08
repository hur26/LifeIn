"""月度报告 agent(P2 第 10 片,第六个 agent)。

**它一个数字都不产出。** 数字全在 `repos/reports.py` 里用 SQL 算好了
([铁律 9](../../AGENTS.md#1-铁律)),这个 agent 拿着算好的数,
只回答一件事:**这个月有什么值得说的**。

分开的理由不是洁癖。一份月度报告全是钱,而**算错的报告和算对的长得一模一样** ——
没有人会去核对"餐饮 1234.56"是不是真的等于那几十笔之和。让模型碰数字,
错误就永远不会被发现;让它只写评语,错了一眼就能看出来(评语和上面的数对不上)。

## 第 4 层复核在这里的形态

和记账 agent 一样,模型说了什么和它说得对不对是两件事。这里的复核是:

1. **评语里出现的每一个数字,都必须在给它的那份统计里逐字出现过。**
   编一个"比上月多了 300"出来是这条链路最典型的错法,而那句话读起来
   比真话还顺
2. 提到的类目必须在这个月真的有支出 —— 模型很爱提"你这个月娱乐花得少",
   而那一类可能根本没有记录
3. 条数超了就截断,不是重试:一份月报里五条观察已经算多

不过的条目**直接丢掉**,不进待确认队列 —— 月度报告是一次性的,
一条没写好的评语进队列只会让人第二天看见一条没头没尾的东西。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from lifein.agents.contract import OnUncertain, agent
from lifein.channels.base import Card, CardSection
from lifein.llm.client import LLMBadResponse, LLMClient
from lifein.repos.reports import MonthlyReport

log = logging.getLogger(__name__)

MAX_NOTES = 5
"""最多留几条观察。**一份月报里五条已经算多** —— 再多就没人读完,
而没人读完的报告等于没发。"""

MAX_NOTE_CHARS = 60


class MonthlyInput(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    report: MonthlyReport


class MonthlyOutput(BaseModel):
    notes: list[str] = Field(default_factory=list)
    """写给人看的观察,每条一句话。**里面的数字都被复核过。**"""

    dropped_invented: int = 0
    """编了数字被丢掉的条数。**这个数不该长期大于 0** ——
    大了说明 prompt 在诱导模型算账,而它不该算账。"""

    dropped_unknown_category: int = 0


@dataclass(frozen=True)
class MonthlyResult:
    output: MonthlyOutput
    llm_fields_sent: list[str]
    prompt_tokens: int | None
    completion_tokens: int | None


class MonthlyReportFailed(RuntimeError):
    """这一轮没产出结果。调度层要告警,不要静默发一份没有评语的报告。"""


_TASK = (
    "下面是某人这个月的记账统计,数字都已经算好了。\n\n"
    "**你不需要也不要重新计算任何数字。** 你的任务是挑出最多 "
    f"{MAX_NOTES} 条值得说的观察,每条一句话,不超过 {MAX_NOTE_CHARS} 个字。\n\n"
    "值得说的是这类:\n"
    "- 某一类比上个月明显多了或少了\n"
    "- 某一个商户占的比例意外地高\n"
    "- 没归类的金额偏大(说明有些消费没被认出来)\n\n"
    "**不值得说的**:重复念一遍上面的数字;夸奖或者批评;"
    "任何形式的理财建议。\n\n"
    "引用数字时**只能原样抄上面出现过的**,不要自己算差值、比例或者合计。\n\n"
    '输出严格的 JSON:{"notes": ["第一句", "第二句"]}'
)


def build_summary(report: MonthlyReport) -> str:
    """把算好的统计写成给模型看的那段文字。

    **给的是算好的数,不是原始交易。** 原始交易一送进去,模型就会开始
    自己求和,而它求和会错 —— 那正是这一片要避免的事。
    """
    lines = [
        f"账单周期:{report.period}",
        f"总支出:{report.total} 元,共 {report.count} 笔",
    ]
    if report.last_total is not None:
        lines.append(f"上个月总支出:{report.last_total} 元")
    if report.uncategorized > 0:
        lines.append(f"其中没归类:{report.uncategorized} 元")

    lines.append("")
    lines.append("按类目:")
    for line in report.categories:
        last = f",上个月 {line.last_total} 元" if line.last_total is not None else ""
        lines.append(f"- {line.category}:{line.total} 元,{line.count} 笔{last}")

    if report.merchants:
        lines.append("")
        lines.append("花得最多的几个商户:")
        for merchant in report.merchants:
            lines.append(f"- {merchant.merchant}:{merchant.total} 元,{merchant.count} 笔")
    return "\n".join(lines)


@agent(
    name="monthly_report",
    inputs=MonthlyInput,
    tools=[],  # 只写字,不写库
    output_schema=MonthlyOutput,
    on_uncertain=OnUncertain.DO_NOTHING,
    evalset="evals/monthly_report.jsonl",
)
def write_notes(payload: MonthlyInput, *, llm: LLMClient) -> MonthlyResult:
    """写这个月的观察。**数字进,评语出。**"""
    report = payload.report
    if report.is_empty:
        # 一笔都没有的月份没什么可说的,也不该为此花一次调用(铁律 9)
        return MonthlyResult(
            output=MonthlyOutput(), llm_fields_sent=[], prompt_tokens=None, completion_tokens=None
        )

    summary = build_summary(report)
    response = llm.chat(
        [
            {"role": "system", "content": _TASK},
            {"role": "user", "content": summary},
        ]
    )

    try:
        parsed = response.as_json()
    except LLMBadResponse as exc:
        raise MonthlyReportFailed(f"月度报告评语解析失败:{exc}") from exc

    output = _review(parsed, report=report, summary=summary)
    return MonthlyResult(
        output=output,
        # 送出去的是算好的统计,不是原始交易 —— 审计里要看得出这一点
        llm_fields_sent=["monthly_summary"],
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _review(payload: object, *, report: MonthlyReport, summary: str) -> MonthlyOutput:
    """复核。**模型说了什么,和它说得对不对,是两件事。**"""
    if not isinstance(payload, dict):
        raise MonthlyReportFailed(f"模型返回的顶层不是对象:{type(payload).__name__}")

    known_numbers = set(_NUMBER.findall(summary))
    known_categories = {line.category for line in report.categories}
    output = MonthlyOutput()

    for raw in payload.get("notes") or []:
        note = str(raw).strip()
        if not note:
            continue
        if len(note) > MAX_NOTE_CHARS:
            note = note[:MAX_NOTE_CHARS]

        invented = [n for n in _NUMBER.findall(note) if n not in known_numbers]
        if invented:
            # **编数字是这条链路最典型的错法**,而那句话读起来比真话还顺
            log.info("丢弃编了数字的观察:%s(编的是 %s)", note, invented)
            output.dropped_invented += 1
            continue

        mentioned = [c for c in _mentioned_categories(note) if c not in known_categories]
        if mentioned:
            # 模型很爱说"你这个月娱乐花得少",而那一类可能根本没有记录
            log.info("丢弃提到不存在类目的观察:%s", note)
            output.dropped_unknown_category += 1
            continue

        output.notes.append(note)
        if len(output.notes) >= MAX_NOTES:
            break

    return output


def _mentioned_categories(note: str) -> list[str]:
    from lifein.repos.transactions import CATEGORIES

    return [category for category in CATEGORIES if category in note]


def to_card(report: MonthlyReport, output: MonthlyOutput) -> Card:
    """月度报告的卡片版。**数字来自 `report`,评语来自 `output`** ——
    卡片这一层也不做任何计算,免得又多一处可能算错的地方。
    """
    sections = [
        CardSection(
            heading="按类目",
            lines=[
                f"{line.category} {line.total} 元{_delta(line.delta)}"
                for line in report.categories[:6]
            ],
        )
    ]
    if output.notes:
        sections.append(CardSection(heading="这个月", lines=list(output.notes)))
    if report.uncategorized > 0:
        # 单独说:混进"其他"的话,报告会声称自己看懂了这些钱
        sections.append(
            CardSection(heading="还没归类", lines=[f"{report.uncategorized} 元"])
        )

    return Card(
        title=f"{report.period} 账单",
        summary=f"共支出 {report.total} 元,{report.count} 笔{_delta(report.delta)}",
        sections=sections,
        footer=_coverage_footer(report),
    )



def to_long_card(report: MonthlyReport, output: MonthlyOutput) -> Card:
    """邮件长版(03 那句"企微卡片 + 邮件长版")。

    **长版存在的理由是推送通道有长度上限**:企微卡片 2048 字节、微信 4000 字,
    而一份完整的月报有十个类目加五个商户,一定会被截断 —— 而截断之后
    **看起来仍然是一份完整的报告**,只是后面几类没了。

    邮件没有这个限制,所以它是唯一能看到全貌的那一份。两份的数字来自同一个
    `report`,不会出现"卡片上说 3120,邮件里说 3121"那种事 ——
    那种不一致比缺几行糟得多。
    """
    sections = [
        CardSection(
            heading="按类目",
            lines=[
                f"{line.category}  {line.total} 元  {line.count} 笔{_delta(line.delta)}"
                for line in report.categories
            ],
        )
    ]
    if report.merchants:
        sections.append(
            CardSection(
                heading="花得最多的几家",
                lines=[
                    f"{m.merchant}  {m.total} 元  {m.count} 笔" for m in report.merchants
                ],
            )
        )
    if output.notes:
        sections.append(CardSection(heading="这个月", lines=list(output.notes)))
    if report.uncategorized > 0:
        sections.append(
            CardSection(
                heading="还没归类",
                lines=[
                    f"{report.uncategorized} 元",
                    "这些消费没被认出属于哪一类。数字大的话是归类那一层要修。",
                ],
            )
        )

    return Card(
        title=f"{report.period} 账单(完整版)",
        summary=f"共支出 {report.total} 元,{report.count} 笔{_delta(report.delta)}",
        sections=sections,
        footer=_coverage_footer(report),
    )


def _delta(delta: Decimal | None) -> str:
    if delta is None:
        return ""
    if delta == 0:
        return "(和上月持平)"
    return f"(比上月{'多' if delta > 0 else '少'} {abs(delta)} 元)"


def _coverage_footer(report: MonthlyReport) -> str:
    """脚注说对账覆盖率。**它是"这份报告可不可信"的唯一提示** ——
    覆盖率低就意味着有些消费根本没进账本,而报告本身看不出这一点。
    """
    if report.reconciled_ratio is None:
        return "还没有对过账"
    return f"其中 {int(report.reconciled_ratio * 100)}% 已和对账单核对过"
