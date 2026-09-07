"""`job_runs` 的读写 —— 定时任务补偿的依据。

ADR-016:补偿"靠数据库记录上次执行窗口实现,**不依赖调度器自身的持久化**"。
这样换调度器时不会丢补偿能力,而 APScheduler 在单机自托管下最常见的失效
就是进程被 kill,内存里的调度状态一起没了。

三条规则:

1. **窗口是幂等键。** `claim_window` 走 `ON CONFLICT DO NOTHING`,同一个
   窗口第二次认领会拿到 False。补几次都是同一个结果。
2. **`running` 的记录不清理。** 进程被 kill 时它会留在那里,而"上一次跑了
   一半"和"从来没跑过"是两种不同的状态,分不清就没法安全补偿。
3. **补偿有上限。** 停机两个月,重启时不该往外发六十条摘要 —— 那比不发更糟。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

MAX_CATCHUP_WINDOWS = 3
"""最多补几个窗口。

选 3 不是算出来的:停机一两天补回来有意义,停机两个月的那些摘要早就过期了,
补出来只是六十条噪音。补不动的那些留在库里没有记录,查得到。
"""

_CLAIM = text("""
    INSERT INTO job_runs (user_id, job_name, window_start, window_end, status)
    VALUES (:user_id, :job_name, :window_start, :window_end, 'running')
    ON CONFLICT (user_id, job_name, window_start) DO NOTHING
    RETURNING id
""")

_FINISH = text("""
    UPDATE job_runs
       SET status = :status,
           finished_at = now(),
           error = :error,
           stats = CAST(:stats AS JSONB)
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND window_start = :window_start
""")

_LAST_SUCCESS = text("""
    SELECT window_end
      FROM job_runs
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND status = 'succeeded'
     ORDER BY window_end DESC
     LIMIT 1
""")


def claim_window(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """认领一个执行窗口。已经被认领过就返回 False。"""
    row = session.execute(
        _CLAIM,
        {
            "user_id": user_id,
            "job_name": job_name,
            "window_start": window_start,
            "window_end": window_end,
        },
    ).first()
    if row is None:
        log.info("窗口 %s 已被认领过,跳过", window_start.isoformat())
        return False
    return True


def finish_window(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    window_start: datetime,
    status: str,
    stats: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if status not in {"succeeded", "failed"}:
        raise ValueError(f"status 只能是 succeeded 或 failed,给的是 {status!r}")
    session.execute(
        _FINISH,
        {
            "user_id": user_id,
            "job_name": job_name,
            "window_start": window_start,
            "status": status,
            "error": error,
            "stats": json.dumps(dict(stats or {}), ensure_ascii=False, default=str),
        },
    )


def last_successful_window_end(user_id: str, session: Session, *, job_name: str) -> datetime | None:
    return session.execute(_LAST_SUCCESS, {"user_id": user_id, "job_name": job_name}).scalar()


def windows_to_run(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    now: datetime,
    length: timedelta = timedelta(days=1),
) -> list[tuple[datetime, datetime]]:
    """算出该跑哪些窗口。

    第一次跑(没有任何成功记录)只跑当前这一个窗口 —— 不去回溯历史,
    那既慢又会把一堆旧邮件当成"今天的事"推给你。
    """
    last_end = last_successful_window_end(user_id, session, job_name=job_name)
    current_start = now - length

    if last_end is None:
        return [(current_start, now)]

    windows: list[tuple[datetime, datetime]] = []
    start = last_end
    while start < now and len(windows) < MAX_CATCHUP_WINDOWS:
        end = min(start + length, now)
        windows.append((start, end))
        start = end

    skipped = start < now
    if skipped:
        # 补不动的不假装补了 —— 日志里要能看出来断了多久
        log.warning(
            "停机时间超过 %d 个窗口,%s 之前的不补",
            MAX_CATCHUP_WINDOWS,
            start.isoformat(),
        )
    return windows
