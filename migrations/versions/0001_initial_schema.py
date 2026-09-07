"""初始 schema:建出 docs/06-data-model.md 定义的全部表。

Revision ID: 0001
Create Date: 2026-09-07

06 §3 要求 P0 就把全部表建出来,哪怕大半是空的 —— 后面几期是往里填东西,
不是重搭。所以这里一次建齐 16 张表,P0 真正会写的只有 users / raw_events /
credentials / tool_calls / push_log 五张。

**DDL 手写,不用 autogenerate。** 下面三条 CHECK 是安全机制而不是数据校验:

    facts_provenance_required      没有来源的记忆写不进去(铁律 5)
    l3_never_triggered_by_external L3 永远不由外部内容触发(铁律 8)
    l2_needs_rollback              L2 工具没有回滚信息就写不进审计表,等于执行不了

它们必须活在数据库里而不是代码里 —— 提示注入即使骗过了 agent,也过不了约束。
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# pgvector。装不上就没有 P1 的模糊召回,但 P0 不受影响。
EXTENSIONS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
]

TABLES = [
    # ---------- 用户 ----------
    """
    CREATE TABLE users (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        display_name TEXT NOT NULL,
        wecom_userid TEXT NOT NULL,
        tz           TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        disabled_at  TIMESTAMPTZ,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (wecom_userid)
    )
    """,
    # ---------- 记忆层 ----------
    """
    CREATE TABLE raw_events (
        id              BIGSERIAL PRIMARY KEY,
        user_id         UUID        NOT NULL,
        source          TEXT        NOT NULL,
        external_id     TEXT        NOT NULL,
        occurred_at     TIMESTAMPTZ NOT NULL,
        ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        trust           TEXT        NOT NULL CHECK (trust IN ('user_input','external')),
        raw             JSONB       NOT NULL,
        normalized      JSONB,
        normalize_error TEXT,
        UNIQUE (user_id, source, external_id)
    )
    """,
    """
    CREATE TABLE entities (
        id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id        UUID NOT NULL,
        kind           TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        attributes     JSONB NOT NULL DEFAULT '{}',
        first_seen_at  TIMESTAMPTZ NOT NULL,
        last_seen_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE entity_aliases (
        id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id            UUID NOT NULL,
        entity_id          UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        alias              TEXT NOT NULL,
        alias_type         TEXT NOT NULL,
        confidence         NUMERIC(3,2) NOT NULL DEFAULT 0.5,
        evidence_event_ids BIGINT[] NOT NULL DEFAULT '{}',
        created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, alias, alias_type)
    )
    """,
    """
    CREATE TABLE facts (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id           UUID NOT NULL,
        statement         TEXT NOT NULL,
        provenance        BIGINT[] NOT NULL,
        confidence        NUMERIC(3,2) NOT NULL,
        confirmed_by_user BOOLEAN NOT NULL DEFAULT false,
        negated_by_user   BOOLEAN NOT NULL DEFAULT false,
        valid_from        TIMESTAMPTZ NOT NULL,
        valid_until       TIMESTAMPTZ,
        created_by_agent  TEXT NOT NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT facts_provenance_required
            CHECK (cardinality(provenance) >= 1)
    )
    """,
    # VECTOR(1024) 与 EMBEDDING_DIM 必须一致,改一边等于改另一边(07 §2.3)
    """
    CREATE TABLE embeddings (
        id         BIGSERIAL PRIMARY KEY,
        user_id    UUID NOT NULL,
        ref_type   TEXT NOT NULL,
        ref_id     TEXT NOT NULL,
        embedding  VECTOR(1024) NOT NULL,
        model      TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, ref_type, ref_id, model)
    )
    """,
    # ---------- 账务(P2) ----------
    """
    CREATE TABLE transactions (
        id                         BIGSERIAL PRIMARY KEY,
        user_id                    UUID NOT NULL,
        occurred_at                TIMESTAMPTZ NOT NULL,
        amount                     NUMERIC(14,2) NOT NULL,
        currency                   TEXT NOT NULL DEFAULT 'CNY',
        direction                  TEXT NOT NULL CHECK (direction IN ('debit','credit')),
        kind                       TEXT NOT NULL CHECK (kind IN
                                     ('expense','income','transfer','refund','repayment')),
        merchant_raw               TEXT,
        merchant_entity_id         UUID REFERENCES entities(id),
        category                   TEXT,
        account_hint               TEXT,
        channel                    TEXT NOT NULL,
        stage                      TEXT NOT NULL DEFAULT 'realtime'
                                     CHECK (stage IN ('realtime','reconciled')),
        matched_statement_event_id BIGINT REFERENCES raw_events(id),
        source_event_id            BIGINT NOT NULL REFERENCES raw_events(id),
        merged_from_event_ids      BIGINT[] NOT NULL DEFAULT '{}',
        confidence                 NUMERIC(3,2) NOT NULL,
        created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, source_event_id)
    )
    """,
    """
    CREATE TABLE merchant_rules (
        id          BIGSERIAL PRIMARY KEY,
        user_id     UUID NOT NULL,
        pattern     TEXT NOT NULL,
        match_type  TEXT NOT NULL CHECK (match_type IN ('exact','prefix','regex')),
        category    TEXT NOT NULL,
        created_by  TEXT NOT NULL CHECK (created_by IN ('llm','user')),
        hit_count   INTEGER NOT NULL DEFAULT 0,
        last_hit_at TIMESTAMPTZ,
        UNIQUE (user_id, pattern, match_type)
    )
    """,
    """
    CREATE TABLE budgets (
        id              BIGSERIAL PRIMARY KEY,
        user_id         UUID NOT NULL,
        category        TEXT,
        period          TEXT NOT NULL DEFAULT 'month',
        amount          NUMERIC(14,2) NOT NULL,
        alert_threshold NUMERIC(3,2) NOT NULL DEFAULT 0.9,
        UNIQUE (user_id, category, period)
    )
    """,
    # ---------- 治理层 ----------
    """
    CREATE TABLE pending_confirmations (
        id               BIGSERIAL PRIMARY KEY,
        user_id          UUID NOT NULL,
        agent            TEXT NOT NULL,
        kind             TEXT NOT NULL,
        target_table     TEXT NOT NULL,
        payload          JSONB NOT NULL,
        reason           TEXT NOT NULL,
        confidence       NUMERIC(3,2),
        source_event_id  BIGINT REFERENCES raw_events(id),
        status           TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','confirmed','edited','rejected','expired')),
        resolved_at      TIMESTAMPTZ,
        resolved_via     TEXT,
        resolved_payload JSONB,
        expires_at       TIMESTAMPTZ NOT NULL,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE approvals (
        id              BIGSERIAL PRIMARY KEY,
        user_id         UUID NOT NULL,
        agent           TEXT NOT NULL,
        tool_name       TEXT NOT NULL,
        tool_args       JSONB NOT NULL,
        preview_text    TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        trigger_trust   TEXT NOT NULL,
        source_event_id BIGINT REFERENCES raw_events(id),
        status          TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','approved','rejected',
                                            'executed','expired','failed')),
        expires_at      TIMESTAMPTZ NOT NULL,
        approved_at     TIMESTAMPTZ,
        executed_at     TIMESTAMPTZ,
        result          JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, idempotency_key),
        CONSTRAINT l3_never_triggered_by_external
            CHECK (trigger_trust = 'user_input')
    )
    """,
    """
    CREATE TABLE tool_calls (
        id                BIGSERIAL PRIMARY KEY,
        user_id           UUID NOT NULL,
        agent             TEXT NOT NULL,
        tool_name         TEXT NOT NULL,
        level             TEXT NOT NULL CHECK (level IN ('L1','L2','L3')),
        args_digest       JSONB NOT NULL,
        llm_fields_sent   TEXT[] NOT NULL DEFAULT '{}',
        result_status     TEXT NOT NULL,
        duration_ms       INTEGER,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER,
        cost_cny          NUMERIC(10,4),
        rollback_info     JSONB,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT l2_needs_rollback
            CHECK (level <> 'L2' OR rollback_info IS NOT NULL)
    )
    """,
    """
    CREATE TABLE push_log (
        id             BIGSERIAL PRIMARY KEY,
        user_id        UUID NOT NULL,
        rule_id        TEXT,
        channel        TEXT NOT NULL CHECK (channel IN ('wecom','email')),
        mode           TEXT NOT NULL CHECK (mode IN ('shadow','active')),
        payload_digest JSONB NOT NULL,
        delivered      BOOLEAN NOT NULL DEFAULT false,
        error          TEXT,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---------- 凭据与采集端 ----------
    """
    CREATE TABLE credentials (
        id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id     UUID NOT NULL,
        kind        TEXT NOT NULL,
        scope       TEXT NOT NULL CHECK (scope IN ('ingest','query','both')),
        ciphertext  BYTEA NOT NULL,
        key_version INTEGER NOT NULL,
        device_id   TEXT,
        revoked_at  TIMESTAMPTZ,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE collector_heartbeat (
        user_id          UUID NOT NULL,
        device_id        TEXT NOT NULL,
        last_seen_at     TIMESTAMPTZ NOT NULL,
        app_version      TEXT,
        android_version  TEXT,
        listener_enabled BOOLEAN NOT NULL,
        PRIMARY KEY (user_id, device_id)
    )
    """,
    """
    CREATE TABLE collector_whitelist (
        id         BIGSERIAL PRIMARY KEY,
        user_id    UUID NOT NULL,
        match_type TEXT NOT NULL CHECK (match_type IN ('sms_sender','package_name')),
        pattern    TEXT NOT NULL,
        purpose    TEXT NOT NULL CHECK (purpose IN ('transaction','message')),
        enabled    BOOLEAN NOT NULL DEFAULT true,
        phase      TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (user_id, match_type, pattern)
    )
    """,
]

# 06 §3:每张表的第一个索引都以 user_id 开头;条件索引优先。
# 索引显式命名,否则 downgrade 无从下手。
INDEXES = [
    "CREATE INDEX ix_raw_events_user_time ON raw_events (user_id, occurred_at DESC)",
    "CREATE INDEX ix_raw_events_user_source_time ON raw_events (user_id, source, occurred_at DESC)",
    "CREATE INDEX ix_raw_events_normalize_error ON raw_events (user_id) "
    "WHERE normalize_error IS NOT NULL",
    "CREATE INDEX ix_raw_events_normalized_gin ON raw_events USING GIN (normalized)",
    "CREATE INDEX ix_entities_user_kind ON entities (user_id, kind)",
    "CREATE INDEX ix_facts_user_active ON facts (user_id) WHERE negated_by_user = false",
    "CREATE INDEX ix_embeddings_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_transactions_user_time ON transactions (user_id, occurred_at DESC)",
    "CREATE INDEX ix_transactions_user_cat_time ON transactions "
    "(user_id, category, occurred_at DESC)",
    "CREATE INDEX ix_transactions_user_realtime ON transactions (user_id, stage) "
    "WHERE stage = 'realtime'",
    "CREATE INDEX ix_pending_user_status_time ON pending_confirmations "
    "(user_id, status, created_at DESC)",
    "CREATE INDEX ix_tool_calls_user_time ON tool_calls (user_id, created_at DESC)",
    "CREATE INDEX ix_push_log_user_active ON push_log (user_id, created_at DESC) "
    "WHERE mode = 'active'",
]

# downgrade 按依赖倒序删。不用 CASCADE —— 要是还有别的东西依赖着,
# 应该报错让人看见,而不是顺手一起删掉。
DROP_ORDER = [
    "collector_whitelist",
    "collector_heartbeat",
    "credentials",
    "push_log",
    "tool_calls",
    "approvals",
    "pending_confirmations",
    "budgets",
    "merchant_rules",
    "transactions",
    "embeddings",
    "facts",
    "entity_aliases",
    "entities",
    "raw_events",
    "users",
]


def upgrade() -> None:
    for stmt in EXTENSIONS + TABLES + INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    # 不删 vector 扩展:它可能被同库别的东西用着,删扩展的破坏面远大于删表。
