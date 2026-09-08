"""信用卡月度对账单 PDF(P2 第 7 片,[ADR-023](../../docs/04-tech-decisions.md))。

**这个模块分成两半,分法是刻意的。**

    open_tables(data, password)   ← 薄。pdfplumber 干活,我只负责密码和报错
    rows_from_table(table)        ← 厚。表格 → 结构,全部逻辑在这里

厚的那一半是**纯函数**:输入是一张表(字符串的二维列表),输出是结构化的行。
它不需要 PDF 就能测,而测出来的结果是可信的。

薄的那一半没法诚实地测。**我手上一份真的对账单都没有**,而用自己造的 PDF
去测自己写的表格定位,只能证明"我造的 PDF 和我写的代码互相匹配" ——
那是[第 4 层复核只看 body](bookkeeper.py) 那种空转的另一个版本。
所以这一半只做一件事:**把 pdfplumber 的失败翻译成看得懂的话**,
剩下的等拿到真账单再说(03 的 2026-09 例外把实测推到了最后)。

## 密码

对账单 PDF 的打开密码通常是身份证后六位或手机号后六位。它是**用户凭据**,
按 [ADR-009](../../docs/04-tech-decisions.md#adr-009--凭据字段级加密从第一天做)
存 `credentials`,不落磁盘、不进日志 —— 这个模块只接收它,不负责存取。

**密码错的报错要说人话。** 这是这条链路最可能出的错(六位数字谁都会记岔),
而 pdfminer 抛的是 `PDFPasswordIncorrect`,直接冒到告警邮件里没人看得懂。
"""

from __future__ import annotations

import io
import logging

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect

from lifein.repos.transactions import Direction, TxnKind
from lifein.sources import statement

log = logging.getLogger(__name__)

MAX_PAGES = 40
"""一份月度对账单最多几页。超过的多半不是对账单,而**解析一份几百页的 PDF
会把这个进程占住几分钟** —— 单进程里那意味着别的 job 全在等。"""


class StatementPasswordWrong(ValueError):
    """打开密码不对。**单独一个类型**,因为它是唯一一个用户自己能修的错误。"""


class StatementUnreadable(ValueError):
    """文件读不开,或者读开了但里面没有像样的表。"""


# 表头认列。**认不出的列就不要**,而不是按位置猜第几列是金额:
# 各家银行的列序不一样,按位置猜的代价是把余额当成消费额记进账本
_HEADERS: dict[str, tuple[str, ...]] = {
    "occurred_on": ("交易日", "交易日期", "消费日期", "交易时间"),
    "posted_on": ("记账日", "记账日期", "入账日期"),
    "merchant": ("交易描述", "商户名称", "交易摘要", "摘要", "商户"),
    "amount": ("交易金额", "金额", "人民币金额", "折合人民币"),
    "account_hint": ("卡号末四位", "卡号后四位", "末四位", "卡号"),
    "order_no": ("交易流水号", "流水号", "订单号", "参考号"),
    "kind": ("交易类型", "类型"),
}




def open_tables(
    data: bytes, *, password: str | None = None, max_pages: int = MAX_PAGES
) -> list[list[list[str]]]:
    """解密并取出所有表格。**薄的那一半** —— pdfplumber 干活,这里只翻译错误。

    返回的是"每页的每张表的每一行的每一格",字符串,可能有 None(空格子),
    统一成空串 —— 下游按列名取值,少一个 `is None` 判断就少一处崩溃点。
    """
    try:
        with pdfplumber.open(io.BytesIO(data), password=password or "") as pdf:
            if len(pdf.pages) > max_pages:
                raise StatementUnreadable(
                    f"这份 PDF 有 {len(pdf.pages)} 页,超过了 {max_pages} 页的上限,"
                    "多半不是月度对账单"
                )
            return [
                [[_cell(cell) for cell in row] for row in table]
                for page in pdf.pages
                for table in (page.extract_tables() or [])
            ]
    except StatementUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_password_error(exc):
            # 唯一一个用户自己能修的错误 —— 说清楚是密码而不是"文件坏了"
            raise StatementPasswordWrong(
                "对账单的打开密码不对。它通常是身份证后六位或手机号后六位,"
                "和邮箱密码不是一回事"
            ) from exc
        raise StatementUnreadable(f"这份 PDF 读不开:{type(exc).__name__}: {exc}") from exc


def _is_password_error(exc: BaseException) -> bool:
    """这是不是"密码不对"。**要往里翻。**

    pdfplumber 把 pdfminer 的异常包了一层:密码错时抛的是
    `PdfminerException(PDFPasswordIncorrect())`,而且**外层的 str() 是空的** ——
    直接把它当成"读不开"报出去,告警邮件里会是一行
    "这份 PDF 读不开:PdfminerException:",而真正的原因用户自己就能修。

    包法将来可能变(直接抛、换个包装类),所以三个地方都看:自己、
    参数里塞的原始异常、`__cause__` 链。
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, PDFPasswordIncorrect):
            return True
        nested = next(
            (arg for arg in getattr(seen, "args", ()) if isinstance(arg, BaseException)),
            None,
        )
        seen = nested or seen.__cause__
    return False


def rows_from_table(
    table: list[list[str]], *, year: int, default_account_hint: str | None = None
) -> list[statement.StatementRow]:
    """把一张表变成结构化的行。**厚的那一半,全部逻辑在这里。**

    `year` 要由调用方给:很多对账单的日期列只有"08-20",没有年份。
    从账单周期推出来的年份比从今天推准 —— 一月收到的是去年十二月的账单。

    **认不出表头就返回空**,不按列的位置猜。各家银行的列序不一样,
    按位置猜的代价是把"余额"当成消费额记进账本,而那种错误在报表上看不出来。
    """
    if len(table) < 2:
        return []

    columns = _map_columns(table[0])
    if "amount" not in columns or "occurred_on" not in columns:
        log.info("这张表认不出交易日或金额列,跳过:%s", table[0])
        return []

    rows: list[statement.StatementRow] = []
    for cells in table[1:]:
        row = _row_from_cells(
            cells, columns, year=year, default_account_hint=default_account_hint
        )
        if row is not None:
            rows.append(row)
    return rows


def _row_from_cells(
    cells: list[str],
    columns: dict[str, int],
    *,
    year: int,
    default_account_hint: str | None,
) -> statement.StatementRow | None:
    amount_text = _at(cells, columns.get("amount"))
    amount = statement.parse_amount(amount_text)
    if amount is None:
        # 表头重复出现、小计行、页脚 —— 金额解不出来就不是一笔交易。
        # **不猜**:猜出来的那一笔会静静地留在账本里
        return None

    occurred_on = statement.parse_date(_at(cells, columns.get("occurred_on")), year=year)
    if occurred_on is None:
        occurred_on = statement.parse_date(_at(cells, columns.get("posted_on")), year=year)
    if occurred_on is None:
        log.info("这一行认不出日期,跳过:%s", cells)
        return None

    merchant = _at(cells, columns.get("merchant")) or None
    kind_text = _at(cells, columns.get("kind"))
    kind = statement.parse_kind(kind_text, merchant)

    value, direction = amount
    if kind is TxnKind.REFUND and direction is Direction.DEBIT:
        # 退货那一行的金额常常写成负数,而符号已经被 `parse_amount` 吃掉了。
        # 类型说是退货就按退货 —— 方向跟着类型走,不跟着符号走
        direction = Direction.CREDIT

    return statement.StatementRow(
        occurred_on=occurred_on,
        amount=value,
        direction=direction,
        merchant_raw=merchant,
        account_hint=statement.card_tail(_at(cells, columns.get("account_hint")))
        or default_account_hint,
        order_no=_at(cells, columns.get("order_no")) or None,
        kind=kind,
        raw_cells=tuple(cells),
    )


def _map_columns(header: list[str]) -> dict[str, int]:
    """表头 → 列号。**一个字段认到第一个就停**,后面同名的列不覆盖它:
    有些对账单同时有"交易金额"和"折合人民币金额",前者才是原币金额。
    """
    columns: dict[str, int] = {}
    for index, cell in enumerate(header):
        text = cell.replace(" ", "")
        for field, names in _HEADERS.items():
            if field not in columns and any(name in text for name in names):
                columns[field] = index
                break
    return columns


def _at(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]


def _cell(cell: object) -> str:
    return "" if cell is None else str(cell).replace("\n", " ").strip()
