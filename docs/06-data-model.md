# 06 · 数据模型与实现契约

**这份文档的地位与前五份不同:前五份记录"为什么",这份记录"是什么"。**
改这里的任何一个字段,等于改代码。动手前先看
[AGENTS.md](../AGENTS.md#4-文档没写的怎么办)。

三条贯穿全文的硬约束,在表结构里用数据库约束强制,不靠代码自觉:

- 所有表带 `user_id`([架构 §6](02-architecture.md#6-为多用户预留p4-前必须做到))
- `facts.provenance` 非空且长度 > 0([架构 §2.3](02-architecture.md#23-事实库--facts))
- 未声明等级的工具按 L3 拒绝([产品定义 §7](01-product-spec.md#7-行动权限边界三级分权))

---

## 1. 归一化骨架

架构 §2.1 说"归一化是核心脏活,决定成败"。这一节就是那个骨架的完整定义。

**一切数据源都要落到这一个结构上。** 邮件、日历、通知、短信、账单 CSV、
PDF 对账单没有例外。落不上去的字段进 `raw`,不要往骨架上加字段
—— 加字段意味着所有已有适配器都要重新审视一遍。

### 1.1 NormalizedEvent

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | :-: | --- |
| `kind` | enum | ✗ | `message` / `calendar_event` / `transaction` / `task` / `document` |
| `title` | text | ✗ | 短标题,用于摘要和列表。**不超过 120 字**,超了截断 |
| `occurred_at` | timestamptz | ✗ | 事件**真实发生**时间,不是摄入时间。带时区 |
| `parties` | Party[] | ✗ | 参与方,可为空数组 |
| `amount` | Amount | ✓ | 只有 `kind=transaction` 时必填 |
| `body` | text | ✓ | 正文或详情。可能被源截断,截断了要在 `flags` 标记 |
| `location` | text | ✓ | |
| `external_ref` | {source, external_id} | ✗ | 去重与溯源 |
| `attachments` | Attachment[] | ✓ | 只存引用与元信息,不存内容 |
| `trust` | enum | ✗ | **`user_input` / `external`** —— 见 §1.2 |
| `confidence` | numeric(3,2) | ✗ | 归一化本身的置信度,不是内容的 |
| `flags` | text[] | ✓ | `truncated` / `aggregated` / `partial` / `parse_degraded` |

```
Party  = { role, display_name, identifier?, identifier_type? }
         role ∈ from | to | cc | organizer | attendee | merchant | payer | payee
         identifier_type ∈ email | phone | wecom_userid | card_last4 | merchant_code

Amount = { value: numeric(14,2), currency: text, direction: debit | credit }
```

### 1.2 trust 字段是安全机制,不是元数据

`trust` 把 [R3](05-risks.md#r3--提示注入不可信的外部内容) 的"外部内容标注为不可信"
**从口头约定变成数据模型的一部分**。取值只有两个:

| 值 | 含义 | 谁属于这类 |
| --- | --- | --- |
| `user_input` | 用户本人主动输入 | 企微里发的消息、App 里手动补的一笔 |
| `external` | **其他一切** | 邮件、群消息、通知、转账备注、日历邀请、账单文件 |

强制规则:

1. `external` 内容进 prompt **必须结构化隔离**,并声明其中的指令不得执行
2. **L3 工具调用的 `triggered_by` 只能是 `user_input`**,数据库层面校验(见 §2.8)
3. `external` 来源写入 `facts` 时 `confidence` 上限 0.6,需用户确认才提升

> 这三条不是建议。第 2 条在 `approvals` 表上有 `CHECK` 约束。

### 1.3 各数据源到骨架的映射

**这张表是防止走偏的关键。** 新增数据源前先把这一行填出来,填不出来说明还没想清楚。

| source | kind | title ← | occurred_at ← | parties ← | amount | body ← | trust |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `email` | `message` | Subject | Date 头 | From/To/Cc → email | ✗ | text/plain 优先,html 降级抽取 | `external` |
| `calendar` | `calendar_event` | summary | start_time | organizer + attendees | ✗ | description | 自建 `user_input`,他人邀请 `external` |
| `notification` | `message` 或 `transaction` | 通知标题 | post_time | 从包名与标题推断 | 交易类必填 | 通知正文 | `external` |
| `sms` | `transaction` 或 `message` | 发件号码 | 接收时间 | 号码 → merchant/payer | 交易类必填 | 短信全文 | `external` |
| `bill_csv` | `transaction` | 商品说明 | 交易时间列 | 对方账号列 → merchant | ✗ 必填 | 备注列 | `external` |
| `statement_pdf` | `transaction` | 交易摘要 | 记账日 | 商户列 | ✗ 必填 | — | `external` |
| `note` | `task` 或 `document` | 标题 | 创建/修改时间 | — | ✗ | 正文 | `user_input` |

### 1.4 归一化失败怎么办

**绝不静默丢弃**([R8](05-risks.md#r8--数据源格式变动))。三档处理:

| 情况 | 处理 |
| --- | --- |
| 必填字段缺失 | `normalized` 留空,`normalize_error` 写原因,**告警** |
| 可选字段缺失 | 正常入库,`flags` 加 `partial` |
| 部分降级(如 HTML 抽不干净) | 正常入库,`flags` 加 `parse_degraded` |

`raw` **永远保留**,解析器修好后可以重跑。重跑靠
`(user_id, source, external_id)` 幂等,不会产生重复事件。

---

## 2. 数据表定义

PostgreSQL 15+。迁移用 Alembic,**一次迁移一个语义变更**,不攒批。

### 2.1 raw_events · 事件流

只追加不修改。全系统的事实来源。

```sql
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
);
CREATE INDEX ON raw_events (user_id, occurred_at DESC);
CREATE INDEX ON raw_events (user_id, source, occurred_at DESC);
CREATE INDEX ON raw_events (user_id) WHERE normalize_error IS NOT NULL;  -- 告警扫描
CREATE INDEX ON raw_events USING GIN (normalized);
```

### 2.2 entities + entity_aliases · 实体库

```sql
CREATE TABLE entities (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL,
    kind           TEXT NOT NULL,   -- person|merchant|place|project|subscription|recurring
    canonical_name TEXT NOT NULL,
    attributes     JSONB NOT NULL DEFAULT '{}',
    first_seen_at  TIMESTAMPTZ NOT NULL,
    last_seen_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON entities (user_id, kind);

CREATE TABLE entity_aliases (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL,
    entity_id         UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias             TEXT NOT NULL,
    alias_type        TEXT NOT NULL,  -- name|email|phone|wecom_userid|merchant_code
    confidence        NUMERIC(3,2) NOT NULL DEFAULT 0.5,
    evidence_event_ids BIGINT[] NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, alias, alias_type)
);
```

新别名先低置信度写入,累积证据后提升 —— `evidence_event_ids` 就是那些证据。

### 2.3 facts · 事实库

```sql
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
        CHECK (array_length(provenance, 1) >= 1)
);
CREATE INDEX ON facts (user_id) WHERE negated_by_user = false;
```

**`CHECK` 约束就是铁律 5 的执行者。** 没有来源的记忆在数据库层面就写不进去。
`negated_by_user` 的记录**保留不删**,用来防止系统重复推断出同一条被否定的事实。

### 2.4 向量索引

```sql
CREATE TABLE embeddings (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    ref_type    TEXT NOT NULL,   -- raw_event|entity|fact
    ref_id      TEXT NOT NULL,
    embedding   VECTOR(1024) NOT NULL,
    model       TEXT NOT NULL,   -- 换模型要能识别出旧向量
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, ref_type, ref_id, model)
);
CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops);
```

`model` 字段必须有:换 embedding 模型时新旧向量不可比,要能筛出来重算。

### 2.5 transactions · 账单

```sql
CREATE TABLE transactions (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             UUID NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,
    amount              NUMERIC(14,2) NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    direction           TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    kind                TEXT NOT NULL CHECK (kind IN
                          ('expense','income','transfer','refund','repayment')),
    merchant_raw        TEXT,
    merchant_entity_id  UUID REFERENCES entities(id),
    category            TEXT,
    account_hint        TEXT,          -- 卡号后四位
    channel             TEXT NOT NULL, -- alipay|wechat|bank_sms|bank_app|meituan|...
    stage               TEXT NOT NULL DEFAULT 'realtime'
                          CHECK (stage IN ('realtime','reconciled')),
    matched_statement_event_id BIGINT REFERENCES raw_events(id),
    source_event_id     BIGINT NOT NULL REFERENCES raw_events(id),
    merged_from_event_ids BIGINT[] NOT NULL DEFAULT '{}',
    confidence          NUMERIC(3,2) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, source_event_id)
);
CREATE INDEX ON transactions (user_id, occurred_at DESC);
CREATE INDEX ON transactions (user_id, category, occurred_at DESC);
CREATE INDEX ON transactions (user_id, stage) WHERE stage = 'realtime';
```

**`kind` 是四层防误判的落地点。** 只有 `expense` 和 `income` 进统计;
`repayment`(信用卡还款)计入支出会造成双重记账,因为消费时已经记过一次。

### 2.6 去重的两个层次

**不要用一个字段解决两个问题**,这是最容易做错的地方:

| 问题 | 手段 |
| --- | --- |
| 同一条通知被重复上报(采集器重试、网络重投) | `UNIQUE (user_id, source_event_id)` —— 幂等键 |
| 同一笔交易在多个渠道各发一条(支付宝通知 + 银行短信) | **5 分钟窗口查询**,不是唯一约束 |

跨渠道合并的判定:

```
同一 user_id
  AND amount 相等
  AND account_hint 相等或一方为空
  AND |occurred_at 差| <= 5 分钟
  AND 已有记录的 channel <> 新记录的 channel
→ 合并:保留信息更全的一条,另一条的 event_id 追加进 merged_from_event_ids
```

用唯一约束做跨渠道去重会误杀真实的连续同额消费(便利店连买两次同价商品),
所以它必须是查询判定 + 可回溯的合并记录,不是数据库约束。

### 2.7 pending_confirmations · 统一待确认队列

**一套机制服务所有 agent。** 日程 agent 和记账 agent 不许各写一套。
它是 agent 契约里"失败行为"([ADR-013](04-tech-decisions.md#adr-013--agent-按领域分工同进程运行))
的唯一落地形式。

```sql
CREATE TABLE pending_confirmations (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    agent           TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- transaction|calendar_event|task|fact
    target_table    TEXT NOT NULL,   -- 确认后写入哪张表
    payload         JSONB NOT NULL,  -- agent 原本想写入的内容
    reason          TEXT NOT NULL,   -- low_confidence|check_failed|rule_miss|ambiguous
    confidence      NUMERIC(3,2),
    source_event_id BIGINT REFERENCES raw_events(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','confirmed','edited','rejected','expired')),
    resolved_at     TIMESTAMPTZ,
    resolved_via    TEXT,            -- app|wecom
    resolved_payload JSONB,          -- 用户修改后的内容
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON pending_confirmations (user_id, status, created_at DESC);
```

**状态机**

```
                  ┌── 确认 ──────→ confirmed ──→ 按 payload 写入 target_table
                  │
pending ──────────┼── 修改后确认 ─→ edited ────→ 按 resolved_payload 写入
                  │
                  ├── 拒绝 ──────→ rejected   ──→ 不写入,记录保留
                  │
                  └── 超时(默认 30 天)→ expired ──→ 不写入,记录保留
```

三条约束:

1. **`rejected` 与 `expired` 的记录永不删除。** 它们是 agent 评测集的负样本来源
   —— 用户拒绝过什么,正是这个 agent 最该学会不做的事。
2. 写入 `target_table` 与更新 `status` 必须在**同一个事务**里,否则会出现
   "确认了但没写进去"或"写了两次"。
3. 用户在 App 和企微都能处理,`resolved_via` 记录从哪来,用于观察哪个入口更常用。

### 2.8 approvals 与 pending_confirmations 的区别

**这两张表都是"让人来把关",但性质完全不同,不许合并。**

| | `approvals` | `pending_confirmations` |
| --- | --- | --- |
| 拦的是什么 | **L3 工具调用** —— 要动外部世界 | **agent 对自己的输出没把握** |
| 不做的后果 | 发错消息,**不可撤销的社交事故** | 记错一笔账,可以改 |
| 默认超时 | 24 小时(防止几天后误点) | 30 天(不着急) |
| 触发来源限制 | **只能由 `user_input` 触发** | 无限制,`external` 也可以 |
| 对应风险 | [R2](05-risks.md#r2--l3-操作越权或重复执行) | [R4](05-risks.md#r4--主动推送误报摧毁信任) / [R7](05-risks.md#r7--记忆污染) |

```sql
CREATE TABLE approvals (
    id               BIGSERIAL PRIMARY KEY,
    user_id          UUID NOT NULL,
    agent            TEXT NOT NULL,
    tool_name        TEXT NOT NULL,
    tool_args        JSONB NOT NULL,
    preview_text     TEXT NOT NULL,          -- 人话,不是 JSON
    idempotency_key  TEXT NOT NULL,
    trigger_trust    TEXT NOT NULL,
    source_event_id  BIGINT REFERENCES raw_events(id),
    status           TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved','rejected',
                                         'executed','expired','failed')),
    expires_at       TIMESTAMPTZ NOT NULL,
    approved_at      TIMESTAMPTZ,
    executed_at      TIMESTAMPTZ,
    result           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key),
    CONSTRAINT l3_never_triggered_by_external
        CHECK (trigger_trust = 'user_input')
);
```

**那条 `CHECK` 就是铁律 8。** L3 永远不由外部内容触发 —— 这句话现在是数据库约束,
不是文档里的一句话。提示注入即使骗过了 agent,也写不进这张表。

### 2.9 tool_calls · 审计与成本

```sql
CREATE TABLE tool_calls (
    id                BIGSERIAL PRIMARY KEY,
    user_id           UUID NOT NULL,
    agent             TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    level             TEXT NOT NULL CHECK (level IN ('L1','L2','L3')),
    args_digest       JSONB NOT NULL,   -- 摘要,不是原文
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
);
CREATE INDEX ON tool_calls (user_id, created_at DESC);
```

两个字段值得单说:

- **`args_digest` 存摘要不存原文。** 日志本身是数据集中点,存原文等于又建了一份
  全量副本([R10](05-risks.md#r10--手机端采集器的越权读取))。
- **`llm_fields_sent` 记录发给模型的字段名**(不是内容)。这样
  "我到底把什么发给外部供应商了"是个能回答的问题
  ([R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露))。

`CHECK` 约束保证 L2 工具没有回滚信息就写不进审计表 —— 也就等于执行不了。

### 2.10 其余表

```sql
-- 推送日志:频率闸门与影子模式靠它计数
CREATE TABLE push_log (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    rule_id    TEXT,
    channel    TEXT NOT NULL CHECK (channel IN ('wecom','email')),
    mode       TEXT NOT NULL CHECK (mode IN ('shadow','active')),
    payload_digest JSONB NOT NULL,
    delivered  BOOLEAN NOT NULL DEFAULT false,
    error      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON push_log (user_id, created_at DESC) WHERE mode = 'active';

-- 商户归类规则表:LLM 结果沉淀于此,调用占比应随时间下降
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
);

CREATE TABLE budgets (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID NOT NULL,
    category        TEXT,                    -- NULL = 总预算
    period          TEXT NOT NULL DEFAULT 'month',
    amount          NUMERIC(14,2) NOT NULL,
    alert_threshold NUMERIC(3,2) NOT NULL DEFAULT 0.9,
    UNIQUE (user_id, category, period)
);

-- 凭据:字段级加密 + 支持轮换 + 读写权限分离
CREATE TABLE credentials (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL,
    kind        TEXT NOT NULL,   -- imap|wecom|llm|collector|app_device
    scope       TEXT NOT NULL CHECK (scope IN ('ingest','query','both')),
    ciphertext  BYTEA NOT NULL,
    key_version INTEGER NOT NULL,
    device_id   TEXT,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 采集器心跳:静默掉线是这条链路最可能的失效方式
CREATE TABLE collector_heartbeat (
    user_id          UUID NOT NULL,
    device_id        TEXT NOT NULL,
    last_seen_at     TIMESTAMPTZ NOT NULL,
    app_version      TEXT,
    android_version  TEXT,
    listener_enabled BOOLEAN NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
```

`credentials.scope` 就是 [R11](05-risks.md#r11--app-直连服务端的认证面) 的
"采集凭据与查询凭据分开" —— 采集端签发的凭据 `scope='ingest'`,读接口一律拒绝。

`key_version` 是为密钥轮换留的。加密主密钥一旦泄露等于全部凭据泄露,
而单机自托管的环境变量很容易进日志、进备份、进 shell history。
没有版本号就没法平滑换密钥。

---

## 3. 索引与迁移约定

- **每张表的第一个索引都以 `user_id` 开头。** 租户隔离靠数据访问层强制,
  但索引也要顺着这个方向,否则 P4 多用户后全表扫。
- **部分索引优先**:`WHERE normalize_error IS NOT NULL`、`WHERE mode = 'active'`
  这类条件索引比全量索引便宜得多,而查询恰好只关心那一小部分。
- 迁移用 **Alembic**,一次迁移一个语义变更。迁移脚本必须可回滚;
  不可回滚的(如删列)拆成两次发布。
- **P0 就把全部表建出来**,哪怕大半是空的。表结构从 P0 到位,
  后面几期是往里填东西,不是重搭([架构 §5](02-architecture.md#5-p0-完整链路))。

---

## 4. 这份文档还缺什么

诚实记录,不要假装完备:

- **接口契约**(`/ingest` payload、App 查询 API 路由、企微卡片模板结构)
  —— P1 开工前补,P0 用不到
- **prompt 结构**(外部内容的隔离标记格式)—— P0 写摘要 agent 时定,定了回填这里
- **评测集格式**(agent 契约第 5 项)—— 同上
- 配置项清单见 [07 配置清单](07-config.md)
