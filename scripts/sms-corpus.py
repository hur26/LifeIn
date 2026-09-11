"""把一批真实短信变成本地回归语料。**产物永远不进版本库。**

    adb shell content query --uri content://sms/inbox --projection 'address:body:date' > dump.txt
    python scripts/sms-corpus.py dump.txt

产物是 `evals/sms_corpus.jsonl`,被 `.gitignore` 挡着(`evals/*.jsonl`,
只有 `*.example.jsonl` 进库)—— 和评测集那批真实邮件与群消息同一个待遇,
理由也一样:**它是 R12 同级的敏感数据**。

## 为什么要有它

[AGENTS §9](../AGENTS.md) 那句话已经应验过两次:

> 真实数据比构造的用例值钱得多。前面那些用例是照着规则写的,
> 所以规则漏掉什么,用例也漏掉什么。

2026-09-11 拿 5042 条历史短信跑了一遍,一次打出三个洞,而它们全都活过了
1349 条用例:金额上四位就被截断(这批语料里 **69 条**会被记错)、
进账被记成支出、"账户"后面的卡号没打码就送去了外部模型。

三个洞的共同点是**用例挑的样本太规整**:金额都在三位以内、方向都是消费、
卡号都写作"储蓄卡"。

## 它和 `tests/test_real_sms.py` 分工不同

- 那边是**几条选出来的样本**,进版本库,钉住具体的 bug
- 这边是**一整批真实语料**,不进版本库,跑的是[不变量](../tests/test_sms_corpus.py):
  "抠出来的金额后面不能紧跟数字"这类判据,不需要给每条标答案

没有这个文件时 `tests/test_sms_corpus.py` 整组跳过,所以日常 `pytest`
和 CI 都不依赖它 —— 和 `TEST_DATABASE_URL` 那套是同一个路子。

## 取语料这件事本身

`adb shell content query` 是**在手机里**执行的,走的是 shell 身份,
和 App 无关 —— 手机上那个 App 依然没有 `READ_SMS`
([ADR-010](../docs/04-tech-decisions.md) 定死只走通知监听)。
**这是开发机取测试语料,不是一条采集通路。**
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "sms_corpus.jsonl"

ROW = re.compile(r"^Row: (\d+) address=(.*?), body=(.*), date=(\d+)$", re.S)


def records(text: str):
    """`content query` 一行一条,但正文里有换行,所以按 `Row:` 切。"""
    blob: list[str] = []
    for line in text.splitlines():
        if line.startswith("Row: ") and blob:
            yield "\n".join(blob)
            blob = [line]
        else:
            blob.append(line)
    if blob:
        yield "\n".join(blob)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    source = Path(argv[1])
    if not source.exists():
        print(f"找不到 {source}", file=sys.stderr)
        return 1

    rows = []
    for blob in records(source.read_text(encoding="utf-8", errors="replace")):
        found = ROW.match(blob.strip())
        if not found:
            continue
        _idx, address, body, date = found.groups()
        body = body.strip()
        if body:
            rows.append({"address": address.strip(), "body": body, "date": int(date)})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"{len(rows)} 条写到 {OUT.relative_to(ROOT)}")
    print("它不进版本库(.gitignore 的 evals/*.jsonl)。跑用例:pytest tests/test_sms_corpus.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
