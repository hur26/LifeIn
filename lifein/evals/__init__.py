"""评测集 —— agent 五项契约的**第五项**([ADR-013](../../docs/04-tech-decisions.md))。

格式定义在 [06 §7](../../docs/06-data-model.md#7-评测集格式),这里是它的可执行版本。

它存在的理由只有一个,ADR-013 说得很直白:
**改一个 agent 的 prompt 时,要能立刻知道有没有把另一个弄坏。**

这个模块**不碰模型也不碰数据库**,只做两件纯粹的事:把 JSONL 读成用例、
拿断言去比输出。跑模型的部分在 `runner.py`,导负样本的部分在 `export.py` ——
分开是因为这一层要能在 `pytest` 里被验,而那两层不能(一个要花钱,一个要真库)。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EQUALS = "equals"
CONTAINS = "contains"
COUNT = "count"
ABSENT = "absent"
AT_MOST = "at_most"

OPS = frozenset({EQUALS, CONTAINS, COUNT, ABSENT, AT_MOST})
"""断言只有五种,**都是机械可判的**(06 §7.2)。

不加"像不像""差不多"这一类:ADR-013 那句"结构化才能被代码校验,自由文本没法测"
是硬约束。写不出机械可判的期望,说明那条样本还没想清楚该验什么。
"""

_INDEX = re.compile(r"^(.*?)\[(\d+)\]$")


class EvalError(ValueError):
    """评测集本身有问题(不是 agent 有问题)。一律在跑之前抛。"""


class PathMissing(LookupError):
    """路径在输出里走不到。"""


@dataclass(frozen=True)
class Expectation:
    path: str
    op: str
    value: Any = None

    def describe(self) -> str:
        if self.op == ABSENT:
            return f"{self.path} 应为空"
        return f"{self.path} {self.op} {self.value!r}"


@dataclass(frozen=True)
class Case:
    id: str
    input: dict[str, Any]
    expect: list[Expectation] = field(default_factory=list)
    note: str = ""


def load_cases(path: str | Path) -> list[Case]:
    """读一个 JSONL 评测集。**空行跳过,坏行报错。**

    坏行不跳过:评测集悄悄少了两条,通过率还会变好看 —— 而那正是这套机制
    最不该出现的失效方式。
    """
    file = Path(path)
    if not file.exists():
        raise EvalError(f"评测集不存在:{file}")

    cases: list[Case] = []
    seen: set[str] = set()
    for line_no, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalError(f"{file}:{line_no} 不是合法 JSON:{exc}") from exc
        case = _to_case(payload, where=f"{file}:{line_no}")
        if case.id in seen:
            # id 重复会让"这条什么时候开始不过的"查不出来
            raise EvalError(f"{file}:{line_no} 用例 id 重复:{case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


def check(output: Mapping[str, Any], case: Case) -> list[str]:
    """拿一条用例的断言去比输出。返回失败说明,**空列表就是通过**。

    **全部断言都要过。** 不做"过了一半算部分通过":一条用例回答的是
    "这次行为对不对",而不是"对了几成"。
    """
    failures: list[str] = []
    for expectation in case.expect:
        try:
            value = resolve(output, expectation.path)
        except PathMissing:
            # `absent` 是唯一的例外:对它来说,走不到就是"确实没有"。
            # 别的断言走不到一律算失败 —— 路径写错和结果不对要一样地红
            if expectation.op == ABSENT:
                continue
            failures.append(f"{expectation.path}:路径走不到")
            continue

        ok, detail = _evaluate(expectation, value)
        if not ok:
            failures.append(f"{expectation.describe()},实际 {detail}")
    return failures


def resolve(data: Any, path: str) -> Any:
    """按 `items[0].route` 这样的路径走进输出。走不到抛 `PathMissing`。"""
    current = data
    for segment in path.split("."):
        if not segment:
            raise EvalError(f"路径写法不对:{path!r}")
        match = _INDEX.match(segment)
        name, index = (match.group(1), int(match.group(2))) if match else (segment, None)

        if name:
            if not isinstance(current, Mapping) or name not in current:
                raise PathMissing(path)
            current = current[name]
        if index is not None:
            if not isinstance(current, Sequence) or isinstance(current, str | bytes):
                raise PathMissing(path)
            if index >= len(current):
                raise PathMissing(path)
            current = current[index]
    return current


def _evaluate(expectation: Expectation, value: Any) -> tuple[bool, Any]:
    op = expectation.op
    expected = expectation.value

    if op == EQUALS:
        return value == expected, value
    if op == CONTAINS:
        if isinstance(value, str):
            return str(expected) in value, value
        if isinstance(value, Sequence):
            return expected in value, value
        return False, value
    if op == COUNT:
        if not isinstance(value, Sequence | Mapping):
            return False, value
        return len(value) == expected, len(value)
    if op == ABSENT:
        if value is None:
            return True, value
        if isinstance(value, Sequence | Mapping):
            return len(value) == 0, value
        return False, value
    if op == AT_MOST:
        try:
            return float(value) <= float(expected), value
        except (TypeError, ValueError):
            return False, value
    raise EvalError(f"不认识的断言:{op}")


def _to_case(payload: Any, *, where: str) -> Case:
    if not isinstance(payload, dict):
        raise EvalError(f"{where} 顶层不是对象")

    case_id = str(payload.get("id", "")).strip()
    if not case_id:
        raise EvalError(f"{where} 缺 id。id 要稳定不变,改期望时也保留它")
    if not isinstance(payload.get("input"), dict):
        raise EvalError(f"{where} 的 input 必须是对象")

    raw_expect = payload.get("expect")
    if not isinstance(raw_expect, list) or not raw_expect:
        # 空断言的用例跑起来永远绿,比没有这条用例更糟
        raise EvalError(f"{where} 的 expect 不能为空:没有期望的样本测不出任何东西")

    expectations = []
    for item in raw_expect:
        if not isinstance(item, dict) or "path" not in item or "op" not in item:
            raise EvalError(f"{where} 的断言要有 path 和 op")
        if item["op"] not in OPS:
            raise EvalError(f"{where} 不认识的断言 {item['op']},只有 {sorted(OPS)}")
        expectations.append(
            Expectation(path=str(item["path"]), op=str(item["op"]), value=item.get("value"))
        )

    return Case(
        id=case_id,
        input=payload["input"],
        expect=expectations,
        note=str(payload.get("note", "")),
    )


def dump_case(case: Case) -> str:
    """把用例写回一行 JSON。导出负样本时用(export.py)。"""
    return json.dumps(
        {
            "id": case.id,
            "input": case.input,
            "expect": [
                {"path": e.path, "op": e.op, **({} if e.value is None else {"value": e.value})}
                for e in case.expect
            ],
            "note": case.note,
        },
        ensure_ascii=False,
        default=str,
    )
