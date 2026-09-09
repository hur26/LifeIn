"""`job_runs` 的读写 —— 定时任务补偿的依据。

ADR-016:补偿"靠数据库记录上次执行窗口实现,**不依赖调度器自身的持久化**"。
这样换调度器时不会丢补偿能力,而 APScheduler 在单机自托管下最常见的失效
就是进程被 kill,内存里的调度状态一起没了。

三条规则:

1. **窗口是幂等键,但"跑过"不等于"跑成了"。** 认领是一条带条件的
   `ON CONFLICT DO UPDATE`:成功的窗口不会被再认领,**失败的会**(至多三次)。
   这一条原来写的是 `DO NOTHING`,而那样"失败"对下一次运行没有任何影响 ——
   见下方那一段,它是真踩出来的。
2. **`running` 的记录不清理,但六小时之后不算数。** 进程被 kill 时它会留在
   那里,而"上一次跑了一半"和"从来没跑过"是两种不同的状态,分不清就没法
   安全补偿。行留着,`attempts` 记着它被认领过几次 —— 但不再拿它当借口不干活。
3. **补偿有上限,重试也有上限。** 停机两个月,重启时不该往外发六十条摘要;
   一个永远失败的窗口也不该把后面每一天的额度都吃掉。

## 那个洞:失败的窗口从此消失

原来的两条规则单看都对,合起来是一个洞:

1. 周一的窗口失败了 —— `status='failed'`,那一天的事件一条都没处理
2. 周二再跑,`windows_to_run` 从周日的成功点往后切,确实切出了周一
3. 但 `claim_window` 撞上周一那行,`DO NOTHING`,返回"已经跑过",跳过
4. 周二成功,成功点推进到周二 —— **周一从此再也不会被切出来**

丢的是那一整天的记账、日程、记忆。**而它不报错**:日志里只有一行早已被
滚掉的 WARNING,账本上只是"这天没花钱"。

重跑之所以安全,靠的**不是**这张表,是下游的幂等键:`transactions` 上的
`UNIQUE (user_id, source_event_id)`、`todos` 与 `facts` 同理。整窗重跑只会
把已经处理过的那些判成 duplicate。**加一个新 job 时先确认这一条成立**,
否则重试机制会从"救回一天"变成"记两遍"。
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

MAX_ATTEMPTS = 3
"""同一个窗口最多认领几次(首次 + 两次重试)。

**上限存在的理由和补偿上限是同一个**:一个每次都失败的窗口如果无限重试,
会把 `MAX_RETRY_WINDOWS` 的名额长期占住,后面真正该救的那些反而进不来。
三次之后行留在库里(`status='failed'`,`attempts=3`),不删也不假装成功。
"""

MAX_RETRY_WINDOWS = 3
"""一次最多重跑几个旧窗口。和 `MAX_CATCHUP_WINDOWS` 分开计数:
补偿是"往前追进度",重试是"回头补窟窿",两者一起最多六个窗口。

合成一个数的话,一串失败会把补偿名额吃光 —— 于是进度永远停在原地,
而那是比丢一天更糟的状态。
"""

STALE_RUNNING_AFTER = timedelta(hours=6)
"""`running` 多久之后认为它已经死了。

不是拍的:这几个 job 里最慢的是月度报告,一次模型调用加一次推送,
分钟量级。跑了六小时的窗口一定是死的,不是慢的。

**这台是单进程自托管**(ADR-016),不存在"另一个副本还在跑"的情况;
真有并发时重跑也只是撞上下游的幂等键。
"""

_CLAIM = text("""
    INSERT INTO job_runs
        (user_id, job_name, window_start, window_end, status, attempts, started_at)
    VALUES
        (:user_id, :job_name, :window_start, :window_end, 'running', 1, :now)
    ON CONFLICT (user_id, job_name, window_start) DO UPDATE
       SET status      = 'running',
           window_end  = EXCLUDED.window_end,
           attempts    = job_runs.attempts + 1,
           started_at  = EXCLUDED.started_at,
           finished_at = NULL
     WHERE job_runs.attempts < :max_attempts
       AND (
            job_runs.status = 'failed'
            OR (job_runs.status = 'running' AND job_runs.started_at < :stale_before)
       )
    RETURNING id, attempts
""")
"""认领一个窗口。**判断和写入在同一条语句里。**

先 SELECT 再 UPDATE 的写法在并发下会让两个进程同时认领到同一个窗口 ——
而这里的 `DO UPDATE ... WHERE` 条件不满足时整个冲突动作被跳过,
一行都不返回,调用方直接拿到 False。

`error` 故意不清:重跑期间它是唯一能回答"上次为什么失败"的东西,
而 `finish_window` 结束时会覆盖它。"""

_FINISH = text("""
    UPDATE job_runs
       SET status = :status,
           finished_at = now(),
           error = :error,
           stats = CAST(:stats AS JSONB)
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND window_start = :window_start
    RETURNING attempts
""")

_RETRYABLE = text("""
    SELECT window_start, window_end
      FROM job_runs
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND attempts < :max_attempts
       AND (
            status = 'failed'
            OR (status = 'running' AND started_at < :stale_before)
       )
     ORDER BY window_start
     LIMIT :limit
""")
"""该回头重跑的窗口。

**必须单独查,不能指望往前切的那一遍切出来**:失败的窗口一旦被后面某个
成功的窗口越过,成功点就推到它后面去了,而往前切只从成功点开始。"""

_LAST_SUCCESS = text("""
    SELECT window_end
      FROM job_runs
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND status = 'succeeded'
     ORDER BY window_end DESC
     LIMIT 1
""")


_STATS_FOR = text("""
    SELECT stats FROM job_runs
     WHERE user_id = :user_id
       AND job_name = :job_name
       AND status = 'succeeded'
       AND to_char(window_start, 'YYYY-MM') = :period
     ORDER BY window_start DESC
     LIMIT 1
""")

def claim_window(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    max_attempts: int = MAX_ATTEMPTS,
) -> bool:
    """认领一个执行窗口。**拿不到就返回 False,拿不到有三种原因。**

    | 库里那行的状态 | 结果 |
    | --- | --- |
    | 不存在 | 认领成功,`attempts=1` |
    | `succeeded` | False —— 干过的活不重复干 |
    | `failed` 且 `attempts < max_attempts` | **认领成功**,`attempts` 加一 |
    | `failed` 且次数用完 | False,而且这一天永久放弃了 |
    | `running` 且不到六小时 | False —— 当它真的在跑 |
    | `running` 且超过六小时 | **认领成功** —— 那个进程已经死了 |

    第三行是这个函数存在的理由:原来它是 `DO NOTHING`,失败的窗口和成功的
    窗口对下一次运行毫无区别(见模块开头)。
    """
    row = session.execute(
        _CLAIM,
        {
            "user_id": user_id,
            "job_name": job_name,
            "window_start": window_start,
            "window_end": window_end,
            "now": now,
            "max_attempts": max_attempts,
            "stale_before": now - STALE_RUNNING_AFTER,
        },
    ).first()
    if row is None:
        log.info("窗口 %s 拿不到(已成功、在跑、或重试次数用完),跳过", window_start.isoformat())
        return False
    if row.attempts > 1:
        log.warning(
            "窗口 %s 第 %d 次认领 —— 上一次没跑成", window_start.isoformat(), row.attempts
        )
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
    row = session.execute(
        _FINISH,
        {
            "user_id": user_id,
            "job_name": job_name,
            "window_start": window_start,
            "status": status,
            "error": error,
            "stats": json.dumps(dict(stats or {}), ensure_ascii=False, default=str),
        },
    ).first()

    # **放弃的那一刻要在日志里留一行,而且只留一行。**
    # 换成"每次运行数一遍还剩几个没救回来的"会天天刷同一条,
    # 而天天出现的告警等于没有告警
    if row is not None and status == "failed" and row.attempts >= MAX_ATTEMPTS:
        log.error(
            "%s 的窗口 %s 失败 %d 次,不再重跑 —— 这一窗的事件永久不会被处理:%s",
            job_name,
            window_start.isoformat(),
            row.attempts,
            error,
        )



def stats_for(
    user_id: str, session: Session, *, job_name: str, period: str
) -> dict[str, Any] | None:
    """取某个月那一次成功运行留下的 `stats`。**没跑过就是 None。**

    App 的月度报表读它而不是现算(06 §6.11):现调一次模型既慢又贵,
    而**同一个月的评语每次点开都不一样,会让人以为数字也在变**。
    """
    row = session.execute(
        _STATS_FOR, {"user_id": user_id, "job_name": job_name, "period": period}
    ).first()
    return dict(row.stats) if row and row.stats else None


def last_successful_window_end(user_id: str, session: Session, *, job_name: str) -> datetime | None:
    return session.execute(_LAST_SUCCESS, {"user_id": user_id, "job_name": job_name}).scalar()


def retryable_windows(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    now: datetime,
    max_attempts: int = MAX_ATTEMPTS,
    limit: int = MAX_RETRY_WINDOWS,
) -> list[tuple[datetime, datetime]]:
    """回头补窟窿:失败过、或者卡在 `running` 早就死掉的那些窗口。

    **要单独查。** 往前切的那一遍从"最后一个成功窗口"开始,而一个失败的
    窗口一旦被后面某个成功的窗口越过,成功点就已经在它后面了 ——
    往前切永远切不到它。
    """
    rows = session.execute(
        _RETRYABLE,
        {
            "user_id": user_id,
            "job_name": job_name,
            "max_attempts": max_attempts,
            "stale_before": now - STALE_RUNNING_AFTER,
            "limit": limit,
        },
    ).all()
    return [(row.window_start, row.window_end) for row in rows]


def windows_to_run(
    user_id: str,
    session: Session,
    *,
    job_name: str,
    now: datetime,
    length: timedelta = timedelta(days=1),
) -> list[tuple[datetime, datetime]]:
    """算出该跑哪些窗口。**两部分相加:回头补的 + 往前追的。**

    第一次跑(没有任何成功记录)只跑当前这一个窗口 —— 不去回溯历史,
    那既慢又会把一堆旧邮件当成"今天的事"推给你。

    两部分各有各的上限(`MAX_RETRY_WINDOWS` 与 `MAX_CATCHUP_WINDOWS`),
    不共用一个:共用的话一串失败会把往前追的名额吃光,于是进度永远停在
    原地 —— 那比丢一天更糟。
    """
    windows: list[tuple[datetime, datetime]] = list(
        retryable_windows(user_id, session, job_name=job_name, now=now)
    )
    if windows:
        log.info("有 %d 个旧窗口要重跑:%s", len(windows), windows[0][0].isoformat())

    last_end = last_successful_window_end(user_id, session, job_name=job_name)

    if last_end is None:
        forward = [(now - length, now)]
        start = now
    else:
        forward = []
        start = last_end
        while start < now and len(forward) < MAX_CATCHUP_WINDOWS:
            end = min(start + length, now)
            forward.append((start, end))
            start = end

    if start < now:
        # 补不动的不假装补了 —— 日志里要能看出来断了多久
        log.warning(
            "停机时间超过 %d 个窗口,%s 之前的不补",
            MAX_CATCHUP_WINDOWS,
            start.isoformat(),
        )

    # 去重按 window_start:重跑清单和往前追的清单会重叠(失败的那个窗口
    # 恰好还在成功点后面时)。重复认领本身是安全的(第二次拿不到),
    # 但那会白跑一遍查询,而且日志里会出现看不懂的"已认领"
    seen = {ws for ws, _ in windows}
    windows += [(ws, we) for ws, we in forward if ws not in seen]
    windows.sort()
    return windows
