"""支付宝 / 微信账单导出(P2 第 8 片)。

**这一片比 PDF 那片验得实。** CSV 的形状是固定的文字,不像表格定位那样
非要真文件不可 —— 所以这里的夹具是照两家导出的真样子写的:前面十几行说明、
后面的统计尾巴、GBK 编码、"收/支"那一列,一样不少。

这一组盯的还是那件事:**账本上不该多出任何一笔**。所以用例大半在
"这一行不该变成交易":页脚、不计收支、方向和字眼互相矛盾的行。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from lifein.repos.transactions import Direction, TxnKind
from lifein.sources.statement_csv import (
    ALIPAY,
    WECHAT,
    ExportEncryptionUnsupported,
    ExportPasswordWrong,
    ExportUnreadable,
    detect_source,
    open_rows,
    rows_from_rows,
)

# 支付宝导出的真样子:前面一段说明,表头,数据,然后 ------ 加统计
ALIPAY_CSV = """支付宝交易记录明细查询
账号:[139****8888]
起始日期:[2026-08-01 00:00:00]    终止日期:[2026-08-31 23:59:59]
---------------------------------交易记录明细列表------------------------------------
交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号
2026-08-20 12:30:00,餐饮美食,星巴克,大杯拿铁,支出,38.50,招商银行储蓄卡(1234),交易成功,2026082012345
2026-08-21 09:00:00,转账,余额宝,余额宝-转出,不计收支,1000.00,余额宝,交易成功,2026082100001
2026-08-22 18:00:00,退款,某某旗舰店,退款,收入,99.00,招商银行储蓄卡(1234),退款成功,2026082200002
------------------------------------------------------------------------------------
共 3 笔记录
已收入:1笔 99.00元
"""

# 微信的:说明更短,列名不一样,"收/支"里的取值带"已"
WECHAT_CSV = """微信支付账单明细
微信昵称:[某某]
起始时间:[2026-08-01 00:00:00] 终止时间:[2026-08-31 23:59:59]
----------------------微信支付账单明细列表--------------------
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2026-08-20 12:30:00,商户消费,全家便利店,饮料,支出,￥12.00,零钱,支付成功,42000012345,FJ0001,/
2026-08-25 10:00:00,转账,朋友,/,不计收支,￥200.00,零钱,已转账,42000012346,/,/
"""


def rows(text: str, *, encoding: str = "gbk") -> list[list[str]]:
    return open_rows(text.encode(encoding))


class TestFindingTheHeader:
    """**不能假设第一行是表头。** 两家都在前面塞十几行说明。"""

    def test_alipay(self):
        source, at = detect_source(rows(ALIPAY_CSV))
        assert source is ALIPAY
        assert at == 4  # 前面四行是说明

    def test_wechat(self):
        source, at = detect_source(rows(WECHAT_CSV))
        assert source is WECHAT

    def test_which_one_is_decided_by_who_maps_more_columns(self):
        """**不能靠固定字眼判。** 两家的表头都有"交易时间"和"收/支",
        我第一版就是这么判的,结果支付宝的账单被当成微信的,
        "收/付款方式"和"交易分类"两列直接丢了 —— 卡号和类型全空,
        而账还是照记,只是记得没头没尾。

        判据只能是整行认下来谁认得更全。
        """
        assert detect_source(rows(ALIPAY_CSV))[0] is ALIPAY
        assert detect_source(rows(WECHAT_CSV))[0] is WECHAT

        # 认错了的表现:这两样会变成 None
        alipay = rows_from_rows(rows(ALIPAY_CSV), year=2026)[0]
        assert (alipay.account_hint, alipay.kind) == ("1234", TxnKind.EXPENSE)

    def test_something_that_is_not_a_bill(self):
        assert detect_source([["姓名", "电话"], ["张三", "13800000000"]]) is None
        assert rows_from_rows([["姓名", "电话"]], year=2026) == []


class TestAlipay:
    def parsed(self):
        return rows_from_rows(rows(ALIPAY_CSV), year=2026)

    def test_only_the_real_transactions_come_out(self):
        """**三行数据里只有两行是交易。** 中间那行"不计收支"是转账,
        记成支出会让月度支出凭空多一千。"""
        assert len(self.parsed()) == 2

    def test_an_expense(self):
        expense = self.parsed()[0]

        assert expense.occurred_on == date(2026, 8, 20)
        assert expense.amount == Decimal("38.50")
        assert expense.direction is Direction.DEBIT
        assert expense.merchant_raw == "星巴克"
        assert expense.kind is TxnKind.EXPENSE
        assert expense.order_no == "2026082012345"

    def test_the_card_tail_comes_out_of_the_payment_method(self):
        """"招商银行储蓄卡(1234)" —— **括号在数字后面**,
        而这正是 card_tail 之前取不到的那种写法。"""
        assert self.parsed()[0].account_hint == "1234"

    def test_a_refund_is_inbound_and_is_not_income(self):
        """退款是钱回来,但**不是收入** —— 记成收入会让月度收入虚高。"""
        refund = self.parsed()[1]

        assert refund.direction is Direction.CREDIT
        assert refund.kind is TxnKind.REFUND

    def test_the_footer_is_not_a_transaction(self):
        """"共 3 笔记录""已收入:1笔 99.00元" —— 后面这行里有金额,
        **不挡住的话账本上会多出一笔 99 元的消费**。"""
        assert all("共" not in (row.merchant_raw or "") for row in self.parsed())
        assert [row.amount for row in self.parsed()] == [
            Decimal("38.50"), Decimal("99.00"),
        ]


class TestWechat:
    def test_the_yuan_sign_is_stripped(self):
        (row,) = rows_from_rows(rows(WECHAT_CSV), year=2026)
        assert row.amount == Decimal("12.00")
        assert row.direction is Direction.DEBIT

    def test_a_transfer_marked_neutral_is_dropped(self):
        assert len(rows_from_rows(rows(WECHAT_CSV), year=2026)) == 1


class TestTheDirectionColumnHasVeto:
    """**字眼定细粒度,"收/支"那一列有否决权。**

    那一列是导出格式自己填的,不是我们从文字里猜的。两边矛盾时两个都不采信 ——
    宁可这一笔走待确认,也不要在账本上留一笔方向反了的记录。
    """

    def header(self) -> list[str]:
        return ["交易时间", "交易分类", "交易对方", "商品说明", "收/支", "金额", "收/付款方式"]

    def one(self, *, category: str, direction: str, merchant: str = "某商户"):
        table = [
            self.header(),
            ["2026-08-20 12:00:00", category, merchant, "", direction, "38.50", "零钱"],
        ]
        parsed = rows_from_rows(table, year=2026, source=ALIPAY)
        return parsed[0] if parsed else None

    def test_words_win_when_they_agree(self):
        assert self.one(category="退款", direction="收入").kind is TxnKind.REFUND

    def test_a_contradiction_is_refused(self):
        """那一列说支出,字眼说退款 —— **两个都不认。**"""
        assert self.one(category="退款", direction="支出").kind is None

    def test_inbound_without_a_clue_is_not_income(self):
        """进账但看不出是工资还是退款。**不兜底成 income** ——
        认错的话月度收入会多一笔,而那种数字没人会去质疑。"""
        assert self.one(category="其他", direction="收入").kind is None

    def test_outbound_without_a_clue_is_an_expense(self):
        """出账那一边可以兜底:钱确实出去了,最坏的情况是分类不准。"""
        assert self.one(category="其他", direction="支出").kind is TxnKind.EXPENSE

    def test_a_transfer_never_contradicts(self):
        """转账两个方向都成立,所以它永远不算矛盾。"""
        assert self.one(category="转账", direction="支出").kind is TxnKind.TRANSFER
        assert self.one(category="转账", direction="收入").kind is TxnKind.TRANSFER


class TestOpeningTheFile:
    def test_gbk_is_tried_first(self):
        """两家导出的默认编码都是 GBK。**UTF-8 读 GBK 不一定报错**,
        可能读出一串看着像字的乱码。"""
        parsed = rows_from_rows(rows(ALIPAY_CSV, encoding="gbk"), year=2026)
        assert parsed[0].merchant_raw == "星巴克"

    def test_utf8_with_bom_also_works(self):
        parsed = rows_from_rows(rows(ALIPAY_CSV, encoding="utf-8-sig"), year=2026)
        assert parsed[0].merchant_raw == "星巴克"

    def test_bytes_that_decode_to_nothing_sensible_raise(self):
        """**不用 errors="replace"。** 替换出来的乱码商户名会变成规则表里
        一条永远匹配不上的规则,而它看起来和正常规则没区别。"""
        with pytest.raises(ExportUnreadable):
            open_rows(b"\xff\xfe\x00\x00\xff\xff\xfe\xfe")

    def test_a_zip_is_unpacked(self):
        data = a_zip(ALIPAY_CSV.encode("gbk"))
        parsed = rows_from_rows(open_rows(data), year=2026)
        assert len(parsed) == 2

    def test_a_zip_without_a_csv_says_what_is_in_it(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("说明.txt", "没有账单")

        with pytest.raises(ExportUnreadable) as caught:
            open_rows(buffer.getvalue())
        assert "说明.txt" in str(caught.value)

    def test_too_many_rows_is_refused(self):
        big = "交易时间,收/支,金额(元)\n" + "2026-08-20,支出,1.00\n" * 50
        with pytest.raises(ExportUnreadable):
            open_rows(big.encode("gbk"), max_rows=10)

    def test_a_broken_zip_is_not_a_password_problem(self):
        """报错要分得开:密码错用户自己能修,包坏了不能。"""
        with pytest.raises(ExportUnreadable):
            open_rows(b"PK" + b"not a real zip at all")


def test_a_wrong_password_says_it_is_the_password(monkeypatch):
    """密码错是用户自己能修的那一类,报错要说清楚是密码。

    造不出真的加密压缩包来测(标准库能读 ZipCrypto 但写不了任何加密包,
    pyzipper 写的又是标准库读不了的 AES),所以直接验错误翻译 ——
    那本来也就是这个模块写的代码,"ZipCrypto 解不解得开"是标准库的事。
    """
    fake_zipfile(monkeypatch, RuntimeError("Bad password for file 'bill.csv'"))

    with pytest.raises(ExportPasswordWrong) as caught:
        open_rows(b"PKwhatever", password="654321")
    assert "密码" in str(caught.value)


def test_another_runtime_error_is_not_blamed_on_the_password(monkeypatch):
    """**别把所有 RuntimeError 都说成密码错。** 那会让用户对着一个
    根本不是密码的问题一遍遍试密码,而试多少遍都不会成功。"""
    fake_zipfile(monkeypatch, RuntimeError("File name in directory differs"))

    with pytest.raises(ExportUnreadable):
        open_rows(b"PKwhatever", password="654321")


def test_the_aes_signal_is_its_own_error(monkeypatch):
    """**ADR-023 那个赌输了的信号。**

    标准库只认 ZipCrypto。碰上 WinZip AES 时要报成"该引 pyzipper 了",
    而不是混进"密码不对"里 —— 后者会让人一遍遍去试密码。
    """
    fake_zipfile(monkeypatch, NotImplementedError("compression type 99 (AES)"))

    with pytest.raises(ExportEncryptionUnsupported) as caught:
        open_rows(b"PKwhatever")
    assert "pyzipper" in str(caught.value)


def a_zip(payload: bytes) -> bytes:
    """造一个不带密码的压缩包。

    **带密码的造不出来**:标准库能读 ZipCrypto 但写不了任何加密包,
    而 pyzipper 写的是 AES —— 正好是标准库读不了的那种。所以密码那两条路
    改成直接测我们的错误翻译(见下面两个用例),那本来也就是我写的代码;
    "ZipCrypto 解得开不开"是标准库的事,不是这个模块的事。
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bill.csv", payload)
    return buffer.getvalue()


def fake_zipfile(monkeypatch, error: Exception) -> None:
    """让解压那一步抛指定的异常,用来验错误翻译。"""
    import lifein.sources.statement_csv as module

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def namelist(self):
            return ["bill.csv"]

        def read(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(module.zipfile, "ZipFile", lambda *a, **k: Fake())
