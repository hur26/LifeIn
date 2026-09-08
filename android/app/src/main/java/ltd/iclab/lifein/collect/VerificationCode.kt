package ltd.iclab.lifein.collect

/**
 * 验证码识别 —— [铁律 11](../../../../../../../AGENTS.md) 的**手机端那一道**。
 *
 * 服务端入库前还会再过一次(`lifein/sources/verification_code.py`)。
 * 两道故意是两份代码、两份正则:
 * 共用一份配置的那一刻,"双重丢弃"就退化成一道,而手机端这道的全部价值
 * 正在于它和服务端那道**独立失效**(07 §4)。
 *
 * 代价是改一次要动两处并重新发版。这个代价是有意付的。
 *
 * 命中的通知**整条不上报**:不进队列、不落本地库、日志里也不写原文。
 * [R10](../../../../../../../docs/05-risks.md) 说得很直白 ——
 * 账单泄露是隐私损失,验证码泄露是资金损失。
 *
 * 正则和服务端那份逐字一致。改的时候只能放宽,不能收窄。
 */
object VerificationCode {

    private val PATTERN = Regex(
        "验证码|校验码|动态密码|verification code|" +
            "\\b\\d{4,8}\\b\\s*(?:为|是)?\\s*(?:您的)?(?:验证码|校验码)",
        RegexOption.IGNORE_CASE,
    )

    /**
     * 标题、正文里任何一处命中就算命中。
     *
     * 分开看而不是拼起来看:拼接会造出原本不存在的相邻关系,
     * "…动态" + "密码保护…" 拼起来才像命中的那种,不该算。
     */
    fun matches(vararg parts: String?): Boolean =
        parts.any { !it.isNullOrEmpty() && PATTERN.containsMatchIn(it) }
}
