"""一整批真实短信过规则层,断言的是**不变量**,不是逐条的答案。

语料从 `scripts/sms-corpus.py` 生成,落在 `evals/sms_corpus.jsonl` ——
**它不进版本库**(和评测集那批真实邮件同一个待遇,R12 同级敏感)。
没有那个文件时整组跳过,所以日常 `pytest` 和 CI 都不依赖它,
和 `TEST_DATABASE_URL` 那套是同一个路子。

仓库里只有 `evals/sms_corpus.example.jsonl`,几条**形状一样、内容是编的**
样本 —— 它让这组用例在任何机器上都跑得起来,也让语料的格式有个活的说明。

## 为什么是不变量,不是逐条标答案

5042 条真实短信没法一条条标"这是不是一笔交易"。但有些性质**不需要标答案
也一定成立**,而它们恰好能抓住 2026-09-11 那一批 bug 的整个类别:

| 不变量 | 它抓的是什么 |
| --- | --- |
| 抠出来的金额后面不能紧跟数字 | 金额被吃掉一半(旧正则在这批语料上违反 **69 次**) |
| 脱敏之后不能留下"卡词+四位数字" | 卡号原样送去外部模型(R12) |
| 正文以【】开头就一定抠得出签名 | 银行识别整条链路的入口 |

**逐条标答案那件事有专门的地方**:[06 §7](../docs/06-data-model.md) 的评测集格式。
这里做的是另一件事 —— 用大量真实输入去撞那些"不可能对但没人想到要测"的形状。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lifein.sources.sms_signature import signature_of
from lifein.sources.transaction_text import _CARD_WORDS, parse, redact_for_model

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "evals" / "sms_corpus.jsonl"
EXAMPLE = ROOT / "evals" / "sms_corpus.example.jsonl"

_LEADING_SIGNATURE = re.compile(r"^\s*[【\[]([^】\]]{1,20})[】\]]")
_CARD_LEFTOVER = re.compile(rf"(?:{_CARD_WORDS})\s*[:：]?\s*\d{{4,}}")


def corpus() -> list[dict]:
    """真语料优先,没有就用仓库里那几条示例。

    **两个都跑得通才算数**:示例保证这组用例在任何机器上有意义,
    真语料保证它真的撞得到没想到的形状。
    """
    source = REAL if REAL.exists() else EXAMPLE
    if not source.exists():  # pragma: no cover —— 示例是进库的,正常不会缺
        pytest.skip("没有语料,跳过")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        pytest.skip(f"{source.name} 是空的")
    return rows


@pytest.fixture(scope="module")
def messages() -> list[dict]:
    return corpus()


def _amount_was_truncated(body: str, matched: str) -> bool:
    """匹配到的金额是不是被吃掉了一半。

    **判据里要放过以"元"收尾的那种**:`定投30元08月14日确认成功` 里,
    `30元` 后面紧跟的是日期而不是金额的剩余部分 —— 那是对的。
    只有不带"元"后缀、后面又接着数字或小数点的,才是真的被截断
    (`人民币500` 后面跟着 `0.00`)。
    """
    start = body.find(matched)
    if start < 0 or matched.rstrip().endswith("元"):
        return False
    return body[start + len(matched) :][:1] in "0123456789."


class TestAmountsAreNotEatenInHalf:
    """2026-09-11:千分位那一支的 `*` 不要求真有逗号,于是 `人民币5000.00`
    解出 500、`人民币12345.67` 解出 123。**这批语料里 69 条会被记错。**"""

    def test_no_amount_is_cut_short(self, messages):
        offenders = []
        for row in messages:
            found = parse(None, row["body"])
            if found and _amount_was_truncated(row["body"], found.matched_amount_text):
                offenders.append(found.matched_amount_text)
        assert offenders == [], f"这些金额被吃掉了一半:{offenders[:5]}"

    def test_every_amount_is_positive(self, messages):
        for row in messages:
            found = parse(None, row["body"])
            if found:
                assert found.amount > 0


class TestNothingLeaksToTheModel:
    """R12:送给外部模型的那一份里不该还留着卡号。

    这一条和 `account_hint` 共用一份词表(`_CARD_WORDS`),**所以它顺带
    盯住了另一件事**:词表漏一个说法时,抽取和脱敏会一起漏 ——
    2026-09-11 漏的是"账户",而招行写的正是"您账户0361"。
    """

    def test_no_card_number_survives_redaction(self, messages):
        offenders = []
        for row in messages:
            leftover = _CARD_LEFTOVER.search(redact_for_model(row["body"]))
            if leftover:
                offenders.append(leftover.group(0))
        assert offenders == [], f"脱敏之后还留着卡号:{offenders[:5]}"


class TestSignaturesComeOutWhole:
    """银行识别整条链路的入口(ADR-034)。抠不出来 = 那家银行整月不入账。"""

    def test_a_leading_bracket_always_yields_that_signature(self, messages):
        for row in messages:
            head = _LEADING_SIGNATURE.match(row["body"])
            if head:
                assert signature_of(row["body"]) == head.group(1).strip()

    def test_no_signature_when_there_is_no_bracket(self, messages):
        for row in messages:
            if not _LEADING_SIGNATURE.match(row["body"]):
                assert signature_of(row["body"]) is None


def test_the_example_corpus_is_in_the_repo():
    """**示例必须进库。** 没有它,这组用例在别人机器上是空跑的,
    而空跑的用例是绿的 —— 那比没有用例更糟。"""
    assert EXAMPLE.exists()
    assert len(EXAMPLE.read_text(encoding="utf-8").strip().splitlines()) >= 5
