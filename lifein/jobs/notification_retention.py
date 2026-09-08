"""通知原文的保留期 —— [R10](../../docs/05-risks.md#r10--手机端采集器的越权读取) 的最后一条。

那条风险里写着:**群消息只保留摘要,不留存原文,且摘要过期即删。**
理由不是隐私洁癖,是**那不是你的隐私**:群友没有同意过自己说的话
被送进你的服务器,更没同意过被永久存档。

做不到"完全不留存",原因写清楚:

- 归一化失败要能按同一个键重跑(06 §1.4),重跑要有原文
- 提取 agent 按窗口读 `raw_events`,而窗口补偿最多三天(`job_runs`)

所以落地形态是**保留期**:`NOTIFICATION_RETENTION_DAYS` 天之后清掉正文,
只留元信息(来源、时间、群名或联系人名)。抽出来的事实和待办不受影响 ——
它们本来就只是结论,`provenance` 指向的那一行也还在,只是那行的正文空了。

**清掉的是"现在还留着",不是"曾经收到过"。** 行不删除,`raw` 里留一个
`text_redacted` 标记 —— 否则"这条通知本来就没正文"和"正文被清了"分不开。

剩下的暴露要说清:那 N 天里群友的原话确实在库里。这是明确接受的代价,
**P4 前必须重新评估**(那时不会要求朋友装采集器,所以只涉及你自己的群)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.sources.notification import SOURCE

log = logging.getLogger(__name__)

JOB_NAME = "notification_retention"

_REDACT = text("""
    UPDATE raw_events
       SET raw = (raw - 'text') || jsonb_build_object('text_redacted', true),
           normalized = CASE
               WHEN normalized IS NULL THEN NULL
               ELSE jsonb_set(
                        jsonb_set(normalized, '{body}', 'null'::jsonb),
                        '{flags}',
                        COALESCE(normalized -> 'flags', '[]'::jsonb) || '["partial"]'::jsonb
                    )
           END
     WHERE user_id = :user_id
       AND source = :source
       AND occurred_at < :cutoff
       AND raw ? 'text'
""")


def run_once(user_id: str, session: Session, *, now: datetime, retention_days: int) -> int:
    """清掉过了保留期的通知正文。返回处理条数。

    **只动 `source='notification'` 的行。** 邮件留着是另一回事:那是你自己
    收件箱里的东西,而这条规矩管的是别人的话。

    `raw ? 'text'` 那个条件让它天然幂等 —— 清过的不会被再清一遍,
    也就不会重复往 flags 里追加。
    """
    cutoff = now - timedelta(days=retention_days)
    count = session.execute(
        _REDACT, {"user_id": user_id, "source": SOURCE, "cutoff": cutoff}
    ).rowcount

    if count:
        # 只记条数。记下清掉了什么,等于把刚删掉的东西抄进日志
        log.info("清掉 %d 条过期通知的正文,截止 %s", count, cutoff.isoformat())
    return int(count)
