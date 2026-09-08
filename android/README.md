# 安卓 App(P1 第 11 片)

采集 + 查看 + 执行,三件事([架构 §8](../docs/02-architecture.md#8-安卓-appp1-起))。
**不接收任何推送**(铁律 10 / ADR-014);桌面小组件是定时去拉,不在此列(ADR-020)。

技术选型与被否决的方案见
[ADR-021](../docs/04-tech-decisions.md#adr-021--安卓端依赖清单逐条定性)。
接口契约见 [06 §6](../docs/06-data-model.md#6-接口契约) —— **改接口先改那一节**。

## 怎么编

这份代码在 Windows 上没有编过:仓库所在的机器没有 Android SDK。
第一次编要么用 Android Studio 打开 `android/` 目录(它会自己补 Gradle wrapper),
要么装了 Gradle 之后:

```bash
cd android
gradle wrapper          # 只需要一次,补出 gradlew 和 wrapper jar
./gradlew assembleDebug
```

`local.properties` 里要有 `sdk.dir`,Android Studio 会自动写。

## 装好之后的三步

1. 服务端签发配码:`python -m lifein.admin issue-device --user <uuid> --device-id <这台手机>`
2. 打开 App,把那一串粘进配置页(**只显示一次**)
3. 系统设置 → 通知使用权 → 允许 LifeIn 读取通知;
   再去电池优化里把它加进白名单(各家 ROM 位置不同)

第 3 步做完之前,采集器一条也送不上去。做完之后服务端还要放行来源:
`python -m lifein.admin allow-source --user <uuid> --package com.tencent.mm`
—— 白名单默认拒绝([R10](../docs/05-risks.md#r10--手机端采集器的越权读取))。

4. 状态页里点"授予日历权限",否则日程会一直停在"未写入日历"
5. 长按桌面加小组件(可选)。它是**定时去拉**,受各家省电策略限制 ——
   是"看一眼"的入口,不是提醒机制(ADR-020)

## 装好之后怎么确认它真的在跑

按这个顺序看,每一步都能单独判断:

| 看哪 | 说明什么 |
| --- | --- |
| App 状态页"通知监听:已开启" | 手机这一侧的权限对了 |
| 状态页"待上报 N 条" | 采集器筛过之后**真的收到了东西**。一直是 0 说明白名单里那个 App 没发通知,或者被筛掉了 |
| 状态页"上次上报:收下 X 条" | 服务端收下了。X 是 0 而丢弃不是 0,多半是服务端白名单没放行 |
| `python -m lifein.admin list-sources --user <uuid>` | 服务端看到的心跳与白名单 |
| 服务端日志里的告警 | 超过一小时没心跳会发一封邮件(P1 验收标准) |

## 已知的边界

- **通知监听只拿得到通知栏展示过的内容**:免打扰的群拿不到、长消息会截断、
  撤回和历史消息完全拿不到(ADR-010)
- **保活靠系统**:通知监听服务被 ROM 杀掉后系统会重绑,但自启动白名单要手动加。
  真掉线了靠服务端心跳告警发现,这是有意的选择(ADR-021)
- **小组件最快十五分钟一刷**,那是 WorkManager 的下限
