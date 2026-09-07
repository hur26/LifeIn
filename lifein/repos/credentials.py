"""`credentials` 的读写 —— 加密在这一层完成,上面的代码永远拿明文。

ADR-009 要求凭据存库前加密。**加密动作放在仓储里而不是调用方**,理由很简单:
放在调用方就意味着"每个写凭据的地方都要记得加密",而只要有一处忘了,
库里就有一条明文,且没有任何报错。放在这里,想存明文得先绕过这个模块。

`scope` 是 [R11](../../docs/05-risks.md) 的执行点:采集凭据(`ingest`)
打到读接口一律拒绝。读取时必须说明自己要什么 scope,拿不到就是拿不到 ——
不做"反正是同一个用户的,给了吧"这种通融。

`revoked_at` 不为空的当作不存在。**吊销不删除**:哪条凭据什么时候被吊销的,
是排查"手机丢了之后还有没有人在用"时唯一的线索。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.config import Settings
from lifein.crypto import DecryptError, decrypt, encrypt

log = logging.getLogger(__name__)

_QUERY_SCOPES = ("query", "both")
_INGEST_SCOPES = ("ingest", "both")

_INSERT = text("""
    INSERT INTO credentials (user_id, kind, scope, ciphertext, key_version, device_id)
    VALUES (:user_id, :kind, :scope, :ciphertext, :key_version, :device_id)
    RETURNING id
""")

_SELECT = text("""
    SELECT id, ciphertext
      FROM credentials
     WHERE user_id = :user_id
       AND kind = :kind
       AND scope = ANY(:scopes)
       AND revoked_at IS NULL
     ORDER BY created_at DESC
     LIMIT 1
""")

_REVOKE = text("""
    UPDATE credentials
       SET revoked_at = now()
     WHERE user_id = :user_id
       AND kind = :kind
       AND revoked_at IS NULL
""")

_SELECT_STALE = text("""
    SELECT id, kind, key_version
      FROM credentials
     WHERE user_id = :user_id
       AND revoked_at IS NULL
       AND key_version <> :current
""")


def put_credential(
    user_id: str,
    session: Session,
    *,
    kind: str,
    scope: str,
    payload: dict[str, Any],
    settings: Settings,
    device_id: str | None = None,
) -> str:
    """存一条凭据。`payload` 是明文字典,进库前在这里加密。"""
    if scope not in {"ingest", "query", "both"}:
        raise ValueError(f"scope 只能是 ingest / query / both,给的是 {scope!r}")

    blob = encrypt(
        json.dumps(payload, ensure_ascii=False),
        user_id=user_id,
        kind=kind,
        settings=settings,
    )
    return str(
        session.execute(
            _INSERT,
            {
                "user_id": user_id,
                "kind": kind,
                "scope": scope,
                "ciphertext": blob,
                "key_version": settings.master_key_version,
                "device_id": device_id,
            },
        ).scalar_one()
    )


def get_credential(
    user_id: str,
    session: Session,
    *,
    kind: str,
    settings: Settings,
    for_ingest: bool = False,
) -> dict[str, Any] | None:
    """取一条凭据并解密。没有、或 scope 不符,都返回 None。

    `for_ingest=True` 时只认 `ingest`/`both`;默认只认 `query`/`both`。
    **两边不通融** —— 手机丢了、App 被逆向,拿到的采集密钥读不出任何账本。
    """
    scopes = list(_INGEST_SCOPES if for_ingest else _QUERY_SCOPES)
    row = session.execute(_SELECT, {"user_id": user_id, "kind": kind, "scopes": scopes}).first()
    if row is None:
        return None

    try:
        plain = decrypt(bytes(row.ciphertext), user_id=user_id, kind=kind, settings=settings)
    except DecryptError:
        # 解不开比没有更严重:多半是换了主密钥没重加密,或者密文被动过。
        # 不静默返回 None —— 那会让调用方以为"还没配过"而去引导用户重配。
        log.exception("凭据 %s 解密失败,id=%s", kind, row.id)
        raise

    return json.loads(plain)


def revoke_credential(user_id: str, session: Session, *, kind: str) -> int:
    """吊销某一类凭据。返回受影响的条数。记录保留,不删除。"""
    return int(session.execute(_REVOKE, {"user_id": user_id, "kind": kind}).rowcount)


def stale_key_versions(
    user_id: str, session: Session, *, settings: Settings
) -> list[tuple[str, str, int]]:
    """列出还停在旧主密钥上的凭据 (id, kind, key_version)。

    密钥轮换的收尾步骤是"确认无残留旧版本"(07 §2.2),这就是那个确认动作。
    没有它,轮换只能靠"应该都换完了吧"。
    """
    rows = session.execute(
        _SELECT_STALE, {"user_id": user_id, "current": settings.master_key_version}
    ).all()
    return [(str(r.id), r.kind, r.key_version) for r in rows]


def rotate_credentials(
    user_id: str, session: Session, *, settings: Settings, now: datetime | None = None
) -> int:
    """把旧密钥加密的凭据逐条重新加密。返回处理条数。

    逐条读出、解密、再加密 —— 中间明文只在内存里存在一瞬。批量处理没有意义,
    凭据总共就几条。
    """
    del now  # 保留参数是为了将来记录轮换时间,现在用不上
    rows = session.execute(
        _SELECT_STALE, {"user_id": user_id, "current": settings.master_key_version}
    ).all()

    for row in rows:
        current = session.execute(
            text("SELECT ciphertext FROM credentials WHERE id = :id"), {"id": row.id}
        ).scalar_one()
        plain = decrypt(bytes(current), user_id=user_id, kind=row.kind, settings=settings)
        session.execute(
            text("UPDATE credentials SET ciphertext = :blob, key_version = :v WHERE id = :id"),
            {
                "blob": encrypt(plain, user_id=user_id, kind=row.kind, settings=settings),
                "v": settings.master_key_version,
                "id": row.id,
            },
        )
    return len(rows)
