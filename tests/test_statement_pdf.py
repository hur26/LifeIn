"""信用卡对账单 PDF(P2 第 7 片)。

**这一组绝大部分不碰 PDF。** 逻辑全在 `rows_from_table()` 里,
它的输入是一张表(字符串的二维列表),而那正是 pdfplumber 交出来的东西。
用真 PDF 测那部分只会多一层噪音,还会让人误以为"表格定位"也测过了 ——
**它没有,而且这里也测不了**:我手上一份真的对账单都没有,
用自己造的 PDF 测自己写的定位,只能证明两边互相匹配。

碰 PDF 的只有一组:**密码错了报的是不是人话**。它是这条链路最可能出的错
(六位数字谁都会记岔),而 pdfminer 抛的 `PDFPasswordIncorrect`
直接冒到告警邮件里没人看得懂。
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest

from lifein.repos.transactions import Direction, TxnKind
from lifein.sources.statement_pdf import (
    StatementPasswordWrong,
    StatementUnreadable,
    open_tables,
    rows_from_table,
)

HEADER = ["交易日", "记账日", "交易描述", "交易金额", "卡号末四位", "交易类型"]


def table(*rows: list[str]) -> list[list[str]]:
    return [HEADER, *rows]


class TestReadingATable:
    def test_a_normal_line(self):
        (row,) = rows_from_table(
            table(["08-20", "08-21", "星巴克(国贸店)", "38.50", "1234", "消费"]),
            year=2026,
        )

        assert row.occurred_on == date(2026, 8, 20)
        assert row.amount == Decimal("38.50")
        assert row.direction is Direction.DEBIT
        assert row.merchant_raw == "星巴克(国贸店)"
        assert row.account_hint == "1234"
        assert row.kind is TxnKind.EXPENSE

    def test_the_year_comes_from_the_caller(self):
        """**很多对账单的日期列只有月日。** 从今天推年份会在一月出错 ——
        一月收到的是去年十二月的账单。"""
        (row,) = rows_from_table(
            table(["12-31", "01-02", "全家便利店", "12.00", "1234", "消费"]), year=2025
        )
        assert row.occurred_on == date(2025, 12, 31)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("38.50", Decimal("38.50")),
            ("1,234.56", Decimal("1234.56")),
            ("¥38.50", Decimal("38.50")),
            ("CNY 38.50", Decimal("38.50")),
        ],
    )
    def test_amount_shapes(self, text, expected):
        (row,) = rows_from_table(
            table(["08-20", "08-21", "商户", text, "1234", "消费"]), year=2026
        )
        assert row.amount == expected

    def test_a_negative_amount_becomes_a_direction_not_a_sign(self):
        """**库里的金额一律是正数。** 混着来的话"退款 -50"和"支出 50"
        在求和时会互相抵消,而它们是两件事。"""
        (row,) = rows_from_table(
            table(["08-20", "08-21", "退货", "-38.50", "1234", "退货"]), year=2026
        )

        assert row.amount == Decimal("38.50")
        assert row.direction is Direction.CREDIT
        assert row.kind is TxnKind.REFUND

    def test_the_kind_follows_the_type_column_not_the_sign(self):
        """退货那一行的金额有的银行写正数有的写负数,而类型列是明确的。"""
        (row,) = rows_from_table(
            table(["08-20", "08-21", "某商户退货", "38.50", "1234", "退货"]), year=2026
        )
        assert (row.kind, row.direction) == (TxnKind.REFUND, Direction.CREDIT)

    def test_a_repayment_is_recognized_from_the_description(self):
        """**还款不是消费。** 类型列常常是空的,而"还款"两个字在描述里。"""
        (row,) = rows_from_table(
            table(["08-25", "08-25", "手机银行还款", "5,000.00", "1234", ""]), year=2026
        )
        assert row.kind is TxnKind.REPAYMENT

    def test_an_unrecognized_type_stays_none(self):
        """**不按方向兜底。** debit 既可能是消费也可能是还款,
        兜底会让"把还款记成消费"发生得很安静。"""
        (row,) = rows_from_table(
            table(["08-20", "08-21", "某某某", "38.50", "1234", "其他"]), year=2026
        )
        assert row.kind is None

    def test_the_original_cells_come_along(self):
        """取错了的时候,这是唯一能看出错在哪一格的东西。"""
        cells = ["08-20", "08-21", "星巴克", "38.50", "1234", "消费"]
        (row,) = rows_from_table(table(cells), year=2026)
        assert row.raw_cells == tuple(cells)


class TestRowsThatAreNotTransactions:
    """**账本上多出一笔比少一笔糟得多**,所以这些必须被丢掉。"""

    @pytest.mark.parametrize(
        "cells",
        [
            ["", "", "本期应还金额", "5,000.00", "", ""],  # 没有日期
            ["08-20", "08-21", "小计", "", "1234", ""],  # 没有金额
            ["交易日", "记账日", "交易描述", "交易金额", "卡号末四位", "交易类型"],  # 表头重复
            ["08-20", "08-21", "零元礼包", "0.00", "1234", "消费"],  # 金额是 0
        ],
    )
    def test_dropped(self, cells):
        assert rows_from_table(table(cells), year=2026) == []

    def test_a_table_without_an_amount_column_is_refused_whole(self):
        """**认不出表头就整张不要,不按位置猜第几列是金额。**

        各家银行的列序不一样,按位置猜的代价是把"余额"当成消费额记进账本,
        而那种错误在报表上看不出来。
        """
        rows = rows_from_table(
            [["日期", "说明", "余额"], ["08-20", "星巴克", "12,345.67"]], year=2026
        )
        assert rows == []

    def test_an_empty_table_is_not_an_error(self):
        assert rows_from_table([], year=2026) == []
        assert rows_from_table([HEADER], year=2026) == []


class TestColumnNames:
    @pytest.mark.parametrize(
        "header",
        [
            ["交易日期", "记账日期", "商户名称", "金额", "卡号后四位", "类型"],
            ["交易日", "入账日期", "摘要", "人民币金额", "末四位", "交易类型"],
        ],
    )
    def test_other_banks_wording(self, header):
        rows = rows_from_table(
            [header, ["08-20", "08-21", "星巴克", "38.50", "1234", "消费"]], year=2026
        )
        assert len(rows) == 1 and rows[0].amount == Decimal("38.50")

    def test_the_first_matching_column_wins(self):
        """有些对账单同时有"交易金额"和"折合人民币金额" —— 前者才是原币金额。"""
        rows = rows_from_table(
            [
                ["交易日", "交易描述", "交易金额", "折合人民币"],
                ["08-20", "星巴克", "5.00", "38.50"],
            ],
            year=2026,
        )
        assert rows[0].amount == Decimal("5.00")

    def test_a_missing_card_column_falls_back_to_the_statement(self):
        """整份对账单就一张卡时,行里往往不重复卡号 —— 那时用账单级的那个。
        **卡号是对账匹配的两道判据之一**,空着会让匹配范围无谓地变大。"""
        rows = rows_from_table(
            [["交易日", "交易描述", "交易金额"], ["08-20", "星巴克", "38.50"]],
            year=2026,
            default_account_hint="8888",
        )
        assert rows[0].account_hint == "8888"


class TestOpeningTheFile:
    """唯一碰 PDF 的一组。**测的是报错说不说人话**,不是表格定位。"""

    def test_a_wrong_password_says_it_is_the_password(self):
        data = an_encrypted_pdf(password="123456")

        with pytest.raises(StatementPasswordWrong) as caught:
            open_tables(data, password="654321")

        # 用户唯一能自己修的错误 —— 告警邮件里要看得懂
        assert "密码" in str(caught.value)

    def test_the_right_password_opens_it(self):
        data = an_encrypted_pdf(password="123456")
        assert open_tables(data, password="123456") == []  # 空白页,没有表

    def test_a_file_that_is_not_a_pdf_says_so(self):
        with pytest.raises(StatementUnreadable):
            open_tables("这不是一个 PDF".encode())

    def test_too_many_pages_is_refused(self):
        """解析一份几百页的 PDF 会把这个单进程占住几分钟,别的 job 全在等。"""
        data = an_encrypted_pdf(password=None, pages=5)

        with pytest.raises(StatementUnreadable) as caught:
            open_tables(data, max_pages=2)
        assert "页" in str(caught.value)


def an_encrypted_pdf(*, password: str | None, pages: int = 1) -> bytes:
    """造一份最小的 PDF。**只用来测密码和页数**,不用来测表格定位 ——
    自己造的 PDF 配自己写的定位,证明不了任何事。
    """
    pypdf = pytest.importorskip("pypdf", reason="造加密 PDF 的夹具需要 pypdf(dev 依赖)")

    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if password:
        writer.encrypt(password)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
