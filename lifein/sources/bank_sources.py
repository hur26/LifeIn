"""P2 要放行的那些来源(第 12 片)。

这是一份**建议清单,不是自动生效的白名单**。默认拒绝那条规矩不变
([R10](../../docs/05-risks.md#r10--手机端采集器的越权读取)):
这里的每一条都要用 `admin allow-source` 显式加进 `collector_whitelist`,
才会真的放行。

放在代码里而不是让人自己查号段,是因为**记错一个号段的后果不对称**:
多放一个进来最多是几条营销短信(会被第 3 层判成 marketing 丢掉);
少放一个则是那家银行的消费一整个月都不入账,而账本上看不出少了什么。

## 银行短信按**短信签名**匹配,不按号码

原来这里是 14 条号段(95555、95588……),按发件号码前缀匹配。
**2026-09-11 的三条真实短信证明那条路根本走不通**(ADR-034):

- 通知标题有时是显示名(`招商银行`)有时是网关号码(`10693495555`)
- 真实号码走 1069 的 SP 网关,**`95555` 在里面是子串不是前缀**

所以就算每次都拿得到号码,前缀这个假设本身也是错的。现在按正文开头
`【机构名】` 那个签名匹配 —— 它由发信方写进内容,两件事都不影响它。

**下面这些签名里,只有「招商银行」是对着真实短信验过的。** 其余是按惯例
写的,收到那家银行的第一条真实短信时要回来核一遍 ——
这份清单的价值全在于它对得上真实世界,而不在于它看起来完整。

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
    MATCH_SMS_SIGNATURE,
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
    Suggested(MATCH_SMS_SIGNATURE, "招商银行", "招商银行"),  # 已对真实短信验过
    Suggested(MATCH_SMS_SIGNATURE, "建设银行", "建设银行"),
    Suggested(MATCH_SMS_SIGNATURE, "工商银行", "工商银行"),
    Suggested(MATCH_SMS_SIGNATURE, "农业银行", "农业银行"),
    Suggested(MATCH_SMS_SIGNATURE, "中国银行", "中国银行"),
    Suggested(MATCH_SMS_SIGNATURE, "兴业银行", "兴业银行"),
    Suggested(MATCH_SMS_SIGNATURE, "中信银行", "中信银行"),
    Suggested(MATCH_SMS_SIGNATURE, "民生银行", "民生银行"),
    Suggested(MATCH_SMS_SIGNATURE, "浦发银行", "浦发银行"),
    Suggested(MATCH_SMS_SIGNATURE, "广发银行", "广发银行"),
    Suggested(MATCH_SMS_SIGNATURE, "光大银行", "光大银行"),
    Suggested(MATCH_SMS_SIGNATURE, "华夏银行", "华夏银行"),
    Suggested(MATCH_SMS_SIGNATURE, "邮储银行", "邮储银行"),
    Suggested(MATCH_SMS_SIGNATURE, "中国银联", "中国银联"),
)
"""银行短信签名。**前缀匹配** —— 同一家机构有多个签名
(`招商银行` / `招商银行信用卡`),而这里给的是完整机构名。"""

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
