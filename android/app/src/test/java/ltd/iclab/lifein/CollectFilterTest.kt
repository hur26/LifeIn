package ltd.iclab.lifein

import ltd.iclab.lifein.collect.VerificationCode
import ltd.iclab.lifein.collect.Whitelist
import ltd.iclab.lifein.collect.WhitelistRule
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 手机端那两道过滤。
 *
 * 服务端有一份形状几乎一样的用例(`tests/test_notification_source.py`)。
 * **两份用例不是重复**:这两道是[铁律 11](../../../../../../../AGENTS.md) 要求的
 * 双重丢弃,它们的价值全在于**独立失效** —— 一边改坏了另一边还在,
 * 而"两边同时改坏"要先让两个测试文件同时变绿。
 */
class CollectFilterTest {

    private fun wechat(purpose: String = "message", enabled: Boolean = true) =
        WhitelistRule(
            matchType = WhitelistRule.MATCH_PACKAGE,
            pattern = "com.tencent.mm",
            purpose = purpose,
            enabled = enabled,
        )

    @Test
    fun `default is deny`() {
        val list = Whitelist(listOf(wechat()))
        assertTrue(list.allows("com.tencent.mm", null))
        assertFalse(list.allows("com.some.other.app", null))
        assertFalse(list.allows(null, null))
    }

    @Test
    fun `package match is exact`() {
        // 前缀匹配会让 com.tencent.mm 顺带放行 com.tencent.mm.fake
        assertFalse(Whitelist(listOf(wechat())).allows("com.tencent.mm.fake", null))
    }

    @Test
    fun `sms sender matches by prefix because bank numbers are ranges`() {
        val rule = WhitelistRule(
            matchType = WhitelistRule.MATCH_SMS_SENDER,
            pattern = "10690",
            purpose = "message",
        )
        assertTrue(Whitelist(listOf(rule)).allows(null, "1069001234567"))
        assertFalse(Whitelist(listOf(rule)).allows(null, "13800138000"))
    }

    @Test
    fun `transaction purpose is open now that P2 is on`() {
        // **这条用例以前断言的是反面。** 手机端的 purpose 闸门一直硬编码成
        // 只放 message,而 P2 打开记账闸门时没有回来改它(ADR-032)——
        // 后果是 purpose=transaction 的规则在手机上就被丢掉,而服务端、
        // list-sources、状态页都显示它放行着。看起来配好了,一条都收不到。
        assertTrue(Whitelist(listOf(wechat(purpose = "transaction"))).allows("com.tencent.mm", null))
    }

    @Test
    fun `an unknown purpose is still refused`() {
        // 闸门放开的是"两种已知的 purpose",不是"什么都放"。
        // 服务端将来加第三种时,旧版本 App 不该自作主张先放行
        assertFalse(Whitelist(listOf(wechat(purpose = "whatever"))).allows("com.tencent.mm", null))
    }

    @Test
    fun `a bank sms preset matches the way it is actually shipped`() {
        // 号段预设在服务端是 sms_sender + transaction(bank_sources.py)。
        // 两处闸门里任何一处关着,这条都过不去 —— 这就是为什么 ADR-032
        // 说那两条必须一起改
        val zhaoshang = WhitelistRule(
            matchType = WhitelistRule.MATCH_SMS_SENDER,
            pattern = "95555",
            purpose = "transaction",
        )
        val list = Whitelist(listOf(zhaoshang))
        // 银行用的是扩展号,所以是前缀匹配
        assertTrue(list.allows("com.android.mms", "955550"))
        assertFalse(list.allows("com.android.mms", "10086"))
        // 发件人取不到时(不是默认短信应用的通知)照样不放行
        assertFalse(list.allows("com.android.mms", null))
    }

    @Test
    fun `a disabled rule stops letting things through`() {
        assertFalse(Whitelist(listOf(wechat(enabled = false))).allows("com.tencent.mm", null))
    }

    @Test
    fun `built-in list is wechat only, never allow-all`() {
        val builtIn = Whitelist(Whitelist.BUILT_IN)
        assertTrue(builtIn.allows("com.tencent.mm", null))
        assertFalse(builtIn.allows("com.eg.android.AlipayGphone", null))
    }

    @Test
    fun `verification codes are recognised`() {
        assertTrue(VerificationCode.matches("您的验证码是 123456"))
        assertTrue(VerificationCode.matches("校验码 8823,请勿告诉他人"))
        assertTrue(VerificationCode.matches("动态密码:4432"))
        assertTrue(VerificationCode.matches("Your verification code is 993022"))
        // 标题命中也算 —— 正文里可能什么都看不出来
        assertTrue(VerificationCode.matches("【某银行】动态密码", "点击查看"))
    }

    @Test
    fun `ordinary messages pass`() {
        assertFalse(VerificationCode.matches("明天下午三点开会"))
        assertFalse(VerificationCode.matches("你的快递已签收"))
        // 拼起来才像命中的那种不该算
        assertFalse(VerificationCode.matches("今天很动态", "密码保护已开启"))
        assertFalse(VerificationCode.matches(null, ""))
    }
}
