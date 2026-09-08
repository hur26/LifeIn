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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from lifein.config import Settings
from lifein.crypto import DecryptError, decrypt, encrypt

log = logging.getLogger(__name__)

_QUERY_SCOPES = ("query", "both")
_INGEST_SCOPES = ("ingest", "both")

INGEST_KIND = "collector"
"""采集设备密钥的 kind。只能写(06 §6.1)。"""

QUERY_KIND = "app_device"
"""App 查询设备密钥的 kind。它同时是短期 token 的签名密钥(06 §6.3)。"""


@dataclass(frozen=True)
class DeviceCredential:
    """一条设备凭据的元信息 —— **不含密钥本身**。

    列设备是运维动作,不该顺手把明文密钥打到终端上。要密钥就重新签发一把,
    旧的同时作废,这比"再看一眼"安全得多。
    """

    device_id: str
    kind: str
    scope: str
    created_at: datetime
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

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

_SELECT_DEVICE = text("""
    SELECT id, ciphertext
      FROM credentials
     WHERE user_id = :user_id
       AND kind = :kind
       AND device_id = :device_id
       AND scope = ANY(:scopes)
       AND revoked_at IS NULL
     ORDER BY created_at DESC
     LIMIT 1
""")

_REVOKE_ALL_OF_KIND = text("""
    UPDATE credentials
       SET revoked_at = now()
     WHERE user_id = :user_id
       AND kind = :kind
       AND revoked_at IS NULL
""")

_REVOKE_DEVICE = text("""
    UPDATE credentials
       SET revoked_at = now()
     WHERE user_id = :user_id
       AND device_id = :device_id
       -- 写 CAST 而不是 :kind::text —— 后者会被 text() 的绑定参数解析吃掉一个冒号
       AND (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT))
       AND revoked_at IS NULL
""")

_LIST_DEVICES = text("""
    SELECT device_id, kind, scope, created_at, revoked_at
      FROM credentials
     WHERE user_id = :user_id
       AND device_id IS NOT NULL
     ORDER BY created_at
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


def get_device_credential(
    user_id: str,
    session: Session,
    *,
    kind: str,
    device_id: str,
    settings: Settings,
    for_ingest: bool = False,
) -> dict[str, Any] | None:
    """取某台设备的凭据并解密。**这是 App 那两组接口的认证依据**(06 §6.1)。

    和 `get_credential` 的区别只有一个 `device_id`,但那个区别就是 R11 要的
    "按设备单点吊销":一台手机丢了,吊销它那两行,别的设备照常用。

    `for_ingest` 的两边**一样不通融**:采集密钥在这里也换不出查询凭据 ——
    scope 过滤是最后一道,就算路由挂错了依赖它也拦得住。
    """
    scopes = list(_INGEST_SCOPES if for_ingest else _QUERY_SCOPES)
    row = session.execute(
        _SELECT_DEVICE,
        {"user_id": user_id, "kind": kind, "device_id": device_id, "scopes": scopes},
    ).first()
    if row is None:
        return None

    try:
        plain = decrypt(bytes(row.ciphertext), user_id=user_id, kind=kind, settings=settings)
    except DecryptError:
        log.exception("设备凭据 %s/%s 解密失败,id=%s", kind, device_id, row.id)
        raise

    return json.loads(plain)


def revoke_device(
    user_id: str, session: Session, *, device_id: str, kind: str | None = None
) -> int:
    """吊销一台设备的凭据。返回受影响条数。

    **默认吊销这台设备的全部凭据**,因为触发它的场景是"手机丢了" ——
    那时只吊销其中一种,等于把另一种留在别人手里。
    要单独吊销一种(比如只停采集)才传 `kind`。

    记录保留不删:哪台设备什么时候被吊销的,是排查"手机丢了之后还有没有人
    在用"时唯一的线索。
    """
    return int(
        session.execute(
            _REVOKE_DEVICE, {"user_id": user_id, "device_id": device_id, "kind": kind}
        ).rowcount
    )



def revoke_all_of_kind(user_id: str, session: Session, *, kind: str) -> int:
    """吊销这个用户**所有设备**上某一类凭据。返回受影响条数。

    和 `revoke_device` 的区别是它不指定设备 —— 用在"把采集整个关掉"那一步
    (P4 第 3 片):那时用户想的是"别再采了",而不是"停掉某一台"。
    要求他先列出自己有几台设备再一台台停,等于把这个开关做成了一道作业。

    记录保留不删,和 `revoke_device` 同一条理由。
    """
    return int(session.execute(_REVOKE_ALL_OF_KIND, {"user_id": user_id, "kind": kind}).rowcount)


def list_device_credentials(user_id: str, session: Session) -> list[DeviceCredential]:
    """列出这个用户签发过的设备凭据,**含已吊销的**。

    已吊销的照样列出来:签发过什么、什么时候断的,是运维要回答的问题,
    而"列表里没有"回答不了它。
    """
    rows = session.execute(_LIST_DEVICES, {"user_id": user_id}).all()
    return [
        DeviceCredential(
            device_id=row.device_id,
            kind=row.kind,
            scope=row.scope,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


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
