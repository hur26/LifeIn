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
