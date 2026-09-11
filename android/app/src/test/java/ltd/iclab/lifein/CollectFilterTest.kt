package ltd.iclab.lifein

import ltd.iclab.lifein.collect.SmsSignature
import ltd.iclab.lifein.collect.VerificationCode
import ltd.iclab.lifein.collect.Whitelist
import ltd.iclab.lifein.collect.WhitelistRule
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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
        assertTrue(list.allows("com.tencent.mm", null, null))
        assertFalse(list.allows("com.some.other.app", null, null))
        assertFalse(list.allows(null, null, null))
    }

    @Test
    fun `package match is exact`() {
        // 前缀匹配会让 com.tencent.mm 顺带放行 com.tencent.mm.fake
        assertFalse(Whitelist(listOf(wechat())).allows("com.tencent.mm.fake", null, null))
    }

    @Test
    fun `sms sender matches by prefix because bank numbers are ranges`() {
        val rule = WhitelistRule(
            matchType = WhitelistRule.MATCH_SMS_SENDER,
            pattern = "10690",
            purpose = "message",
        )
        assertTrue(Whitelist(listOf(rule)).allows(null, "1069001234567", null))
        assertFalse(Whitelist(listOf(rule)).allows(null, "13800138000", null))
    }

    @Test
    fun `transaction purpose is open now that P2 is on`() {
        // **这条用例以前断言的是反面。** 手机端的 purpose 闸门一直硬编码成
        // 只放 message,而 P2 打开记账闸门时没有回来改它(ADR-032)——
        // 后果是 purpose=transaction 的规则在手机上就被丢掉,而服务端、
        // list-sources、状态页都显示它放行着。看起来配好了,一条都收不到。
        assertTrue(Whitelist(listOf(wechat(purpose = "transaction"))).allows("com.tencent.mm", null, null))
    }

    @Test
    fun `an unknown purpose is still refused`() {
        // 闸门放开的是"两种已知的 purpose",不是"什么都放"。
        // 服务端将来加第三种时,旧版本 App 不该自作主张先放行
        assertFalse(Whitelist(listOf(wechat(purpose = "whatever"))).allows("com.tencent.mm", null, null))
    }

    @Test
    fun `a bank preset matches by signature, whatever the title showed`() {
        // 这条用例以前是按号段(95555)写的,而 2026-09-11 的三条真实短信把那个
        // 设计整个证伪了(ADR-034):通知标题有时是显示名有时是网关号码,
        // 而真实号码 10693495555 里 95555 是子串不是前缀
        val cmb = WhitelistRule(
            matchType = WhitelistRule.MATCH_SMS_SIGNATURE,
            pattern = "招商银行",
            purpose = "transaction",
        )
        val list = Whitelist(listOf(cmb))

        // 标题是显示名的那条,和标题是网关号码的那条,签名一样
        assertTrue(list.allows("com.android.mms", "招商银行", "招商银行"))
        assertTrue(list.allows("com.android.mms", "10693495555", "招商银行"))
        // 同一家机构的变体签名靠前缀覆盖
        assertTrue(list.allows("com.android.mms", "10693495555", "招商银行信用卡"))
        // 别人家的不放行
        assertFalse(list.allows("com.android.mms", "10690000", "建设银行"))
        // 没有签名的短信走不了这条规则 —— 宁可不放行,也不猜
        assertFalse(list.allows("com.android.mms", "10693495555", null))
    }

    @Test
    fun `the signature is pulled from the start of the body`() {
        assertEquals("招商银行", SmsSignature.of("【招商银行】您的储蓄卡…"))
        assertEquals("游戏中心", SmsSignature.of("【游戏中心】…"))
        // ASCII 方括号也认 —— 有些网关会转
        assertEquals("招商银行", SmsSignature.of("[招商银行]您的储蓄卡…"))
        // 只认开头:结尾签名现在不认(ADR-034 的重评触发条件里写着)
        assertNull(SmsSignature.of("您的验证码是 1234【招商银行】"))
        assertNull(SmsSignature.of("没有签名的一条短信"))
        assertNull(SmsSignature.of(null))
    }

    @Test
    fun `a disabled rule stops letting things through`() {
        assertFalse(Whitelist(listOf(wechat(enabled = false))).allows("com.tencent.mm", null, null))
    }

    @Test
    fun `built-in list is wechat only, never allow-all`() {
        val builtIn = Whitelist(Whitelist.BUILT_IN)
        assertTrue(builtIn.allows("com.tencent.mm", null, null))
        assertFalse(builtIn.allows("com.eg.android.AlipayGphone", null, null))
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
