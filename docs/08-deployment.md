# 08 · 部署实操

**这份文档是给第一次部署的人的,一步一步走。**
每个配置项**是什么意思**在 [07 配置清单](07-config.md);这里只讲**怎么拿到它、
怎么确认它对了**。两份不要都写一遍步骤,会各自漂移。

---

## 先知道一件事,能省不少事

> **每日摘要不需要公网。** 它是服务端主动往企微发消息 —— 出站请求,
> 家里的机器、笔记本都能跑。
>
> **只有问答需要公网**,因为企微得回调到你。
>
> 所以可以分两步:先跑通摘要拿到第一条推送([§1](#1-跑通摘要)),
> 等有服务器了再开问答([§2](#2-可选开问答))。
>
> **手机那半边是 P1 的事,在 [§5](#5-p1装上手机那半边)** —— 摘要的 14 天数完再装,
> 顺序反了会把那 14 天的样本口径搅了。
>
> **App 要一个手机够得着的 HTTPS 入口**,而家里那台机器没有 ——
> 所以 P1 收尾时服务端搬到了云服务器,见 [§6](#6-搬到云服务器p1-收尾)
> 与 [ADR-022](04-tech-decisions.md#adr-022--服务端搬到云服务器用已备案域名的子域名)。

P0 的验收标准是"你自己每天会不会看",而那个问题只需要摘要就能回答。
**先别为了问答去买服务器。**

---

## 1. 跑通摘要

### 1.0 在哪跑

| 方案 | 适合 | 代价 |
| --- | --- | --- |
| **本机** | 今天就想看到第一条 | 关机就没有摘要 |
| 云服务器 | 长期用 | 一台最小规格够了 |
| 家里常开的机器 | 省钱 | 出口 IP 会变,见 [1.2 第 6 步](#12-企业微信) |

内存给到 **1GB 以上**:服务端常驻约 200MB,数据库另算。

### 1.1 数据库

```bash
docker run -d --name lifein-db --restart unless-stopped \
    -e POSTGRES_PASSWORD=换成你的密码 \
    -e POSTGRES_USER=lifein \
    -e POSTGRES_DB=lifein \
    -p 5432:5432 \
    -v lifein-data:/var/lib/postgresql/data \
    pgvector/pgvector:pg16
```

**`-v lifein-data` 那行不要省。** 没有它,容器一删数据就没了。

用 `pgvector/pgvector` 这个镜像是因为 P1 的向量检索要 `pgvector` 扩展,
P0 用不上但表已经建了([06 §3](06-data-model.md#3-索引与迁移约定):
P0 就把全部表建出来)。

### 1.2 企业微信

**最麻烦的一步,先做完它,后面都是顺的。** 以下菜单位置以后台实际界面为准。

1. 手机装企业微信 → **注册企业**。个人就能注册,填个名字即可,不需要企业认证
2. 电脑打开企业微信管理后台
3. **我的企业 → 企业信息**,抄下 **企业ID** → `WECOM_CORP_ID`
4. **应用管理 → 自建 → 创建应用**
   - 应用名随便,可见范围选你自己
   - 抄 **AgentId** → `WECOM_AGENT_ID`
   - 抄 **Secret**(点"查看"后会发到你的企微上)→ `WECOM_SECRET`
5. **通讯录 → 点开你自己 → 账号** 那一栏,这个值是 `--wecom-userid` 的参数
6. **应用详情页往下拉 → 开发者接口 → 企业可信IP** →
   填运行服务的那台机器的**公网出口 IP**

   ```bash
   curl ifconfig.me     # 查出口 IP
   ```

   > **这一步不做,发消息一直报 `errcode=60020`。** 这是最常见的坑。
   > 家宽的出口 IP 会变,变了要回后台改 —— 如果发现某天摘要停了,先查这个。

7. **我的企业 → 微信插件** → 扫码关注 —— **这一步决定你平时在哪看消息**,
   见下面那一节

### 消息到底在哪收

| 方向 | 在哪 |
| --- | --- |
| **每日摘要、提醒** | **微信** —— 走 iLink,落在和 bot 的对话里 |
| **你提问** | **微信**,同一个对话直接问 |
| 微信没配 / 会话过期 | 推送自动降级到**企微**并告警;入站停止 |
| P3 的审批按钮 | 企微 |

**收和发都在微信里**,日常不用打开企业微信。

**企微是可选的**,配了就多一层兜底和 P3 的审批入口;没有公网域名配不了
也不影响 P0 —— 代价是微信会话过期那天没有摘要,以及没有日历数据源。

### 1.2b 微信推送(可选但推荐)

配了它,摘要就直接落到微信里,不用走企微转投。

```bash
python -m lifein.admin login-weixin --user <uuid>
```

它会:打印二维码 → 你用微信扫 → 手机上点确认 → **然后让你在微信里给这个
bot 发一句话**。最后那步不是多余的:扫码拿到的是 bot 身份,而"摘要推给谁"
是另一回事 —— 要等对方开口才知道该往哪发。

> 终端画不出二维码时会提示你扫上面的链接。中文 Windows 的控制台默认 GBK,
> 编不出二维码用的方块字符;想在终端里看的话先 `chcp 65001`。

**给 LifeIn 单独扫一个 bot。** 一个微信号可以连多个 bot,但**两个客户端
长轮询同一个 bot 会抢消息** —— 你已经在跑别的 agent 的话,不要共用。

配完**一定要验一次**:

```bash
python -m lifein.admin test-push --user <uuid>
```

它会真发一条,并告诉你**走的是哪个通道**。这一句很重要 —— 微信没配好会
静默落到企微,消息照样收得到,你会以为微信通了。

配完之后**在微信里直接问它话就行** —— 服务起来时会开一个后台线程收消息,
不需要公网回调。

> 会话过期时入站会停下来并告警,推送自动降级到企微(如果配了)。
> 重新扫一次即可:`login-weixin` 再跑一遍。

**做到这里就够跑摘要了。** 回调那两个配置(Token / EncodingAESKey)是问答用的,
[§2](#2-可选开问答) 再说。

### 1.3 邮箱授权码

| 邮箱 | 怎么开 |
| --- | --- |
| **QQ 邮箱** | 设置 → 账户 → `IMAP/SMTP服务` → 开启 → 短信验证 → 给你一个 **16 位授权码** |
| **163 / 126** | 设置 → `POP3/SMTP/IMAP` → 开启 IMAP → 设置**客户端授权密码** |

两件事要记住:

- **授权码不是登录密码。** 填错会一直认证失败
- **改了邮箱登录密码,授权码会失效。** 系统连续认证失败会告警
  ([R9](05-risks.md#r9--单点自托管可用性)),但你得知道去哪儿重新生成

### 1.4 外部模型

任何 **OpenAI 兼容**的供应商都行,不绑厂商
([ADR-002](04-tech-decisions.md#adr-002--p0p2-不引入-agent-编排框架))。
国内常见的几家(**具体地址以各家官方文档为准**):

| 供应商 | `LLM_BASE_URL` 大致形态 |
| --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` |
| 通义千问 | DashScope 的 compatible-mode 地址 |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` |
| Kimi | `https://api.moonshot.cn/v1` |

> **注册后去控制台确认"是否用你的数据训练"能不能关掉,能关就关。**
> 你的邮件正文会原样发过去 —— 自托管保护的是存储,**保护不了推理**。
> 这是 [R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露),
> 确认结果记进你自己的部署记录里。

### 1.5 主密钥

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

> **这一串泄露 = 全部凭据泄露。** 不进仓库、不进日志、不进备份的明文部分,
> 不发聊天工具。轮换流程见 [07 §2.2](07-config.md#22-加密主密钥)。

### 1.6 装

```bash
git clone https://github.com/hur26/LifeIn.git
cd LifeIn
git checkout dev          # 代码在 dev,main 只放正式版本

python -m venv .venv
.venv\Scripts\activate     # Windows;Linux/macOS 是 source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env       # Windows 用 copy
```

编辑 `.env`,**这些必填**:

```ini
DATABASE_URL=postgresql+psycopg://lifein:你的密码@127.0.0.1:5432/lifein
MASTER_KEY=1.5 生成的那串
MASTER_KEY_VERSION=1
LLM_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=...
WECOM_CORP_ID=...
WECOM_AGENT_ID=...
WECOM_SECRET=...
WECOM_CALLBACK_TOKEN=随便一个字符串
WECOM_CALLBACK_AES_KEY=随便一个 43 位字符串
```

后两个跑摘要用不上,但**配置校验要求它们存在**。先凑一个:

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode()[:43])"
```

> 缺配置会在**进程启动的那一刻**报错,并明确告诉你缺哪一项 —— 这是刻意的
> ([07 §1](07-config.md#1-配置分三处不要混))。自托管没有值班的人,
> 失败必须尽量早,不能等到当晚八点推摘要时才发现。

### 1.7 建表、建用户、配邮箱

```bash
alembic upgrade head

python -m lifein.admin create-user --name 你的名字 --wecom-userid 1.2 第 5 步抄的账号
# 记下打印出来的 uuid

python -m lifein.admin set-imap --user <uuid> --host imap.163.com --username you@163.com
# 这里会提示输授权码，不回显，粘贴后回车

python -m lifein.admin test-imap --user <uuid>
```

> **授权码从交互输入读,不做成命令行参数。** 参数会进 shell history、
> 出现在 `ps` 的输出里、被跳板机的会话录制录下来 —— 三处都不是能事后清干净的。

**`test-imap` 必须打印"登录成功"才往下走。** 它真连了一次邮箱 ——
163 那个 `ID` 握手对不对,只有这一步能证明。

### 1.8 跑一次

```bash
python -m lifein --once
```

顺利的话,**企业微信里应该收到一条摘要**。

不用等到早上八点才知道配没配对 —— 这个参数就是为了这件事留的。

### 1.9 让它每天跑

```bash
python -m lifein
```

进程挂着,每天 `DAILY_DIGEST_AT`(默认 `08:00`)自动跑。

关机再开也没关系:补偿机制会把漏掉的窗口补上,**最多补 3 天** ——
停机两个月重启时不该往外发六十条摘要,那比不发更糟
([06 §2.10](06-data-model.md#210-其余表) 的 `job_runs`)。

Linux 上建议交给 systemd 或 `docker run --restart unless-stopped`,
别用 `nohup` —— 这个系统最危险的失效方式是**安静**,而 `nohup` 挂了没人知道。

---

## 2. (可选)开问答

**只有这一步需要公网。** 做完之后可以在企微里直接问它话。

企微要求回调地址是 **80 或 443 端口**,且必须能被公网访问。所以需要:

1. 一台有公网 IP 的机器 + 一个域名
2. 反向代理 + TLS(Caddy 最省事,自动签证书)
3. **服务端仍然只监听 `127.0.0.1`**(`APP_HOST` 默认值),
   由反向代理转发 —— 不要图省事把它直接暴露出去

Caddy 的配置大致是:

```
你的域名 {
    reverse_proxy 127.0.0.1:8000
}
```

然后回企微后台:

1. **应用详情 → 接收消息 → 设置API接收**
2. URL 填 `https://你的域名/wecom/callback`
3. Token 和 EncodingAESKey 填 `.env` 里那两个值(或者让后台随机生成再抄回 `.env`)
4. **点保存** —— 企微会立刻发一次 GET 做 URL 验证,验证通过才存得下

保存成功就通了。在企微里给这个应用发一句"上周我答应了谁什么事"试试。

> **回调被拒时服务端只回一个裸 400,不说原因。** 告诉对方错在哪一步等于
> 帮他调试。真实原因在服务端日志里。

---

## 3. 卡住了看这里

| 现象 | 原因 |
| --- | --- |
| 发消息 `errcode=60020` | **企业可信 IP 没配**,或者出口 IP 变了([1.2 第 6 步](#12-企业微信)) |
| `errcode=40001` | Secret 抄错,或者抄成了别的应用的 |
| IMAP `Unsafe Login` | 163/126 的 `ID` 握手 —— 代码里处理了,还报就是 host 填错 |
| IMAP 认证失败 | 用了登录密码而不是授权码;或最近改过邮箱密码 |
| 启动就报配置错误 | **设计如此**,它会明确告诉你缺哪一项,照着填 |
| 日历没采到 | 正常,`WECOM_CALENDAR_ID` 没配。接口参数标了"待实测",见 [07 §2.4](07-config.md#24-企业微信整组可选) |
| 回调 URL 保存不上 | 服务没起、反向代理没通、或者 Token/AESKey 和 `.env` 对不上 |
| `test-push` 说走的是 wecom | 微信会话没配或已过期。`ret=-14` 或 `-2/unknown error` 都是"要重新扫码" |
| 摘要突然从微信变成企微收到了 | 微信会话过期了。**会有告警**,别当没看见 —— 这正是留兜底的意义 |
| 摘要收到了但内容很差 | **先回去修归一化,不要先改 prompt** —— 见下 |

---

## 4. 接下来 14 天

**什么都别写,就用。**

P0 的唯一目标是回答两个问题:你会不会每天看,以及摘要质量够不够
([03](03-roadmap.md#p0--自己每天会打开它))。验收标准刻意是主观的
——「连续 14 天,你觉得有用的比例 > 70%」。

每天记两个数:

| 记什么 | 说明什么 |
| --- | --- |
| **「这条有用」的比例** | 低于 70% 就别往 P1 走,先修这一期 |
| 推送落款里「丢弃 N 条无法溯源」的 **N** | 一直很大 = prompt 要改;偶尔一两条是正常的 |

**摘要质量差时先修归一化,不是先改 prompt。** 这是 03 写死的退出条件:
绝大多数情况下模型没抓住重点,是因为喂给它的东西本来就是乱的
——「摘要质量差到你自己都不看 → 问题多半在数据归一化而不是 prompt」。

另外两件只能你亲自验的事,验完把结论写回文档:

- **企微日程接口**的确切路径与分页参数(标记在 `lifein/sources/calendar_source.py`
  的常量上,对不上只改那三行,归一化不用动)
- **LLM 供应商是否将请求用于训练**,记进部署记录([R12](05-risks.md#r12--外部-llm-供应商侧的数据暴露))

---

## 5. P1:装上手机那半边

摘要跑顺了、14 天数完了,再做这一步。**顺序不能反** ——
采集器一放行,群消息就会进 `raw_events`,而摘要是按窗口读它的:
代码一个字节没动,摘要的口径却变了,那 14 天的样本就不好比了
([AGENTS §9](../AGENTS.md#9-当前状态))。

服务端**没有新迁移**:App 用到的四张表(`credentials`、`collector_heartbeat`、
`collector_whitelist`、`todos`)前面几片就建好了。拉代码、重启进程即可。

### 5.1 签发这台手机的凭据

```bash
python -m lifein.admin issue-device --user <uuid> --device-id pixel-7a \
    --base-url https://你的域名
```

它会打出一串配码,并在当前目录生成 `device-pixel-7a.html`(二维码)。

**那串东西只显示一次。** 库里存的是密文,服务端自己也读不出来给你看第二遍
—— 丢了就重新签发,旧的同时作废。

签出来的是**两条独立的行、两把独立的密钥**(铁律 12):采集那把只能写,
查询那把才读得到东西。手机丢了:

```bash
python -m lifein.admin revoke-device --user <uuid> --device-id pixel-7a
```

**下一次请求就失效**,不用等 token 过期([06 §6.3](06-data-model.md#63-查询端长期凭据换短期-token))。

### 5.2 编 App、装上去

编译步骤在 [`android/README.md`](../android/README.md)。产物是
`android/app/build/outputs/apk/debug/app-debug.apk`,`adb install` 或者直接传到手机装。

**`base_url` 必须是 https。** 写 `http://` 的话安卓 9 起直接拒明文流量,
表现是所有请求都失败 —— 那正是要的(架构 §8.3),没有开口子。

### 5.3 在手机上配好

1. 打开 App,把 5.1 那串配码粘进去(用系统相机扫二维码扫出文字再粘)
2. **系统设置 → 通知使用权 → 允许 LifeIn** —— 状态页有按钮直接跳过去
3. 状态页点"授予日历权限"
4. 厂商的自启动 / 电池优化白名单里加上它(各家位置不同,这一步没有 API 能替你做)
5. 想要小组件就长按桌面加上

### 5.4 放行来源(**默认拒绝,不放行什么都进不来**)

```bash
python -m lifein.admin allow-source --user <uuid> --package com.tencent.mm
python -m lifein.admin list-sources --user <uuid>     # 白名单 + 心跳一起看
```

P1 只放消息类。银行与支付类是 P2 的事,提前加了也会被 `purpose` 闸门挡住
([06 §6.4](06-data-model.md#64-采集上报-post-ingestevents))。

### 5.5 确认它真的在跑

按这个顺序看,每一步单独能判断:

| 看哪 | 说明什么 |
| --- | --- |
| App 状态页"通知监听:已开启" | 手机这侧的权限对了 |
| 状态页"待上报 N 条" | 采集器**真的收到了东西**(筛过之后)。一直是 0 = 那个 App 没发通知,或全被筛掉了 |
| 状态页"上次上报:收下 X 条" | 服务端收下了。X=0 而丢弃不为 0,多半是白名单没放行 |
| `list-sources` 里的最后心跳 | 服务端这侧看到的。超过一小时会发告警邮件 |

### 5.6 三条只能实测的事(P1 验收标准要的就是这几条)

**掉线告警**:别等它自然掉线。**去系统里关掉通知使用权** ——
下一次心跳会带 `listener_enabled=false`,服务端照样告警
([06 §6.5](06-data-model.md#65-心跳-post-ingestheartbeat))。这比等一小时快,
而且验的是更隐蔽的那一种:心跳正常但采不到东西。

**日程真的进日历**:等一条日程被提取出来 → App 的待确认里点确认 →
半小时内(或在 App 里点一下待办触发一次同步)看系统日历。
回来看 `todos.synced_at` 有没有值:**空的就是没落地**,那个状态在待办列表上
也看得见("还没写进系统日历")。

**采集凭据读不到账本** —— 03 要求"实测验证,不是设计上认为":

```bash
python scripts/verify-app-api.py            # 默认打 http://127.0.0.1:8000
LIFEIN_BASE=https://你的域名 python scripts/verify-app-api.py
```

那个脚本对着**真库、真 HTTP** 走一遍两组接口:心跳、上报、采集密钥换 token
(必须 401)、拿采集密钥伪造的 token(必须 401)、六个查询接口、以及吊销之后
同一个 token 立刻失效。**它不会往 `raw_events` 里写任何东西** ——
白名单空着时上报会被第一道挡下(06 §6.4),链路照样走完,
而测试数据一旦进了事件流,第二天就会出现在摘要里。

拿不到 401 就**立刻停下**:那意味着采集端能读账本,而手机丢了等于全部数据丢了
([R11](05-risks.md#r11--app-直连服务端的认证面))。

### 5.7 卡住了

| 现象 | 原因 |
| --- | --- |
| App 所有请求都 401 | 手机时钟偏差超过五分钟(签名带时间戳),或者凭据被吊销过。心跳返回里有服务端时间可以对 |
| 上报回来全是 `not_whitelisted` | 服务端白名单没放行那个包名 —— 跑 `allow-source` |
| 上报回来是 `phase_not_open` | 那条白名单的 `purpose` 是 `transaction`,P1 不放行 |
| 队列一直涨、上报不动 | 看状态页"最近一次失败"。401 不会自动重试(设计如此,重试到没电也一样) |
| 日程一直"未写入日历" | 日历权限没给;或者手机很久没联网。**这是看得见的延迟,不是丢失**(ADR-020) |
| 小组件不刷新 | 各家省电策略。它是"看一眼"的入口不是提醒机制 —— 真提醒走消息通道 |
| 心跳正常但什么都采不到 | 通知使用权被系统收走了。状态页那行会显示,服务端也会告警 |

---

## 6. 搬到云服务器(P1 收尾)

为什么搬、代价是什么,见
[ADR-022](04-tech-decisions.md#adr-022--服务端搬到云服务器用已备案域名的子域名)。
这一节只讲**怎么搬**。

> **搬家本身就是那次恢复演练。** [07 §6](07-config.md#6-部署前检查清单) 要求
> "备份配了不算,演练过才算" —— 把家里的库导出、在云上还原、验证跑得通,
> 这一整套走完,那条就打勾了。**所以别图快跳过验证那几步。**

### 6.0 顺序不能反

**先停家里那台,再起云上那台。** 两个进程会抢微信的长轮询会话,
表现是"消息一会儿到一会儿不到"(`scripts/run-lifein.md` 里那条坑)。
而且两边都在跑定时任务的话,同一个窗口会被认领两次。

### 6.1 云上准备

```bash
# Ubuntu 22.04 / 24.04
sudo apt update
sudo apt install -y python3.12-venv postgresql-16 postgresql-16-pgvector caddy

sudo -u postgres psql -c "CREATE USER lifein WITH PASSWORD '换成你的';"
sudo -u postgres psql -c "CREATE DATABASE lifein OWNER lifein;"
sudo -u postgres psql -d lifein -c "CREATE EXTENSION IF NOT EXISTS vector;"

sudo useradd -r -m -d /opt/lifein lifein
sudo mkdir -p /var/log/lifein && sudo chown lifein:lifein /var/log/lifein
```

**安全组只开 22 / 80 / 443。** 5432 与 8000 一律不对外 ——
数据库和应用都只监听 `127.0.0.1`,TLS 在 Caddy 终结
([07 §2.1](07-config.md#21-基础) 那条不变)。80 要开是因为
Let's Encrypt 的 HTTP-01 验证走它。

### 6.2 把代码和配置搬上去

```bash
git clone <你的仓库> /opt/lifein && cd /opt/lifein
python3 -m venv .venv && .venv/bin/pip install -e .
```

`.env` **手抄一份**,不要 scp 整个文件过去(那份里有旧机器的路径习惯)。
必须原样带过去的是这三行:

```
MASTER_KEY=...            # 一个字符都不能改
MASTER_KEY_VERSION=...    # 和上面成对
DATABASE_URL=...          # 改成云上那个库
```

> **这是整个迁移最容易翻车的一条。** `MASTER_KEY` 不跟着搬,
> `credentials` 里所有凭据**全部解不开** —— 加密信封的 AAD 绑了
> `user_id` 与 `kind`,密钥不对是 `DecryptError`,不是"读出乱码"。
> 症状是"迁完了但采不到邮件、推不出微信",而日志里只有一行解密失败。

顺便把这一期新增的两项补上(家里那台的 `.env` 里还没有):

```
NOTIFICATION_RETENTION_DAYS=7
SMTP_HOST=...  SMTP_PORT=465  SMTP_USERNAME=...  SMTP_PASSWORD=...  SMTP_TO=...
```

**SMTP 这一组现在是必须的**:告警的唯一出口是邮件(07 §2.7),
而"采集器掉线 1 小时内告警"是 P1 的验收标准 ——
不配的话那条标准只会写进日志,没人看得见。

`INGEST_SECRET` 那一行如果还在,删掉:密钥改成按设备签发了([07 §2.5](07-config.md#25-采集入口))。

### 6.3 迁数据

```bash
# 家里那台(先停服务!)
docker exec lifein-pg pg_dump -U lifein -d lifein --format=custom > lifein.dump
scp lifein.dump user@云服务器:/tmp/

# 云上
sudo -u postgres pg_restore -d lifein --no-owner --role=lifein /tmp/lifein.dump
cd /opt/lifein && .venv/bin/python -m alembic upgrade head   # 应当显示已是 head
```

还原完立刻对一遍条数,**对不上就停下**:

```bash
.venv/bin/python -c "
from sqlalchemy import text
from lifein.db import session_scope
with session_scope() as s:
    for t in ('users','raw_events','facts','entities','todos','credentials','push_log'):
        print(t, s.execute(text(f'SELECT count(*) FROM {t}')).scalar_one())"
```

**再验一件事:凭据解得开。** 这是主密钥有没有搬对的唯一判据:

```bash
.venv/bin/python -m lifein.admin key-status --user <uuid>   # 不报错就是解得开
.venv/bin/python -m lifein.admin test-imap --user <uuid>    # 真连一次邮箱
```

### 6.4 常驻与反代

```bash
sudo cp deploy/lifein.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lifein
sudo systemctl status lifein

sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # 改成你的子域名
sudo systemctl reload caddy
```

子域名在 DNS 里加一条 A 记录指向服务器公网 IP。**备案是按域名的**,
主域名备过,子域名跟着走,不用再备一次。

### 6.5 验证(这一步不能省)

```bash
curl https://lifein.你的域名.com/healthz                    # {"status":"ok"}
LIFEIN_BASE=https://lifein.你的域名.com \
    .venv/bin/python scripts/verify-app-api.py             # 必须全部通过
sudo journalctl -u lifein -n 50                            # 微信长轮询有没有恢复
```

`verify-app-api.py` 里那条 **R11 实测**(采集密钥换不出 token)在新地址上
必须照样是 401 —— 反代改写了路径的话,签名会对不上,表现就是全部 401,
所以这个脚本同时也在验"反代没有动路径"(06 §6.2)。

摘要那条链路等第二天早上八点自己验:收到了就是通的,没收到就看日志。
急的话 `python -m lifein --once` 立刻跑一遍。

### 6.6 家里那台怎么处理

- **关掉自启**(任务计划里那条),否则下次开机它会跟云上抢微信会话
- 代码留着当开发机。`.env` 里的 `DATABASE_URL` 指回本地测试库,
  别指云上那个 —— 开发时一条 `alembic downgrade` 就能把线上库打回去
- 那份 `lifein.dump` 留着。**它是这次搬家的回退路径**,
  在云上跑满一周之前不要删

### 6.7 备份从此是云上的事

家里那台不再有数据,备份要在云上重新配:

```bash
# /etc/cron.daily/lifein-backup
sudo -u postgres pg_dump -d lifein --format=custom \
    > /var/backups/lifein-$(date +\%F).dump
find /var/backups -name 'lifein-*.dump' -mtime +14 -delete
```

**备份和主密钥不要放同一个地方。** `.env` 在服务器上,备份也在服务器上的话,
一次拖库就两样都拿走了 —— 加密等于没做([R1](05-risks.md#r1--代管他人凭据与支付数据)
那张新增暴露面的表)。
