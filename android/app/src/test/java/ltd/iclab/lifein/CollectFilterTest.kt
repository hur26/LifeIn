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
    fun `transaction purpose is not open on the device either`() {
        // 记账链路 P2 才打开。手机端挡住的那些根本不会离开这台设备
        assertFalse(Whitelist(listOf(wechat(purpose = "transaction"))).allows("com.tencent.mm", null))
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
