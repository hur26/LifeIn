"""支付宝 / 微信账单导出(P2 第 8 片,[ADR-023](../../docs/04-tech-decisions.md))。

和 PDF 那一片同样的分法:

    open_rows(data, password)   ← 薄。解压 + 解码 + 切行
    rows_from_rows(rows, ...)   ← 厚。行 → 结构,全部逻辑在这里

**这一片比 PDF 那片容易验。** CSV 的形状我记得住,不像表格定位那样非要
真文件不可 —— 所以这里的测试是拿真格式的样子写的,而不是自己造一个
刚好能过的形状。

## 三件必须容错的事

**一、前面有一段废话。** 两家的导出都在真正的表头前面塞十几行说明
("支付宝交易记录明细查询""导出时间"这类)。所以不能假设第一行是表头,
要**从上往下找第一行看起来像表头的**。

**二、后面还有一段废话。** 支付宝在数据后面跟一行 `----------` 加统计,
微信没有。碰上解不出金额的行就跳过,天然把它们吃掉了。

**三、编码多半是 GBK。** 用 UTF-8 硬读会在第一个中文上炸。按 GBK →
UTF-8-sig → UTF-8 的顺序试,**都不行就报错而不是用 errors="replace"** ——
替换出来的乱码商户名会变成规则表里一条永远匹配不上的规则。

## 压缩包

标准库的 `zipfile` 只认传统 ZipCrypto,不认 WinZip AES(ADR-023 明说这是个赌)。
不够用时症状是明确的,那时引 `pyzipper` 一行就够 —— 所以这里把两种失败
分开报,让"密码错了"和"加密方式不支持"在告警里长得不一样。
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import zipfile
from dataclasses import dataclass

from lifein.repos.transactions import Direction, TxnKind
from lifein.sources import statement

log = logging.getLogger(__name__)

MAX_ROWS = 20000
"""一次最多读多少行。一个月的账单几百行,两万行说明拿错文件了 ——
而把几十万行读进内存会把这个单进程撑爆。"""

ENCODINGS = ("gbk", "utf-8-sig", "utf-8")
"""按这个顺序试。**GBK 在前**:两家导出的默认编码都是它,
而 UTF-8 读 GBK 不一定报错,可能读出一串看着像字的乱码。"""


class ExportPasswordWrong(ValueError):
    """压缩包密码不对。**和"加密方式不支持"分开** —— 前者用户自己能修。"""


class ExportEncryptionUnsupported(ValueError):
    """WinZip AES 之类,标准库解不了。**这是 ADR-023 那个赌输了的信号**,
    到这一步就该引 `pyzipper` 了,而不是让用户去想办法。"""


class ExportUnreadable(ValueError):
    """压缩包或 CSV 读不开。"""


@dataclass(frozen=True)
class ExportSource:
    """一份导出是从哪儿来的。**支付宝和微信的列名不一样**,认列时要分开。"""

    name: str
    columns: dict[str, tuple[str, ...]]
    """字段 → 可能的列名。**"direction" 那一项("收/支")是这两家独有的**,
    信用卡对账单没有 —— 有它就别去猜方向,它比金额符号硬。"""


ALIPAY = ExportSource(
    name="alipay",
    columns={
        "occurred_on": ("交易时间", "交易创建时间", "付款时间"),
        "merchant": ("交易对方", "对方名称", "商品名称", "商品说明"),
        "amount": ("金额", "金额(元)", "订单金额"),
        "order_no": ("交易订单号", "交易号", "商家订单号"),
        "kind": ("交易分类", "交易类型", "交易状态"),
        "account_hint": ("收/付款方式", "付款方式", "支付方式"),
        "direction": ("收/支",),
    },
)

WECHAT = ExportSource(
    name="wechat",
    columns={
        "occurred_on": ("交易时间",),
        "merchant": ("交易对方", "商品"),
        "amount": ("金额(元)", "金额"),
        "order_no": ("交易单号", "商户单号"),
        "kind": ("交易类型", "当前状态"),
        "account_hint": ("支付方式",),
        "direction": ("收/支",),
    },
)

SOURCES = (ALIPAY, WECHAT)

_INBOUND = ("收入", "已收入")
_OUTBOUND = ("支出", "已支出")
_NEUTRAL = ("不计收支", "/")
_OUTBOUND_KINDS = (TxnKind.EXPENSE, TxnKind.REPAYMENT)
_INBOUND_KINDS = (TxnKind.INCOME, TxnKind.REFUND)
"""转账不在任何一边:它两个方向都成立,所以永远不算矛盾。"""
"""**"不计收支"要单独认。** 两家都用它标转账、余额宝存取这类
"钱换了个地方"的记录,而把它们记成支出会让月度支出翻倍。"""


def open_rows(
    data: bytes, *, password: str | None = None, max_rows: int = MAX_ROWS
) -> list[list[str]]:
    """解压(如果是压缩包)、解码、切成行。**薄的那一半。**

    传进来的可以是 `.zip` 也可以是裸 `.csv` —— 微信有时直接发 CSV,
    而调用方去分辨文件类型只会让每个调用点各写一次同样的判断。
    """
    payload = _unzip(data, password) if _looks_like_zip(data) else data
    text = _decode(payload)

    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) > max_rows:
        raise ExportUnreadable(
            f"这份导出有 {len(rows)} 行,超过了 {max_rows} 行的上限,多半拿错文件了"
        )
    return [[cell.strip() for cell in row] for row in rows]


def detect_source(rows: list[list[str]]) -> tuple[ExportSource, int] | None:
    """认出是哪家的导出,以及表头在第几行。**认不出返回 None。**

    从上往下找第一行能当表头的:两家都在真正的表头前面塞十几行说明,
    所以不能假设第一行是表头。

    **哪一家不能靠固定字眼判,要比谁认得出的列多。** 我第一版给微信定的字眼
    是"交易时间"+"收/支",以为支付宝没有第二个 —— 现在的支付宝导出也有,
    于是支付宝的账单被当成微信的,"收/付款方式"和"交易分类"两列直接丢了,
    卡号和类型全空。两家的列名重叠得比想象中多,而重叠的部分永远在变,
    所以判据只能是**整行认下来谁认得更全**。

    并列时按 `SOURCES` 的先后 —— 真并列说明这一行两家都能认,那时认成谁
    结果都一样。
    """
    for index, row in enumerate(rows):
        best: tuple[int, ExportSource] | None = None
        for source in SOURCES:
            columns = _map_columns(row, source)
            if "amount" not in columns or "occurred_on" not in columns:
                continue
            if best is None or len(columns) > best[0]:
                best = (len(columns), source)
        if best is not None:
            return best[1], index
    return None


def rows_from_rows(
    rows: list[list[str]], *, year: int, source: ExportSource | None = None
) -> list[statement.StatementRow]:
    """把切好的行变成结构。**厚的那一半,全部逻辑在这里。**

    `year` 只在日期列没有年份时用得上。两家的导出都带完整年份,
    所以这里基本用不到它 —— 留着是为了和 PDF 那一片同一个签名,
    调用方不用记"哪一片要传年份"。
    """
    detected = detect_source(rows) if source is None else (source, _header_index(rows, source))
    if detected is None:
        log.info("这份导出认不出是哪一家,不解析")
        return []

    found, header_at = detected
    if header_at < 0:
        return []

    columns = _map_columns(rows[header_at], found)
    if "amount" not in columns or "occurred_on" not in columns:
        log.info("认不出金额或时间列,不解析:%s", rows[header_at])
        return []

    parsed: list[statement.StatementRow] = []
    for cells in rows[header_at + 1 :]:
        row = _row_from_cells(cells, columns, year=year)
        if row is not None:
            parsed.append(row)
    return parsed


def _row_from_cells(
    cells: list[str], columns: dict[str, int], *, year: int
) -> statement.StatementRow | None:
    amount = statement.parse_amount(_at(cells, columns.get("amount")))
    if amount is None:
        # 页脚("----------"、"共 N 笔记录")和空行。解不出金额就不是一笔交易,
        # **不猜** —— 猜出来的那一笔会静静地留在账本里
        return None

    occurred_on = statement.parse_date(_at(cells, columns.get("occurred_on")), year=year)
    if occurred_on is None:
        return None

    direction_text = _at(cells, columns.get("direction"))
    if _is_neutral(direction_text):
        # **"不计收支"** —— 转账、余额宝存取这类"钱换了个地方"的。
        # 记成支出会让月度支出翻倍,而那正是这一列存在的理由
        return None

    merchant = _at(cells, columns.get("merchant")) or None
    kind_text = _at(cells, columns.get("kind"))
    value, direction = amount
    direction = _direction(direction_text) or direction

    return statement.StatementRow(
        occurred_on=occurred_on,
        amount=value,
        direction=direction,
        merchant_raw=merchant,
        account_hint=statement.card_tail(_at(cells, columns.get("account_hint"))),
        order_no=_at(cells, columns.get("order_no")) or None,
        kind=_kind(kind_text, merchant, direction_text, direction),
        raw_cells=tuple(cells),
    )


def _kind(
    kind_text: str, merchant: str | None, direction_text: str, direction: Direction
) -> TxnKind | None:
    """认类型。**字眼定细粒度,"收/支"那一列有否决权。**

    两样各有各的强处,所以不是简单的谁先谁后:

    - 字眼更细。"收/支"只说钱进还是出,而"退款"和"工资"都是进 ——
      要分开只能靠字眼
    - **那一列更硬。** 它是导出格式自己填的,不是我们从文字里猜的。
      所以字眼推出来的类型如果和它矛盾(它说支出,字眼说退款),
      **两个都不采信**,返回 None 让这一笔走待确认

    进账但字眼看不出是工资还是退款时也返回 None。**不兜底成 income** ——
    认错的话月度收入会多一笔,而那种数字没人会去质疑。
    """
    outbound = any(marker in direction_text for marker in _OUTBOUND)
    inbound = any(marker in direction_text for marker in _INBOUND)

    by_words = statement.parse_kind(kind_text, merchant)
    if by_words is not None:
        if outbound and by_words in _INBOUND_KINDS:
            log.info("收/支说支出但字眼说 %s,矛盾,不认:%s", by_words, kind_text)
            return None
        if inbound and by_words in _OUTBOUND_KINDS:
            log.info("收/支说收入但字眼说 %s,矛盾,不认:%s", by_words, kind_text)
            return None
        return by_words

    if outbound:
        return TxnKind.EXPENSE
    if inbound:
        return None
    # 没有"收/支"那一列(老版本的导出)。只剩金额符号能用
    return TxnKind.EXPENSE if direction is Direction.DEBIT else None


def _direction(text: str) -> Direction | None:
    if any(marker in text for marker in _OUTBOUND):
        return Direction.DEBIT
    if any(marker in text for marker in _INBOUND):
        return Direction.CREDIT
    return None


def _is_neutral(text: str) -> bool:
    return bool(text) and any(marker == text or marker in text for marker in _NEUTRAL)


def _map_columns(header: list[str], source: ExportSource) -> dict[str, int]:
    """表头 → 列号。**一个字段认到第一个就停**,后面同名的不覆盖它。"""
    columns: dict[str, int] = {}
    for index, cell in enumerate(header):
        text = cell.replace(" ", "")
        for field, names in source.columns.items():
            if field not in columns and any(name in text for name in names):
                columns[field] = index
                break
    return columns


def _header_index(rows: list[list[str]], source: ExportSource) -> int:
    """调用方指定了是哪一家时,找它的表头在第几行。"""
    for index, row in enumerate(rows):
        columns = _map_columns(row, source)
        if "amount" in columns and "occurred_on" in columns:
            return index
    return -1


def _looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _unzip(data: bytes, password: str | None) -> bytes:
    """解压。**只取里面第一个 CSV**,不管别的 —— 两家的包里就一个数据文件,
    而"取哪一个"这种事一旦要猜,就会在某个月猜错。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ExportUnreadable(
                    f"压缩包里没有 CSV,只有:{', '.join(archive.namelist()) or '(空)'}"
                )
            return archive.read(names[0], pwd=password.encode() if password else None)
    except ExportUnreadable:
        raise
    except NotImplementedError as exc:
        # 标准库遇到 WinZip AES 抛的就是它。**ADR-023 那个赌输了的信号**
        raise ExportEncryptionUnsupported(
            "这个压缩包用的加密方式标准库解不了(多半是 WinZip AES)。"
            "按 ADR-023 的触发条件,这时候该引 pyzipper 了"
        ) from exc
    except RuntimeError as exc:
        if "password" in str(exc).lower() or "Bad password" in str(exc):
            raise ExportPasswordWrong(
                "压缩包的密码不对。支付宝和微信的导出密码是你申请导出时自己设的,"
                "和登录密码不是一回事"
            ) from exc
        raise ExportUnreadable(f"压缩包读不开:{exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ExportUnreadable(f"这不是一个能读的压缩包:{exc}") from exc


def _decode(payload: bytes) -> str:
    """按 GBK → UTF-8-sig → UTF-8 试。**都不行就报错,不用 errors="replace"。**

    替换出来的乱码商户名会变成规则表里一条永远匹配不上的规则,
    而它看起来和一条正常规则没有区别 —— 那比读不出来糟得多。
    """
    for encoding in ENCODINGS:
        try:
            return codecs.decode(payload, encoding)
        except UnicodeDecodeError:
            continue
    raise ExportUnreadable(f"这份导出用 {'、'.join(ENCODINGS)} 都解不开")


def _at(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]
