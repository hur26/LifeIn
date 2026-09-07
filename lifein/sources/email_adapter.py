"""邮箱数据源适配器 —— 把 IMAP 收信与归一化接起来。

架构 §9.1 的 PullAdapter 实现。它自己几乎不做事,这是刻意的:
收信的怪脾气在 `imap_client`,解析的脏活在 `email_source`,
适配器只负责"把两边接起来并保证一封坏邮件不会卡住整轮采集"。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from lifein.models.normalized import Trust
from lifein.sources.base import IngestedEvent
from lifein.sources.email_source import SOURCE, normalize_email
from lifein.sources.imap_client import ImapMailbox

log = logging.getLogger(__name__)


class EmailAdapter:
    source = SOURCE

    def __init__(self, mailbox: ImapMailbox) -> None:
        self._mailbox = mailbox

    def fetch(self, since: datetime) -> Iterator[IngestedEvent]:
        for uid, raw in self._mailbox.fetch_raw_since(since):
            received_at = datetime.now(UTC)
            try:
                event = normalize_email(raw, received_at=received_at)
            except Exception as exc:  # noqa: BLE001
                # 归一化里出了没预料到的异常,也要产出一条带 error 的事件。
                # 静默跳过等于这封邮件在系统里从未存在过 —— 06 §1.4 不允许。
                log.exception("归一化抛异常,uid=%s", uid)
                yield IngestedEvent(
                    source=SOURCE,
                    external_id=f"uid:{uid}",
                    occurred_at=received_at,
                    trust=Trust.EXTERNAL,
                    raw={"uid": uid, "size_bytes": len(raw)},
                    normalize_error=f"归一化异常:{exc}",
                )
                continue
            yield event
