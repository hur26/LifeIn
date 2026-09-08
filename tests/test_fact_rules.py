"""事实库的纯规则 —— 置信度封顶与去重键。不需要数据库。"""

from __future__ import annotations

import pytest

from lifein.models.normalized import Trust
from lifein.repos.facts import (
    EXTERNAL_MAX_CONFIDENCE,
    FactError,
    cap_confidence,
    statement_key,
)


def test_external_is_capped():
    """06 §1.2 第 3 条:外部内容推出的事实,置信度上限 0.6。

    邮件正文里的"我下周离职"可能是转发的别人的话。模型再有把握也不该
    让这句话看起来像用户自己确认过的。
    """
    assert cap_confidence(0.95, Trust.EXTERNAL) == EXTERNAL_MAX_CONFIDENCE
    assert cap_confidence(0.3, Trust.EXTERNAL) == pytest.approx(0.3)


def test_user_input_is_not_capped():
    assert cap_confidence(0.95, Trust.USER_INPUT) == pytest.approx(0.95)


def test_confidence_out_of_range_is_rejected():
    # 越界值多半来自模型输出被直接透传,那时候封顶会把它悄悄变成 0.6,
    # 掩盖掉"解析出了个 1.7"这件事
    with pytest.raises(FactError):
        cap_confidence(1.7, Trust.USER_INPUT)


def test_statement_key_ignores_whitespace_and_case():
    assert statement_key("张三  下周三 来北京") == statement_key("张三下周三来北京")
    assert statement_key("Meet Bob") == statement_key("meetbob")


def test_statement_key_does_not_pretend_to_understand():
    """换个语序就不是同一个键 —— 这是刻意的,不是缺陷。

    认出"下周三张三来北京"和"张三下周三来北京"是同一件事需要语义比较,
    那是向量的活。写入路径上假装能做到,只会得到一个偶尔漏、偶尔误的去重。
    """
    assert statement_key("张三下周三来北京") != statement_key("下周三张三来北京")
