# 07 · 配置清单

配置项此前散落在五份文档里,谁也说不出"跑起来到底要准备什么"。这份文档就是那张单子。

**凭据一律不进仓库。** `.env` 在 `.gitignore` 里,示例文件是 `.env.example`,
里面只放键名和格式说明,不放任何真实值。

---

## 1. 配置分三处,不要混

| 放哪 | 装什么 | 判据 |
| --- | --- | --- |
| **环境变量** | 系统级凭据与启动参数 | 进程起来之前就必须知道的 |
| **`credentials` 表**(加密) | 用户级凭据 | 每个用户各有一份,P4 后按 `user_id` 隔离 |
| **数据库配置表** | 用户可自行调整的 | 用户能在 App 里改的,不该要求重启 |

最容易做错的是把 IMAP 授权码写进环境变量 —— P0 单用户时看着没问题,
P4 来第二个人就得推倒重来。**用户级凭据从第一天就进 `credentials` 表**
([06 §2.10](06-data-model.md#210-其余表))。

---

## 2. 环境变量

**“必填”列写 `✓` 表示 P0 启动就必须有,写 `P1` 表示到那一期才校验。**
P0 不该逼着人先生成一把用不到的密钥 —— 提前存在的凭据只是提前多一份泄露面。

### 2.1 基础

| 键 | 必填 | 示例 / 默认 | 说明 |
| --- | :-: | --- | --- |
| `DATABASE_URL` | ✓ | `postgresql://…/lifein` | PostgreSQL 15+,需装 `pgvector` |
| `APP_HOST` | | `127.0.0.1` | **默认只监听本地**,公网访问走反向代理 |
| `APP_PORT` | | `8000` | |
| `TZ` | ✓ | `Asia/Shanghai` | 影响"昨日邮件""今日日历"的窗口划分 |
| `LOG_LEVEL` | | `INFO` | |

### 2.2 加密主密钥

| 键 | 必填 | 说明 |
| --- | :-: | --- |
| `MASTER_KEY` | ✓ | 32 字节,base64。加密 `credentials` 表的字段 |
| `MASTER_KEY_VERSION` | ✓ | 整数,写入 `credentials.key_version` |
| `MASTER_KEY_PREVIOUS` | | 轮换期间的旧密钥,解密用;轮换完成后删除 |

> **主密钥泄露等于全部凭据泄露。** 它不进仓库、不进日志、不进备份的明文部分。
> 轮换流程:配 `MASTER_KEY_PREVIOUS` → 逐条重加密并递增 `key_version` →
> 确认无残留旧版本 → 删除 `MASTER_KEY_PREVIOUS`。
> 没有 `key_version` 就没法平滑轮换,所以它从 P0 就在表里。

### 2.3 外部模型

| 键 | 必填 | 示例 / 默认 | 说明 |
| --- | :-: | --- | --- |
| `LLM_BASE_URL` | ✓ | `https://…/v1` | OpenAI 兼容接口 |
| `LLM_API_KEY` | ✓ | | |
| `LLM_MODEL` | ✓ | | 换厂商只改这两行 |
| `LLM_TIMEOUT_S` | | `60` | |
| `LLM_MAX_RETRIES` | | `2` | |
| `EMBEDDING_MODEL` | P1 | | 换它要重算全部向量,见 `embeddings.model` |
| `EMBEDDING_DIM` | P1 | `1024` | 与建表时的 `VECTOR(n)` 必须一致 |
| `LLM_PRICE_PROMPT_PER_1K` | | `0` | 输入每千 token 单价(元),用于 `tool_calls.cost_cny` |
| `LLM_PRICE_COMPLETION_PER_1K` | | `0` | 输出每千 token 单价(元) |

> 单价默认 0,也就是**不记成本**。填了才算 —— 各家计价差别大且会变,
> 与其在代码里维护一张价目表,不如让部署的人填一次。填错只会让成本统计
> 不准,不影响功能。

> **部署前必须确认供应商是否将请求用于训练**,优先选可关闭的接口,
> 并把确认结果记在部署记录里 —— 这是 [R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露)
> 的措施之一,不是口头确认一次就算完。

### 2.4 企业微信(整组可选)

| 键 | 必填 | 说明 |
| --- | :-: | --- |
| `WECOM_CORP_ID` | | |
| `WECOM_AGENT_ID` | | 自建应用 |
| `WECOM_SECRET` | | |
| `WECOM_CALLBACK_TOKEN` | | 回调签名校验 |
| `WECOM_CALLBACK_AES_KEY` | | 回调消息解密 |
| `WECOM_CALENDAR_ID` | | 要读取的日历 `cal_id`,见下 |

> **为什么整组可选**:企微要配"企业可信 IP"(不配发不了消息),必须先配
> 可信域名或接收消息服务器 URL —— 两个都要公网域名。对一台家里的机器,
> 这是过不去的坎。
>
> [ADR-018](04-tech-decisions.md#adr-018--微信推送走-ilink-bot-api企微降为兜底与审批入口)
> 之后推送和问答都能走微信 iLink,所以企微降为可选。
>
> **半套配置视同没配** —— 只配一半只会在运行时炸得莫名其妙,不如当它不存在,
> 让降级路径接手。
>
> **代价要说清**:没有企微就没有兜底推送通道(微信会话过期时当天没有摘要),
> 也没有日历数据源(P0 只剩邮箱)。有域名之后补上即可,不用改任何代码。

日历复用同一套凭据(企微日程 API),但**需要多一个 `cal_id`**:
企微的日程读取接口是按日历取的,而自建应用没有"列出我的全部日历"这个能力。
部署时在企微里建一个专用日历,把它的 `cal_id` 填进来 —— 这样也顺带划清了边界,
系统只看这一个日历,不去翻你别的日程。

> **待实测确认**:日程读取接口的确切路径与分页参数,以及日程对象里
> `start_time` 的单位(按秒级时间戳实现)。P0 部署时对着真实响应校一遍,
> 校完把结论写回这里。归一化那部分不受影响 —— 它只依赖字段含义,不依赖接口形态。

**推送给谁不在环境变量里**,在 `users.wecom_userid`([06 §2.10](06-data-model.md#210-其余表))。写进环境变量的话,P4 来第二个人就得改部署。

### 2.5 采集入口

安卓 App 在 P1 才上线,这一组 P0 留空即可。

| 键 | 必填 | 默认 | 说明 |
| --- | :-: | --- | --- |
| `INGEST_MAX_SKEW_S` | | `300` | 超出时间偏移的请求拒收,防重放 |
| `APP_TOKEN_TTL_H` | | `24` | App 短期 token 有效期 |

> **`INGEST_SECRET` 已删除**(原本写在这里,值是一把全局的采集签名密钥)。
> 签名密钥改为**按设备签发**,和查询凭据一样落在 `credentials` 表里
> (`kind=collector`, `scope=ingest`,见 §3 与 [06 §6.2](06-data-model.md#62-采集端每次请求签名))。
>
> 一把全局密钥办不到 [R11](05-risks.md#r11--app-直连服务端的认证面) 要的两件事:
> **按设备单点吊销**(手机丢了只能换掉所有设备的密钥),
> 以及 **P4 的用户隔离**(所有人共用一把,谁上报的都验得过)。
> 而按设备签发不需要任何新表 —— `credentials` 的 `device_id` 列从 P0 就在。

### 2.6 行为参数

有默认值,通常不用改;改了要能解释为什么。

| 键 | 默认 | 出处 |
| --- | --- | --- |
| `DAILY_DIGEST_AT` | `08:00` | [产品定义 §5](01-product-spec.md#5-主动性双模式) |
| `MAX_PROACTIVE_PUSH_PER_DAY` | `3` | 频率闸门硬上限 |
| `SHADOW_MODE_DEFAULT` | `true` | 新规则一律先影子模式 |
| `TXN_DEDUP_WINDOW_S` | `300` | 跨渠道去重窗口,[06 §2.6](06-data-model.md#26-去重的两个层次) |
| `TXN_MIN_CONFIDENCE` | `0.8` | 低于此值进待确认,不入账 |
| `PENDING_EXPIRE_DAYS` | `30` | 待确认队列过期 |
| `APPROVAL_EXPIRE_H` | `24` | L3 审批过期 |
| `COLLECTOR_HEARTBEAT_TIMEOUT_M` | `60` | 超时即告警,P1 验收要求 1 小时内 |
| `ALERT_CHANNEL` | `email` | 告警走兜底通道,不走可能已经挂掉的推送通道 |

### 2.7 邮件兜底通道(P1,可选)

降级链的最后一环,也是告警的出口。**配了 `SMTP_HOST` 就必须把这一组配全** ——
配一半的话降级链会多出一条必然失败的通道,而失败的表现是每天多一条告警,
告警变成噪音之后真出事的那次就被忽略了(启动时校验,配不全直接起不来)。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | 空 | 留空 = 不启用邮件通道,降级链只有微信与企微 |
| `SMTP_PORT` | `465` | 465 走 SSL,587 走 STARTTLS |
| `SMTP_USERNAME` | 空 | 登录名,通常就是邮箱地址 |
| `SMTP_PASSWORD` | 空 | **授权码,不是登录密码**(和 IMAP 那把是同一类东西) |
| `SMTP_FROM` | 同 `SMTP_USERNAME` | 发件地址 |
| `SMTP_TO` | 空 | 收件地址 |
| `SMTP_USE_SSL` | `true` | 配 587 时改成 false |

**`SMTP_TO` 不建议填成被采集的那个邮箱。** 填了也不会回环 —— 系统发出去的信
都带 `X-LifeIn-Push` 头,采集侧见到就跳过 —— 但收件箱里会多一份自己给自己的
抄送,而那个收件箱本来就是要拿来看正事的。

---

## 3. 用户级凭据(存 `credentials` 表,加密)

| `kind` | `scope` | 内容 | 备注 |
| --- | --- | --- | --- |
| `imap` | `query` | 主机、端口、账号、**授权码** | 授权码不是登录密码 |
| `bill_archive` | `query` | 账单导出压缩包密码 | 支付宝/微信各一份 |
| `statement_pdf` | `query` | 信用卡对账单 PDF 打开密码 | 每家银行一份 |
| `collector` | `ingest` | 采集设备密钥 | **只能写不能读** |
| `app_device` | `query` | App 查询长期凭据 | 存安卓 Keystore,服务端可单点吊销 |

**`scope` 是 [R11](05-risks.md#r11--app-直连服务端的认证面) 的执行点。**
`ingest` 的凭据打到读接口一律拒绝 —— 手机丢了或 App 被逆向,拿到的采集密钥
读不出任何账本。

### IMAP 的两个坑

- **163 / 126 必须在 `SELECT` 前先发 `ID` 命令**,否则报 `Unsafe Login`。
  Python 的 `imaplib` 默认不允许在该状态发 `ID`,要先注册为 AUTH 态可用命令。
  QQ 邮箱无此要求。见 [ADR-011](04-tech-decisions.md#adr-011--数据源全面本地化)
- **授权码会在改账号密码后失效。** 连续认证失败必须告警,不能静默停采
  ([R9](05-risks.md#r9--单点自托管可用性))

---

## 4. 采集白名单(存表,用户可改)

白名单**不进环境变量** —— 用户要能在 App 里增删,不该要求重启
([架构 §8.4](02-architecture.md#84-查看侧))。

```sql
CREATE TABLE collector_whitelist (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL,
    match_type TEXT NOT NULL CHECK (match_type IN ('sms_sender','package_name')),
    pattern    TEXT NOT NULL,
    purpose    TEXT NOT NULL CHECK (purpose IN ('transaction','message')),
    enabled    BOOLEAN NOT NULL DEFAULT true,
    phase      TEXT NOT NULL,     -- P1|P2,控制分期放开
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, match_type, pattern)
);
```

**默认拒绝。** 不在白名单里的短信与通知,采集器根本不上报
([R10](05-risks.md#r10--手机端采集器的越权读取))。

| 阶段 | 放行什么 |
| --- | --- |
| **P1** | 仅微信(`com.tencent.mm`),`purpose='message'` |
| **P2** | 追加银行号段、支付宝、云闪付、银行 App、美团/京东等,`purpose='transaction'` |

先用一条低风险链路验证采集器能不能稳定活着,再把账目压上去。

### 验证码过滤

**手机端与服务端各过滤一次,两边用同一份正则。**

```
验证码|校验码|动态密码|verification code|\b\d{4,8}\b\s*(?:为|是)?\s*(?:您的)?(?:验证码|校验码)
```

命中即丢弃,不进队列、不入库。这份正则允许调整,
但**只能放宽匹配范围不能收窄** —— 收窄意味着更多验证码会流进系统。

**它不是环境变量,是两份代码常量**:服务端在
`lifein/sources/verification_code.py`,安卓端在 `VerificationCode.kt`。
写成配置项曾经是本节的说法,但那样两边就得**共用一份配置**,
而共用的那一刻,"双重丢弃"(铁律 11)就退化成了一道 ——
手机端那道过滤的全部价值,正在于它和服务端那道**独立失效**。
代价是改一次要动两处并重新发版,这个代价是有意付的。

---

## 5. 部署步骤

**一步一步怎么做在 [08 部署实操](08-deployment.md)** —— 包括企微后台点哪里、
邮箱授权码在哪生成、每一步怎么确认成功。

这份文档只回答"这个配置项是什么意思、判据是什么",不重复步骤 ——
两份都写一遍,迟早各自漂移。

---

## 6. 部署前检查清单

- [ ] `.env` 不在 git 里,`.env.example` 在
- [ ] `MASTER_KEY` 已生成,且**不等于**示例值
- [ ] PostgreSQL 装了 `pgvector`,`EMBEDDING_DIM` 与建表一致
- [ ] `APP_HOST` 是 `127.0.0.1`,公网访问走反向代理 + TLS
- [ ] 企微回调 URL 已实测能收到并验签通过
- [ ] IMAP 实测能登录(163 记得发 `ID`)
- [ ] **已确认 LLM 供应商是否将请求用于训练,结果记入部署记录**
- [ ] 告警通道实测能收到(故意让一次采集失败)
- [ ] 备份已配置,且**演练过一次恢复**(配了不算,演练过才算)
