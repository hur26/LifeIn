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
        CHECK (cardinality(provenance) >= 1)
);
CREATE INDEX ON facts (user_id) WHERE negated_by_user = false;
```

**`CHECK` 约束就是铁律 5 的执行者。** 没有来源的记忆在数据库层面就写不进去。

> 用 `cardinality` 而不是 `array_length`,是踩过的坑:空数组的 `array_length`
> 返回 **NULL**,而 `NULL >= 1` 也是 NULL,**CHECK 遇到 NULL 判定为通过** ——
> 约束看着在那里,实际一条都拦不住。`cardinality('{}')` 老老实实返回 0。
> 这个错只有真连数据库才发现得了,单元测试永远测不出来。
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
    order_no            TEXT,          -- 订单号,只有对账单那一道给得出
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

### 2.6 去重与对账的三个层次

**不要用一个字段解决两个问题**,这是最容易做错的地方:

| 问题 | 手段 |
| --- | --- |
| 同一条通知被重复上报(采集器重试、网络重投) | `UNIQUE (user_id, source_event_id)` —— 幂等键 |
| 同一笔交易在多个渠道各发一条(支付宝通知 + 银行短信) | **5 分钟窗口查询**,不是唯一约束 |
| 月度对账单里的同一笔(实时那条已经入过账) | **(金额, 时间窗, 卡号后四位)匹配查询** + `matched_statement_event_id` 幂等键 |

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

**第三层：对账回填(ADR-012 的两阶段入账)。**
月度对账单到达时,它里的每一行都已经落成一条 `raw_events`
(**一行一条,不是一封邮件一条**)—— 因为
`UNIQUE (user_id, source_event_id)` 要求每笔交易有自己的来源事件,
一封带 200 行的对账单共用一个 `source_event_id` 只能入账一笔。

```
对对账单里的一行：
  先看它是不是对过了：
      EXISTS (SELECT 1 FROM transactions
               WHERE user_id = ? AND matched_statement_event_id = 这行的 event_id)
    → 对过了,什么都不做(幂等)
  再找实时那一笔：
      同一 user_id AND amount 相等
        AND account_hint 相等或一方为空
        AND |occurred_at 差| <= 对账时间窗(默认 3 天)
        AND stage = 'realtime'
        AND matched_statement_event_id IS NULL
    → 找到：回填真实商户名、订单号,重新归类,
      stage 改成 reconciled,matched_statement_event_id 写上这行的 event_id
    → 没找到：按一笔新的入账(stage = reconciled),
      并计入覆盖率统计 —— 它意味着实时那一路漏了一笔
```

时间窗比跨渠道合并那 5 分钟宽得多,是因为
**对账单上记的往往是入账日而不是消费日**,周末和节假日能差几天。
窗口开大的代价是可能认错两笔同额消费,所以金额和卡号必须同时对得上,
而且**一笔实时记录只能被对账单回填一次**(`matched_statement_event_id IS NULL`)。

**幂等键不能靠“重跑不了”。** ADR-012 写着“重复入账比漏记更糟”
—— 漏记你会发现,重复不会。同一封对账单被重新解一遍是常态
(补跑、手动重导),所以上面那两道必须各守一边：
`matched_statement_event_id` 挡住“回填两次”,
`UNIQUE (user_id, source_event_id)` 挡住“补成两笔新的”。


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
        CHECK (level <> 'L2' OR result_status <> 'allowed' OR rollback_info IS NOT NULL)
);
CREATE INDEX ON tool_calls (user_id, created_at DESC);
```

两个字段值得单说:

- **`args_digest` 存摘要不存原文。** 日志本身是数据集中点,存原文等于又建了一份
  全量副本([R10](05-risks.md#r10--手机端采集器的越权读取))。
- **`llm_fields_sent` 记录发给模型的字段名**(不是内容)。这样
  "我到底把什么发给外部供应商了"是个能回答的问题
  ([R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露))。

`CHECK` 约束保证**放行了的** L2 工具没有回滚信息就写不进审计表 —— 也就等于执行不了。

**为什么要带上 `result_status <> 'allowed'` 这一段**(改于 P1,见迁移 0006):
不带的话,被拒和出错的 L2 调用同样写不进来 —— 它们本来就没有回滚信息可写。
而写入被库拒之后 `record_tool_call` 按设计吞掉异常(审计失败不该让业务失败),
结果是**审计里悄悄少了一整类记录**:谁都不知道有人试过一次 L2 并被拦下。
网关第 5 道写的是"无论结果如何都记审计,包括被拒的",那句话对 L2 此前是空的。
`error` 一档尤其要留:它意味着副作用可能已经发生却拿不到回滚信息。

### 2.10 其余表

```sql
-- 用户:所有表的 user_id 指向这里
-- P0 只有一行,但它必须存在 —— 没有它,user_id 就只是个从配置里抄来的字符串,
-- 而"摘要该推给哪个企微用户"也没有地方记(wecom_userid 就是那个地方)。
CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT NOT NULL,
    wecom_userid TEXT NOT NULL,   -- 推送目标,企微自建应用里的成员 UserID
    tz           TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    disabled_at  TIMESTAMPTZ,     -- 停用不删除:历史事件仍要能追溯到人
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (wecom_userid)
);
```

**为什么不给其他表加 `user_id` 外键**:`raw_events` 是只追加的事件流,量最大
写入最频繁,每行一次外键检查不划算;而租户隔离本来就要靠数据访问层强制(铁律 1),
外键给不了那个保证。`users` 表的作用是**让 `user_id` 有出处**,不是替代那道检查。

```sql
-- 推送日志:频率闸门与影子模式靠它计数
-- channel 的取值要跟着通道一起加(迁移 0004 就是补 weixin)。
-- 漏了的表现很坑:消息发出去了、记录写不进去、事务回滚、任务标成失败,
-- 而下次重跑同一个窗口会再发一遍。
CREATE TABLE push_log (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    rule_id    TEXT,
    channel    TEXT NOT NULL CHECK (channel IN ('weixin','wecom','email')),
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
    -- NULLS NOT DISTINCT 不能省:总预算的 category 是 NULL,而 Postgres 默认
    -- 把 NULL 之间看成互不相同 —— 少了它,同一个人能有两条总预算,
    -- 各发各的预警,而改额度这个动作在用户眼里就是失败了(迁移 0009)
    UNIQUE NULLS NOT DISTINCT (user_id, category, period)
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

-- 定时任务执行窗口:补偿的依据
-- ADR-016 说补偿"靠数据库记录上次执行窗口实现,不依赖调度器自身的持久化"。
-- 这张表就是那个记录。唯一键让同一个窗口不会被**并发**跑两遍,
-- 而 attempts 让它在**失败之后**还能再跑一遍 —— 这是两件事,见下方。
CREATE TABLE job_runs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    job_name     TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    error        TEXT,
    stats        JSONB NOT NULL DEFAULT '{}',
    UNIQUE (user_id, job_name, window_start)
);
CREATE INDEX ON job_runs (user_id, job_name, window_end DESC);

-- 通道状态:入站通道自己要记住的一点东西
-- 两个已知用途,都不属于"凭据"也不属于"业务数据":
--   get_updates_buf      iLink 长轮询的游标。不存,进程重启会重放旧消息 ——
--                        重放意味着重复回答、重复花钱
--   context_token:<对方>  iLink 要求回复时原样带回对方最近一次的 context_token
-- 刻意做成通用 KV 而不是给 iLink 开专表:下一个入站通道也会有类似的东西,
-- 而这类状态丢了不会出事(重新同步即可),不值得各自建表。
CREATE TABLE channel_state (
    user_id    UUID NOT NULL,
    channel    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, channel, key)
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

#### 窗口失败之后会发生什么

**这一段是补出来的,因为原来的写法会静默丢事件。**

原来的语义是:窗口认领走 `ON CONFLICT DO NOTHING`,而"该跑哪些窗口"只从
**最后一个成功窗口**往后切。两条规则单看都对,合起来是一个洞:

1. 周一的窗口失败了 —— `status='failed'`,那一天的事件一条都没处理
2. 周二再跑,`windows_to_run` 从周日的成功点往后切,确实切出了周一
3. 但 `claim_window` 撞上周一那行,`DO NOTHING`,返回"已经跑过",跳过
4. 周二的窗口成功,成功点推进到周二 —— **周一从此再也不会被切出来**

结果是"失败"和"成功"对下一次运行**没有任何区别**,而那一天的钱、日程、
记忆全部消失,日志里只有一行早已被滚掉的 WARNING。

所以 `attempts`:

- 认领改成 `ON CONFLICT DO UPDATE ... WHERE status = 'failed' AND attempts < 3`。
  判断和写入在同一条语句里 —— 先查后写会在并发下两个进程同时认领
- **重跑必须是幂等的。** 这一点由下游保证而不是由这张表保证:
  `transactions` 有 `UNIQUE (user_id, source_event_id)`,`todos`、`facts`
  同理。重跑整个窗口只会把已经处理过的那些判成 duplicate
- 三次之后不再重跑,行留在库里(`status='failed'`,`attempts=3`)。
  **无限重试比放弃更糟**:一个永远失败的窗口会把后面每一天的额度都吃掉,
  而那时丢的就不是一天了

#### `running` 留着,但六小时之后不算数

`job_runs.status='running'` 的记录**不清理**:进程被 kill 时它会留在那里,
而"上一次跑了一半"和"从来没跑过"是两种不同的状态,分不清就没法安全补偿。

但"不清理"不等于"永远当它在跑"。上面那个洞有一扇一模一样的侧门:
进程在窗口跑到一半时被 kill,行卡在 `running`,而 `running` 既不算成功
(不推进成功点)也不能被重新认领 —— **这一天同样丢掉了**。

所以认领时把 `started_at` 早于六小时的 `running` 也当成可重认领的。
六小时不是拍的:这几个 job 里最慢的是月度报告,一次模型调用加一次推送,
分钟量级;跑了六小时的窗口一定是死的,不是慢的。

> 这和"不清理"不矛盾:行还在,`attempts` 记着它被认领过几次,
> "上一次跑了一半"这个事实一点没丢 —— 只是不再拿它当借口不干活。

采集白名单表 `collector_whitelist` 定义在
[07 §4](07-config.md#4-采集白名单存表用户可改) —— 它是用户可改的配置,
放在那份文档里更顺手,但它同样是一张需要 `user_id` 的业务表。

`credentials.scope` 就是 [R11](05-risks.md#r11--app-直连服务端的认证面) 的
"采集凭据与查询凭据分开" —— 采集端签发的凭据 `scope='ingest'`,读接口一律拒绝。

`key_version` 是为密钥轮换留的。加密主密钥一旦泄露等于全部凭据泄露,
而单机自托管的环境变量很容易进日志、进备份、进 shell history。
没有版本号就没法平滑换密钥。

---

### 2.11 todos · 待办与待写入的日程

P1 新增。[ADR-020](04-tech-decisions.md#adr-020--待办与日程落在自己的-app企微退出主链路)
把待办和日程的落地点定在自己这边,**App 是它的界面,不是另一个数据源**。

```sql
CREATE TABLE todos (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL,
    kind          TEXT NOT NULL,            -- todo | schedule
    title         TEXT NOT NULL,
    notes         TEXT,
    starts_at     TIMESTAMPTZ,              -- kind=schedule 时必填
    ends_at       TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'open',   -- open|done|cancelled
    source        TEXT NOT NULL,            -- agent|user
    provenance    BIGINT[] NOT NULL DEFAULT '{}', -- raw_events.id,agent 建的必须有
    device_ref    TEXT,                     -- 系统日历里那条事件的 id,设备回报
    synced_at     TIMESTAMPTZ,              -- 设备确认写入的时间。空 = 还没落地
    created_by_agent TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT todos_kind_known   CHECK (kind IN ('todo', 'schedule')),
    CONSTRAINT todos_status_known CHECK (status IN ('open', 'done', 'cancelled')),
    CONSTRAINT todos_source_known CHECK (source IN ('agent', 'user')),
    CONSTRAINT todos_schedule_needs_time
        CHECK (kind <> 'schedule' OR starts_at IS NOT NULL),
    CONSTRAINT todos_agent_needs_provenance
        CHECK (source <> 'agent' OR cardinality(provenance) >= 1)
);
CREATE INDEX ON todos (user_id, status, starts_at);
CREATE INDEX ON todos (user_id) WHERE synced_at IS NULL AND kind = 'schedule';
```

**两条 CHECK 各挡一类错:**

- `todos_schedule_needs_time` —— 没有时间的东西写不进日历。它进来只会变成
  设备端一次必然失败的写入,而失败发生在手机上,服务端只看得到"一直没同步"
- `todos_agent_needs_provenance` —— **和 `facts` 是同一条铁律 5**。
  用户自己加的待办不需要出处,系统替他加的必须说得出为什么。
  一条"帮张三带个东西"凭空出现在待办列表里,比不出现更让人不敢用

**`device_ref` 与 `synced_at` 是这张表和别处最不一样的地方。**
`kind=schedule` 的行,副作用**发生在服务端之外**:App 写进系统日历之后回报
event id,那个 id 就是 L2 的回滚信息(`tool_calls.rollback_info` 里存的也是它)。
`synced_at` 为空就是**还没落地** —— 这个状态必须在 App 上看得见,
"看得见的延迟"可以接受,"以为写进去了其实没有"不行。

**为什么待办和日程同一张表。** 它们的区别只有"有没有时间"这一项:
提取时拿不准时间的走待办,拿得准的走日程,而拿不准是常态。
分两张表意味着"补上一个时间"要跨表搬行,而那正是最常发生的编辑。

---

### 2.12 rule_state · 主动规则的开关

P1 新增。[架构 §4](02-architecture.md#4-触发方式) 要求"每条主动规则有
`mode ∈ {shadow, active}`",[产品定义 §5](01-product-spec.md#5-主动性双模式)
要求"每条主动推送都要能一键关闭该类规则"。这张表就是那两句话的落点。

```sql
CREATE TABLE rule_state (
    user_id    UUID NOT NULL,
    rule_id    TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'shadow',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, rule_id),
    CONSTRAINT rule_state_mode_known CHECK (mode IN ('shadow', 'active', 'off'))
);
```

**没有记录就是 `shadow`。** 这一条比表结构本身重要:新加一条规则时,
不写这张表它就只会记录、不会推送 —— **忘记配置的后果是安静,不是打扰**。
反过来设计(默认 active)的话,某天有人加了条规则忘了说,用户第二天就被吵到,
而 [R4](05-risks.md#r4--主动推送误报摧毁信任) 说误报两次就足够让人关掉通知。

`off` 是用户主动关掉的那一档,和"还在观察期"的 `shadow` 分开:
前者不该再被自动转成 active,后者迟早要转。

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

- ~~**接口契约**(`/ingest` payload、App 查询与待办 API 路由、日历回报的 payload)~~
  ✅ 已定,见 §6
- ~~**prompt 结构**(外部内容的隔离标记格式)~~ ✅ 已定,见 §5

- ~~**评测集格式**(agent 契约第 5 项)~~ ✅ 已定,见 §7
- 配置项清单见 [07 配置清单](07-config.md)

---

## 5. prompt 里的外部内容隔离

实现在 `lifein/llm/prompt.py`。**这是 [R3](05-risks.md#r3--提示注入不可信的外部内容)
在 prompt 层的那道防线**,格式定死如下:

```
<external_content source="email" id="<m1@qq.com>" trust="external">
发件人: finance@example.com
时间: 2026-09-07T08:30:00+08:00

(正文)
</external_content>
```

四条规则:

1. **外部内容一律包在标记里**,系统提示同时声明其中的指令不得执行。
   外部内容包括:邮件正文、群消息、**转账备注**、App 通知文本。
2. **正文里出现的结束标记要被打断**(插零宽空格),否则一封邮件写上
   `</external_content>` 就能"跳出"隔离区,后面的文字会被当成系统的话。
   打断而不是删除 —— 删了会改变正文语义;也不报错 —— 报错会让一封带这种
   字符串的正常邮件永远进不了摘要。
3. **属性值里的引号与尖括号一律剥掉**,否则能伪造出 `trust="user_input"`。
4. **能用规则拿到的字段不进正文**(铁律 9),抽成标记内的结构化行单独给。
   字段名清单写进 `tool_calls.llm_fields_sent`([§2.9](#29-tool_calls--审计与成本))。

**必须说清它挡不住什么。** 隔离标记不是加密也不是沙箱,模型完全可以无视它。
它降低的是"模型把外部文本里的祈使句当成用户指令"的概率,**不能归零**。
真正兜底的是另外两道:L3 永远不由外部内容触发(网关 + `approvals` 的 CHECK),
以及 L2 必须可回滚。没有那两道,这里写得再漂亮也只是心理安慰。

---

## 6. 接口契约

[§4](#4-这份文档还缺什么) 欠的那一条。**P1 第 10 / 11 片开工前补** ——
App 改一次要重新发版,接口还在变的时候不动它([AGENTS.md §9](../AGENTS.md#9-当前状态))。

服务端默认只监听 `127.0.0.1`([07 §2.1](07-config.md#21-基础)),
公网走反向代理 + TLS。下面所有路径都是反代之后的路径。

### 6.1 两组接口,两套凭据

[R11](05-risks.md#r11--app-直连服务端的认证面) 那句"**最重要的一条**"在这里落地,
也就是[铁律 12](../AGENTS.md#1-铁律):

| | 采集端 | 查询端 |
| --- | --- | --- |
| 路径前缀 | `/ingest/*` | `/app/*` |
| 凭据 `kind` | `collector` | `app_device` |
| 凭据 `scope` | `ingest` | `query` |
| 能做什么 | **只能写**:上报通知、上报心跳 | 读待办 / 待确认 / 同步队列,写自己地盘 |
| 认证方式 | 每次请求 HMAC 签名(§6.2) | 长期凭据换短期 token(§6.3) |
| 丢了的后果 | 别人能往你的事件流里塞垃圾 | 别人能读到你的待办与待确认内容 |

**分离是结构性的,不是约定**:两组路由各挂各的依赖函数,
采集端那个只认 `for_ingest=True` 取出来的凭据,查询端那个只认 `query` 的。
`credentials.get_credential` 的 scope 过滤是最后一道 ——
就算路由挂错了依赖,采集密钥也解不出查询凭据那一行。

**两条凭据都按设备签发**(`credentials.device_id` 非空),同一台手机上是两条独立的行。
手机丢了,吊销这台设备的那两条即可,别的设备不受影响 ——
这就是 R11 要求的"token 服务端可单点吊销"。

### 6.2 采集端:每次请求签名

```
POST /ingest/events
X-LifeIn-User:      <user_id>
X-LifeIn-Device:    <device_id>
X-LifeIn-Timestamp: <unix 秒>
X-LifeIn-Signature: <hex(HMAC-SHA256(secret, signing_string))>
```

```
signing_string = METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + sha256_hex(BODY)
```

`secret` 是 `credentials`(`kind=collector`, `scope=ingest`)那条里的
`secret` 字段,32 字节随机数的 base64,在设备上存安卓 Keystore。

校验顺序,任何一步不过一律 **401 且不说原因**(理由与企微回调那条一样 ——
告诉对方错在哪一步等于帮他调试):

1. 三个头齐全,`timestamp` 与服务端时间相差不超过 `INGEST_MAX_SKEW_S`
2. 按 `(user_id, device_id, kind=collector)` 取出未吊销的凭据,取不到即拒
3. 用取出的 secret 重算签名,**常数时间比较**

**重放保护靠两条,都不是 nonce:** 五分钟时间窗,以及
`raw_events (user_id, source, external_id)` 的唯一键 ——
重放一次上报写不进任何新东西([§2.6](#26-去重的两个层次))。
不存 nonce 是因为它要么无限增长要么得多一个清理任务,
而在幂等已经成立的地方,它挡不住任何新的东西。
**心跳是这条推理唯一的缺口**:五分钟内重放一次心跳,能让一台刚掉线的采集器
看起来还活着,最多延后五分钟告警 —— 接受这个代价,不为它引入 nonce 表。

### 6.3 查询端:长期凭据换短期 token

`POST /app/token` 用 §6.2 同样的签名方式,只是 secret 换成
`kind=app_device`(`scope=query`)那条。返回:

```json
{ "token": "v1.<base64url(payload)>.<hex 签名>", "expires_at": "2026-09-09T10:00:00+00:00" }
```

`payload` 是 `{"u": user_id, "d": device_id, "exp": <unix 秒>}`,
签名用的还是那台设备的查询密钥。有效期 `APP_TOKEN_TTL_H`(默认 24 小时)。

其余 `/app/*` 请求带 `Authorization: Bearer <token>`。校验:
解出 `u` 与 `d` → **回库取那台设备未吊销的查询凭据** → 用它验签 → 看 `exp`。

**为什么验签要回库读那条凭据**,而不是拿一把全局密钥签一个自洽的 JWT:
因为 R11 要求"token 服务端可单点吊销"。自洽的 token 在过期前谁也拦不住,
除非再建一张吊销表;而把签名密钥绑在设备凭据上,
`revoked_at` 一填,那台设备已经发出去的 token **下一次请求就失效**,
不需要第二张表,也不存在"忘了同步吊销名单"这种状态。

代价是每次请求多一次带索引的单行查询和一次解密。P1 的 QPS 是个位数,
这个代价不值得优化 —— 真到了要优化的时候,加缓存也必须带吊销的失效路径。

### 6.4 采集上报 `POST /ingest/events`

```json
{
  "device_id": "pixel-7a",
  "events": [
    {
      "channel": "notification",
      "source_app": "com.tencent.mm",
      "posted_at": "2026-09-08T10:11:12+08:00",
      "title": "项目组",
      "text": "老王:明天下午三点开会",
      "external_id": "0f2c9a"
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | :-: | --- |
| `channel` | ✓ | `notification` / `sms`。P1 只有前者会被放行(白名单里没有短信来源) |
| `source_app` | notification | 包名。白名单按它匹配 |
| `sender` | sms | 发件号码。白名单按它匹配 |
| `posted_at` | ✓ | 通知**展示**的时间,带时区。它就是 `occurred_at` |
| `title` / `text` | ✓ / | 通知标题与正文,原样上报,设备端不解析(架构 §8.1) |
| `external_id` | ✓ | 设备生成的去重键。服务端会再拼上 `device_id`,跨设备不会撞 |

**服务端逐条按这个顺序处理,顺序本身是安全机制:**

1. **白名单**(`collector_whitelist`,默认拒绝)。不匹配就丢弃,计
   `not_whitelisted`。手机端已经过滤过一次,这里是第二道 ——
   [R10](05-risks.md#r10--手机端采集器的越权读取) 不假设手机端规则永远正确
2. **`purpose` 闸门**。P1 只放行 `purpose=message`;命中一条
   `purpose=transaction` 的规则也丢弃,计 `phase_not_open` ——
   记账链路 P2 才打开([产品定义 §6](01-product-spec.md#6-数据源)),
   这条闸门让"白名单提前配好"不等于"账目提前开始流入"
3. **验证码正则**(铁律 11 的服务端那一道)。命中就丢弃,计 `verification_code`。
   **不入库、不记原文**,日志里也只记条数
4. **归一化**:`kind=message`、`trust=external`、`title` 进标题、`text` 进 body。
   系统折叠出来的"[3 条] ……"打上 `aggregated`
5. **入库**,`ON CONFLICT DO NOTHING`,重复的计 `duplicates`

**交易类多一步(P2)**:`purpose=transaction` 的来源放行之后,
金额、卡号后四位、方向**用正则抠**(铁律 9),抠不出金额的计 `not_a_transaction`
丢弃 —— 那多半是银行的营销短信,而 ADR-012 的第 3 层本来就要区分"营销"。
**丢弃不是进待确认**:进队列的话你会收到一堆"优惠券待领取"。

响应:

```json
{
  "accepted": 3,
  "duplicates": 1,
  "dropped": {"not_whitelisted": 2, "verification_code": 1, "phase_not_open": 0,
              "malformed": 0, "not_a_transaction": 0}
}
```

**返回计数不违反"只能写"**:这些数字是设备刚刚提交的那一批的处理结果,
不是库里已有的任何数据。采集端需要它才能在状态面板上说清"我发出去的东西
有没有被收下",而这正是[静默失败](05-risks.md#r8--数据源格式变动)最需要被打破的地方。

### 6.5 心跳 `POST /ingest/heartbeat`

```json
{ "device_id": "pixel-7a", "app_version": "1.0.0", "android_version": "14", "listener_enabled": true }
```

upsert 进 `collector_heartbeat`,`last_seen_at` 由**服务端**取当前时间 ——
设备时钟不可信,而这个值是掉线告警的判据。

`listener_enabled=false` 是一种特殊的活着:进程还在,但通知监听权限被系统收走了。
**这种情况要照样告警** —— 它和掉线的后果一样(采不到东西),
但表现更隐蔽(心跳一切正常)。

超过 `COLLECTOR_HEARTBEAT_TIMEOUT_M`(默认 60)分钟没有心跳即告警,
对应 P1 验收标准里的"采集器掉线能在 1 小时内告警"。

### 6.6 待办读写 `/app/todos`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app/todos?until=<ISO8601>&limit=50` | 未完成的待办与日程。`until` 不给就按用户时区算到今天结束 —— **桌面小组件用的就是这个默认值** |
| POST | `/app/todos` | 用户手动加一条。`source=user`,不带 provenance |
| POST | `/app/todos/{id}/status` | `{"status": "done"}` 或 `{"status": "cancelled"}` |

返回的每一条都带 `device_ref` 与 `synced_at`。
**App 必须把 `synced_at` 为空的日程显示成"未写入日历"** ——
[ADR-020](04-tech-decisions.md#adr-020--待办与日程落在自己的-app企微退出主链路) 的
"看得见的延迟可以接受,静默丢失不行"就落在这一个字段上。

#### 为什么 App 的写操作不过治理层网关

[铁律 4](../AGENTS.md#1-铁律) 说的是"**能力层的工具不许被编排层直接调用**"。
App 里用户自己点的"新建 / 完成 / 撤销"不属于编排层 ——
那是用户在自己的地盘上直接动自己的数据,和 `admin` CLI 写凭据是同一类动作。

判据在 `todos.source` 这一列上,它本来就把两种来源分开了:

| 来源 | 走哪 | 为什么 |
| --- | --- | --- |
| `source=agent` | **必须过网关** | `tool_calls` 里那条带 `rollback_info` 的记录,是"这条待办哪来的、怎么撤"唯一的答案 |
| `source=user` | 直接调仓储 | 用户知道自己点了什么,回滚就是再点一下。为它伪造一个 agent 名字,只会让审计表里多出一个查不到契约的调用方 |

**唯一的例外是确认待确认队列**(§6.7):那条内容是 agent 提出来的,
只是由用户点头,所以它照旧过网关,`agent` 记的是当初把它排进队列的那个。

### 6.7 待确认队列 `/app/pending`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app/pending?limit=50` | 还没处理、还没过期的 |
| POST | `/app/pending/{id}/resolve` | `{"action": "confirm"}` 或 `{"action": "reject"}`,confirm 可带 `payload` |

返回体里给的是 `payload` 原样,**不是渲染好的文本**([§2.7](#27-pending_confirmations--统一待确认队列)
那句"人看的那份由展示层从它渲染")。同时给 `target_table`:

> **App 遇到不认识的 `target_table` 只展示,不给确认按钮。**
> P1 只有 `todos` 一种,P2 的 `transactions` 进来时,旧版本 App 会把它列出来
> 但不让点 —— 这样服务端加一类待确认不必等 App 发版,而没发版的 App
> 也不会拿错误的形状去确认。

`action=confirm` 带 `payload` 就是"修改后确认"(`edited`),不带就是原样确认。
两条硬规则:

1. **写入目标表与更新 `status` 在同一个事务里**,由 `pending.confirm(writer=…)`
   保证(§2.7 那条硬要求)。写入失败就整个事务回滚,那条仍然是 `pending`,
   不会出现"确认了但没写进去"
2. **客户端只能改人看得懂的那几项**:`title` / `notes` / `starts_at` / `ends_at`。
   `provenance` 与 `created_by_agent` 一律沿用队列里那份 ——
   出处不由客户端说了算(铁律 5)

### 6.8 日历回报 `/app/calendar`

这是 ADR-020 里"**副作用发生在服务端之外**"那件事的接口面。

`GET /app/calendar/queue` 返回两个列表,一个都不能少:

```json
{
  "to_create": [{"todo_id": "…", "title": "…", "starts_at": "…", "ends_at": null, "notes": null}],
  "to_delete": [{"todo_id": "…", "device_ref": "…"}]
}
```

| 列表 | 取自 | 设备该做什么 |
| --- | --- | --- |
| `to_create` | `kind=schedule` 且 `status=open` 且 `synced_at IS NULL` | 写 `CalendarContract`,回报 event id |
| `to_delete` | `status=cancelled` 且 `device_ref IS NOT NULL` | 删掉那条日历事件,回报删完了 |

**`status=done` 的不进 `to_delete`。** 一场已经开完的会该留在日历里 ——
删掉它等于篡改历史,而用户看日历正是为了回想"那天我干了什么"。

`POST /app/calendar/report`:

```json
{ "todo_id": "…", "action": "created", "device_ref": "系统日历的 event id" }
```

- `action=created` 就 `mark_synced`,`device_ref` 落库。**那个 id 就是 L2 的回滚信息**
- `action=deleted` 就清空 `device_ref` 与 `synced_at`。清空之后这条自然掉出
  `to_delete`,不会被反复要求删除。审计里那条 `rollback_info` 仍然留着 event id,
  所以"设备上曾经有过一条"查得出来

**幂等责任在设备端**:服务端只记最后一次回报,同一条 todo 写两次日历会留下
两条事件而服务端只知道后一条。App 端以 `todo_id` 为主键记住自己写过什么,
**先查本地再写日历**。

### 6.9 采集器状态与白名单 `/app/collector`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app/collector/status` | 每台设备的心跳 + 当前白名单 |
| POST | `/app/collector/whitelist` | 加一条来源 |
| POST | `/app/collector/whitelist/{id}/enabled` | `{"enabled": false}` 停用 |

**没有删除。** 停用即不放行,而留着那一行能回答"曾经放行过谁" ——
排查 [R10](05-risks.md#r10--手机端采集器的越权读取) 那类问题时,那是唯一的线索。

白名单在**查询端**读写,不在采集端 —— 采集端只能写。
App 打开时把它同步到本地,采集器按本地那份过滤;
**服务端入库前照样再过一次**,两道是独立的(架构 §8.3)。

### 6.10 记忆与实体浏览 `/app/memory`

[03 的 P1 查看侧](03-roadmap.md#p1--记忆层安卓-app-与主动触发)要求
"记忆与实体浏览:查看、否定、纠正 `facts` 条目"。这一组就是它。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app/memory/facts?q=&limit=50` | 当前成立的事实。`q` 给了就按字面搜 |
| POST | `/app/memory/facts/{id}/confirm` | 用户说这条对 |
| POST | `/app/memory/facts/{id}/negate` | 用户说这条不对 |
| POST | `/app/memory/facts/{id}/correct` | `{"statement": "改过的说法"}` |
| GET | `/app/memory/entities?q=&limit=20` | 实体浏览,**只读** |

**返回体里带来源。** 每条事实给 `provenance`(`raw_events.id` 数组),
同时在 `sources` 里给那些事件的标题与时间:

```json
{
  "facts": [
    {"id": "…", "statement": "不吃香菜", "confidence": 0.6,
     "confirmed_by_user": false, "provenance": [812],
     "created_by_agent": "memory"}
  ],
  "sources": {"812": {"source": "email", "title": "周五聚餐", "occurred_at": "…"}}
}
```

这不是锦上添花:[P1 的退出条件](03-roadmap.md#退出条件-1)写着
"**记忆里开始出现你不认可又说不清来源的条目 → provenance 链路有漏**"。
让来源和事实并排显示,是那句话唯一能被日常验证的形式 ——
翻库查 `provenance` 谁也不会天天做。

**三条规则,都不是可选的:**

1. **否定只标记,不删除。** 删了明天同一条会被重新推断出来
   ([R7](05-risks.md#r7--记忆污染))。`negated_by_user` 的记录还是
   抽取 agent 的负样本来源 —— 和 `pending_confirmations` 的 `rejected` 同理
2. **纠正 = 否定旧的 + 用同一份出处写一条新的**,在**同一个事务**里。
   `provenance` 由服务端从旧那条继承,**客户端给不了** ——
   出处不由客户端说了算([铁律 5](../AGENTS.md#1-铁律),和 §6.7 那条同源)。
   改过的那条 `confirmed_by_user = true`:用户亲手写的话不该再被
   external 的 0.6 上限压着
3. **确认是突破 0.6 上限的唯一路径**([§2.3](#23-facts--事实库))。
   外部内容推断出来的事实置信度封顶 0.6,只有人点头才能更高

**实体只读。** 别名归并走规则(铁律 9),不该在手机上手工编 ——
[06 §2.2](#22-entities--entity_aliases--实体库) 那套证据累积机制会被手工改乱,
而它错了没有任何外部表现。要改先改规则。

### 6.11 账本、报表与预算 `/app/ledger`

[03 的 P2 查看侧](03-roadmap.md#p2--记账与账单自动化)要求
"账本浏览、月度报表、预算与超支查看"。这一组是它。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/app/ledger/transactions?from=&to=&category=&q=&limit=100` | 账目列表,按时间倒序 |
| GET | `/app/ledger/report?period=2026-08` | 月度报表(数字与评语) |
| GET | `/app/ledger/budgets` | 每条预算当期花到哪儿了 |
| PUT | `/app/ledger/budgets` | `{"category": "餐饮"或 null, "amount": "1500.00", "alert_threshold": 0.9}` |
| DELETE | `/app/ledger/budgets?category=餐饮` | 删一条(不给 category 就是删总预算) |
| PATCH | `/app/ledger/transactions/{id}` | 改分类或商户,**只这两样** |
| POST | `/app/ledger/transactions` | 手动补一笔(§6.12) |
| DELETE | `/app/ledger/transactions/{id}` | 删一笔记错的 |

**`PATCH` 只让改分类和商户,不让改金额和时间。**
金额和时间来自银行短信或对账单,它们是这个系统里最不该被手改的两个字段 ——
改了之后账本和银行对不上,而对不上的时候你没有办法知道是谁改的。
记错了就删掉重记(那会留下两条审计记录),不是就地改。

**改分类会写回 `merchant_rules`,`created_by='user'`。**
用户改一次,以后这个商户就一直归到那一类(ADR-008),而且模型不能再改回去。
这是规则表最有价值的一条入口 —— 比模型自己沉淀的那些准得多。

**报表不现算。** `GET /app/ledger/report` 直接读那个月的统计,
评语来自 `monthly_report` agent 上次跑的结果。手机上点开报表就现调一次模型
既慢又贵,而**同一个月的评语每次点开都不一样会让人以为数字也在变**。

```json
{
  "period": "2026-08",
  "total": "3120.50", "count": 42, "last_total": "2800.00",
  "categories": [{"category": "餐饮", "total": "1200.00", "count": 20,
                  "last_total": "900.00"}],
  "merchants": [{"merchant": "星巴克", "total": "380.00", "count": 8}],
  "uncategorized": "0.00",
  "reconciled_ratio": 0.8,
  "notes": ["餐饮花得比上月多"]
}
```

**金额一律是字符串。** JSON 的 number 是双精度浮点,`38.50` 传过去可能变成
`38.499999999999996`,而账本上出现那个数字比出现一笔错账更让人不信任。
安卓端用 `BigDecimal` 接。

### 6.12 手动补一笔与拍照记小票 `/app/ledger`

[03 的 P2](03-roadmap.md#p2--记账与账单自动化) 还要求"拍照识别小票、手动补一笔"——
现金和纸质票据那条长尾,实时通知那一路永远采不到。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/app/ledger/transactions` | 手动补一笔 |
| POST | `/app/ledger/receipts` | 上传小票照片,回一份**待确认的**解析结果 |

**手动补的那笔不过网关。** 06 §6.6 那张表的判据是"谁提的":
用户自己填的金额和商户,不需要 agent 的审计链,也没有什么可回滚的 ——
他填错了自己就会改。`transactions.source_event_id` 指向一条
`trust=user_input` 的 `raw_events`,那是它的出处([铁律 5](../AGENTS.md#1-铁律))。

**小票识别的结果一律进待确认,不直接入账。**
识别出来的金额可能是"实付"也可能是"原价",可能把桌号当成金额 ——
而 03 那条"误记率 = 0"不区分错误来自模型还是来自 OCR。

```json
{
  "pending_id": "…",
  "parsed": {"amount": "38.50", "merchant_raw": "星巴克", "occurred_at": "…"},
  "confidence": 0.7,
  "reason": "low_confidence"
}
```

**小票原图不留。** 解析完就丢,只留结构化字段
([R10](05-risks.md#r10--手机端采集器的越权读取) 对交易类的口径:
只保留金额、时间、卡号后四位、商户)。一张小票照片上可能有同行的人、
店里的其他客人、你的手 —— 而记账一样都用不上。

**识别放在手机上,照片不上传**(ADR-021 的 2026-09 补充)。
决定它的是一条:照片里有的东西比一笔账多得多 —— 同行的人、邻桌、你的手。
**服务端从来没收到过的东西,不需要承诺"不留"。**

### 6.15 一次性换取码配码 `POST /enroll/claim`(P4)

**今天的配码方式有一个洞。** `admin issue-device` 打出来的二维码里是
**明文的两把密钥**。自己扫没问题;而 [03 的 P4](03-roadmap.md#p4--多用户托管)
说朋友要用它配码,那张图会走微信发过去 —— **等于把密钥发在聊天里**,
而微信的聊天记录会漫游、会备份、会被截图。

所以二维码里换成一张**一次性的换取码**,密钥由 App 自己去换:

```
POST /enroll/claim            ← 不带任何认证。它换的就是认证
{
  "code": "<二维码里那一串>",
  "device_id": "<App 自己生成的>",
  "app_version": "1.0.0"
}
→ 200 {"user_id": "...", "collector_secret": "...", "query_secret": "...",
       "base_url": "..."}
→ 401 (空)  码不对、用过了、或者过期了 —— **三种不区分**
```

| 性质 | 为什么 |
| --- | --- |
| **一次性** | 换过一次立刻作废。截图被别人拿到时,要么你已经换过了(他换不了),要么你还没换(你会发现自己换不了) |
| **短命**(默认 10 分钟) | 配码是一个当面或即时的动作。十分钟够走完,而聊天记录里躺三天的码等于没有一次性 |
| **不带认证** | 它换的就是认证 —— 所以**它是这个系统里唯一一个不需要凭据的写入口**,也因此是唯一需要防爆破的 |
| **失败一律空 401** | 码不对、用过了、过期了长得一样。区分等于告诉对方"这个码存在过" |

**`device_id` 由 App 生成并上报**,不再由人现编一个名字。人编的名字会重复
(两个人都叫 `phone`),而重复的 `device_id` 意味着**吊销一台会连带吊销另一台**。

**防爆破靠码本身的长度,不靠限流。** 码是 128 位随机数,穷举不现实;
而限流要存计数状态,那又是一张表。**但换取失败要记进日志** ——
连续的失败是"有人在扫这个接口"唯一的信号。

### 6.16 状态码约定


| 码 | 什么时候 | 注意 |
| --- | --- | --- |
| 200 | 处理完了(**哪怕全被丢弃**) | 丢弃的条数在响应体里,不用状态码表达 |
| 401 | 签名或 token 不过 | **不带任何原因**,响应体是空的 |
| 404 | 那条 todo / pending 不属于这个用户,或不存在 | 两者不区分 —— 区分等于告诉对方"这个 id 存在" |
| 409 | 待确认那条已经被处理过或已过期 | 两个入口同时点确认是正常的用户行为,不是错误 |
| 422 | 请求体形状不对 | FastAPI 默认行为,只对已经通过认证的请求发生 |

### 6.17 这份契约还缺什么

诚实记录,不要假装完备:

- ~~**账本、报表、预算、待确认里的账目**~~ ✅ 已定,见 §6.11 与 §6.12
- ~~**记忆与实体浏览**(查看、否定、纠正 `facts`)~~ ✅ 已定,见 §6.10
- **审批卡片的回调** —— P3,走企微不走 App(ADR-001)
- ~~**小票识别放哪一端**~~ ✅ 已定:手机上识别,照片不上传(ADR-021 的 2026-09 补充)
- ~~**评测集格式**(agent 契约第 5 项)~~ ✅ 已定,见 §7

---

## 7. 评测集格式

[§4](#4-这份文档还缺什么) 最后欠的那一条,也是
[ADR-013](04-tech-decisions.md#adr-013--agent-按领域分工同进程运行) 五项契约里的第五项。
每个 agent 注册时都声明了 `evalset` 路径,但格式一直没定 ——
**声明了却没有格式,等于那一项没有落地。**

它存在的理由只有一个,ADR-013 说得很直白:
**改一个 agent 的 prompt 时,要能立刻知道有没有把另一个弄坏。**

### 7.1 一行一个用例(JSONL)

```json
{"id": "planner-003", "input": {…}, "expect": [{"path": "items", "op": "count", "value": 1}], "note": "群里那句"改到周四"不该建成新日程"}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 稳定不变。改期望时保留 id,那样"这条什么时候开始不过的"查得出来 |
| `input` | 直接喂给 agent 的输入,形状就是它契约里的 `inputs` 模型 |
| `expect` | 断言数组,**全部要过**。空数组不允许 —— 没有期望的样本测不出任何东西 |
| `note` | 为什么是这个期望。**三个月后看不懂的用例等于没有** |

### 7.2 断言只有五种,都是机械可判的

| `op` | 判什么 | 用在哪 |
| --- | --- | --- |
| `equals` | 相等 | 路由、分类、布尔判断 |
| `contains` | 字符串含子串 / 数组含元素 | "摘要里提到了体检" |
| `count` | 数组长度 | "只该提出一条" |
| `absent` | 为空、None、或长度 0 | **最重要的一种**:"不该建出任何日程" |
| `at_most` | 数值上界 | token 数、条目数的天花板 |

`path` 用点号和方括号走进输出:`items[0].route`、`facts[1].statement`。
走不到就是失败,**不是跳过** —— 路径写错和结果不对要一样地红。

**`absent` 是唯一的例外**:对它来说走不到就是"确实没有",算通过。
`items[0]` 在空列表上走不到,而那正是"一条都不该提出来"要表达的意思。

**期望里不许出现自由文本比对。** 摘要的期望不能是"和这段话一模一样",
只能是"提到了 X"、"没有编造不存在的事件 id"。ADR-013 那句
"结构化才能被代码校验,自由文本没法测"在这里是硬约束:
写不出机械可判的期望,说明那条样本还没想清楚该验什么。

### 7.3 负样本从哪来:不是编的,是攒的

[§2.7](#27-pending_confirmations--统一待确认队列) 说 `rejected` 与 `expired`
的记录永不删除,理由就是这里 —— **用户拒绝过什么,正是这个 agent 最该学会不做的事。**
`facts.negated_by_user` 同理。

所以负样本靠导出,不靠想象:

```bash
python -m lifein.evals export --user <uuid> --agent planner   # 拒绝过的待确认
python -m lifein.evals export --user <uuid> --agent memory    # 否定过的事实
```

导出的用例默认期望是 `absent`:**那件事当初就不该被提出来**。
导完要人看一遍再合并进 `evals/` —— 用户拒绝的原因不总是"agent 错了",
也可能是"这事我自己记得",后一种不该进负样本。

### 7.4 跑法与它的代价

```bash
python -m lifein.evals planner          # 跑一个 agent 的评测集
python -m lifein.evals all              # 全部
```

**它会真的调外部模型,真的花钱。** 所以:

- **不进 CI,也不进 `pytest`。** 是手动动作,改 prompt 之后跑一次
- 跑完打印通过率与花掉的 token。**通过率不是越高越好** ——
  100% 通常说明样本太容易,而不是 agent 太好
- 样本里有你的真实邮件和群消息,所以 `evals/*.jsonl` 与
  [R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露) 同级敏感:
  **它们不进公开仓库**(`.gitignore` 里已经排除),只有 `*.example.jsonl` 进

### 7.5 每个 agent 至少 20 条(ADR-013)

现在远远不够。补样本的顺序按"错了最贵"排:

| agent | 优先补什么 |
| --- | --- |
| `planner` | **误报进日历的**(P1 验收要求这一项为 0),以及拒绝过的待确认 |
| `memory` | 否定过的事实,以及"说不清来源"的那些 |
| `daily_digest` | 你当天判断"这条没用"的摘要 |
| `qa` | 答错和答不上来的问题 |

**攒不到 20 条不是拖延,是这个 agent 还没被真正用过。** 那种情况下
先去用它,不要为了凑数编样本 —— 编出来的样本会让评测通过率变好看,
而那正是这套机制要防的事。

### 2.13 enrollment_codes · 一次性配码换取码

[06 §6.15](#615-一次性换取码配码-postenrollclaim-p4) 那个接口的存储。P4 第 1 片。

```sql
CREATE TABLE enrollment_codes (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    code_hash   TEXT NOT NULL,   -- sha256(码)。**码本身只在生成那一刻显示一次**
    purpose     TEXT NOT NULL DEFAULT 'all'
                  CHECK (purpose IN ('all', 'collect', 'query')),
    base_url    TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    claimed_at  TIMESTAMPTZ,     -- 换过了。**不删行**:留痕迹
    claimed_by  TEXT,            -- App 自己生成并上报的 device_id
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (code_hash)
);
CREATE INDEX ON enrollment_codes (user_id, created_at DESC);
```

**存哈希不存码**,和存密码同一个道理:库被拖走时,里面的东西不该直接可用。

**`claimed_at` 而不是 `DELETE`。** "这台设备是什么时候、用哪个码配上的"是排查
"我的号被别人配走了吗"唯一的线索,而删掉那一行之后这个问题就没法回答了。
过期且**没被用过**的那些才会被清掉。

**换取那一步是一条 `UPDATE ... WHERE claimed_at IS NULL AND expires_at > now`。**
两个人同时扫同一张图时只有一个能换走 —— 先查再写的话两个都能换,
而那时你和他各有一套**都有效**的密钥,你不会发现任何异常。

### 2.14 console_links · 控制台的一次性链接

[Web 控制台](../lifein/api/console.py)的认证。P4 第 9 片。

```sql
CREATE TABLE console_links (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    token_hash TEXT NOT NULL,   -- sha256(token)。明文只在发出那一刻有
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (token_hash)
);
```

**浏览器打开一个链接时带不了 `Authorization` 头**,所以 App 那套 Bearer token
在 Web 上用不了。三条路里选了"一次性链接":让人粘贴 token 非技术背景的人做不到,
而用户名密码 + 会话 cookie **多一个认证面就多一处会被攻破的地方**(R11)。

**它是短命的多次可用,不是一次性的。** 页面上有链接要点(导出、隐私说明),
每点一次就换一张的话,每次点击都要回 App 一趟。安全性靠那十五分钟,
不靠"只能用一次"。

**过期的直接删。** 和 `enrollment_codes` 不一样:那张要留"谁什么时候配上的"
的痕迹,而"某人打开过控制台"不是那种事。
