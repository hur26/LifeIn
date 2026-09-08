"""P2 要放行的那些来源(第 12 片)。

这是一份**建议清单,不是自动生效的白名单**。默认拒绝那条规矩不变
([R10](../../docs/05-risks.md#r10--手机端采集器的越权读取)):
这里的每一条都要用 `admin allow-source` 显式加进 `collector_whitelist`,
才会真的放行。

放在代码里而不是让人自己查号段,是因为**记错一个号段的后果不对称**:
多放一个进来最多是几条营销短信(会被第 3 层判成 marketing 丢掉);
少放一个则是那家银行的消费一整个月都不入账,而账本上看不出少了什么。

## 银行短信是号段前缀匹配,不是全等

95555、95588 这些是主号,但银行实际发短信用的是它们的扩展号
(955550、9555501……)。全等匹配会漏掉绝大多数,而这种漏是**静默的**。

## 这里没有微信和支付宝的"消息"

`com.tencent.mm` 在 P1 就以 `purpose=message` 放行了。这里给它们的是
**支付类通知**那一路,两条是不同的 purpose,走不同的解析器,
所以同一个包名在表里可以有两条 —— 而 `UNIQUE (user_id, match_type, pattern)`
挡住了那件事。

**这是一个已知的限制**:微信支付的通知和微信聊天的通知来自同一个包名,
系统层面分不开。目前的取舍是让它留在 `purpose=message`,
交易靠银行短信那一路 —— 微信零钱支付因此会漏,而那正是每月导出账单
要补的那部分(ADR-012 的两阶段入账)。
"""

from __future__ import annotations

from dataclasses import dataclass

from lifein.repos.collector import (
    MATCH_PACKAGE,
    MATCH_SMS_SENDER,
    PURPOSE_TRANSACTION,
)

PHASE = "P2"


@dataclass(frozen=True)
class Suggested:
    match_type: str
    pattern: str
    label: str
    purpose: str = PURPOSE_TRANSACTION
    phase: str = PHASE


BANK_SMS: tuple[Suggested, ...] = (
    Suggested(MATCH_SMS_SENDER, "95555", "招商银行"),
    Suggested(MATCH_SMS_SENDER, "95533", "建设银行"),
    Suggested(MATCH_SMS_SENDER, "95588", "工商银行"),
    Suggested(MATCH_SMS_SENDER, "95599", "农业银行"),
    Suggested(MATCH_SMS_SENDER, "95566", "中国银行"),
    Suggested(MATCH_SMS_SENDER, "95561", "兴业银行"),
    Suggested(MATCH_SMS_SENDER, "95558", "中信银行"),
    Suggested(MATCH_SMS_SENDER, "95568", "民生银行"),
    Suggested(MATCH_SMS_SENDER, "95528", "浦发银行"),
    Suggested(MATCH_SMS_SENDER, "95508", "广发银行"),
    Suggested(MATCH_SMS_SENDER, "95595", "光大银行"),
    Suggested(MATCH_SMS_SENDER, "95577", "华夏银行"),
    Suggested(MATCH_SMS_SENDER, "95580", "邮储银行"),
    Suggested(MATCH_SMS_SENDER, "95516", "银联"),
)
"""银行短信号段。**前缀匹配** —— 见模块开头那条。"""

PAYMENT_APPS: tuple[Suggested, ...] = (
    Suggested(MATCH_PACKAGE, "com.eg.android.AlipayGphone", "支付宝"),
    Suggested(MATCH_PACKAGE, "com.unionpay", "云闪付"),
    Suggested(MATCH_PACKAGE, "com.chinamworld.main", "建设银行 App"),
    Suggested(MATCH_PACKAGE, "cmb.pb", "招商银行 App"),
    Suggested(MATCH_PACKAGE, "com.icbc", "工商银行 App"),
    Suggested(MATCH_PACKAGE, "com.jingdong.app.mall", "京东"),
    Suggested(MATCH_PACKAGE, "com.sankuai.meituan", "美团"),
)
"""支付与银行 App 的包名。**全等匹配** —— 包名是精确的,前缀会误伤
(`com.icbc` 前缀会连上 `com.icbcxxx`,而那可能是别人的应用)。"""

ALL: tuple[Suggested, ...] = BANK_SMS + PAYMENT_APPS


def by_label(keyword: str) -> list[Suggested]:
    """按名字找。给 `admin allow-source --preset 招商` 用。"""
    return [item for item in ALL if keyword in item.label or keyword in item.pattern]
