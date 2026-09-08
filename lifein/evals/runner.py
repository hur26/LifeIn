"""跑评测集 —— **它会真的调外部模型,真的花钱**(06 §7.4)。

所以它不进 CI、不进 `pytest`:是改完 prompt 之后手动跑的动作。
纯逻辑那半边在 `__init__.py`,那半边进 pytest。

跑法统一是因为四个 agent 的契约统一(ADR-013):输入是声明好的模型,
输出是声明好的 schema,处理函数都是 `(输入, llm=…)`。
**这就是"五项契约"里那两项换来的东西** —— 没有它们,这个文件写不出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from lifein.agents.contract import get_agent, registered_agents
from lifein.evals import Case, EvalError, check, load_cases
from lifein.llm.client import LLMClient

log = logging.getLogger(__name__)


@dataclass
class CaseResult:
    case_id: str
    failures: list[str] = field(default_factory=list)
    error: str | None = None
    """agent 直接炸了(解析失败、超时)。**和"结果不对"分开记** ——
    前者是坏了,后者是不够好,两种要分别处理。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures and self.error is None


@dataclass
class Report:
    agent: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.results)

    def render(self) -> str:
        lines = [f"# {self.agent}:{self.passed}/{self.total} 通过,花掉 {self.tokens} token"]
        for result in self.results:
            if result.passed:
                continue
            if result.error:
                lines.append(f"  ✗ {result.case_id} 炸了:{result.error}")
            else:
                for failure in result.failures:
                    lines.append(f"  ✗ {result.case_id}:{failure}")
        if self.total and self.passed == self.total:
            # 100% 通常说明样本太容易,不是 agent 太好(06 §7.4)
            lines.append("  (全过。看看是不是该补几条更难的样本)")
        return "\n".join(lines)


def run_agent(name: str, *, llm: LLMClient, cases: list[Case] | None = None) -> Report:
    """跑一个 agent 的评测集。

    用例从它契约里声明的 `evalset` 路径读 —— **路径是契约的一部分**,
    不让调用方随便指,否则"这个 agent 的评测集是哪一份"就没有唯一答案。
    """
    spec = get_agent(name)
    loaded = cases if cases is not None else load_cases(Path(spec.evalset))
    report = Report(agent=name)

    for case in loaded:
        result = CaseResult(case_id=case.id)
        try:
            payload = spec.inputs.model_validate(case.input)
        except Exception as exc:  # noqa: BLE001
            # 输入形状不对是评测集的 bug,不是 agent 的
            raise EvalError(f"{name}/{case.id} 的 input 不符合 {spec.inputs.__name__}:{exc}") from exc

        try:
            outcome = spec.handler(payload, llm=llm)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            report.results.append(result)
            continue

        result.prompt_tokens = getattr(outcome, "prompt_tokens", None) or 0
        result.completion_tokens = getattr(outcome, "completion_tokens", None) or 0
        result.failures = check(outcome.output.model_dump(mode="json"), case)
        report.results.append(result)

    return report


def run_all(*, llm: LLMClient) -> list[Report]:
    """跑全部 agent。**缺评测集的直接报错,不静默跳过** ——
    ADR-013 说缺一项不许注册,那么"文件不存在"也该是同一种响亮的失败。"""
    return [run_agent(name, llm=llm) for name in sorted(registered_agents())]
