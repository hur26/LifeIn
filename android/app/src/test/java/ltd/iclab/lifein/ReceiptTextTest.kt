package ltd.iclab.lifein

import ltd.iclab.lifein.collect.ReceiptText
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.math.BigDecimal

/**
 * 小票文字 → 金额与商户(P2 第 14 片)。
 *
 * **这一组几乎全在测"挑错了没有"。** 一张小票上有小计、优惠、实付、找零、
 * 桌号、订单号、时间里的数字 —— 挑错一个就是一笔错账,而错账和对账在列表里
 * 长得一模一样,没有人会去核对。
 *
 * 剩下的用例测"认不出来的时候有没有老实返回 null":返回 null 的结果是让人
 * 手填(多点几下),而猜一个的结果是一笔看起来完全正常的错账。
 * [03 那条"误记率 = 0"](../../../../../../../docs/03-roadmap.md)
 * 不区分错误来自模型还是来自 OCR。
 */
class ReceiptTextTest {

    private fun amountOf(text: String) = ReceiptText.parse(text).amount

    @Test
    fun `实付优先于合计`() {
        // 有优惠券时两者不同,而你花掉的是实付那个
        val text = """
            星巴克咖啡
            合计 45.00
            优惠 6.50
            实付 38.50
        """.trimIndent()

        assertEquals(BigDecimal("38.50"), amountOf(text))
    }

    @Test
    fun `没有实付时用合计`() {
        assertEquals(BigDecimal("38.50"), amountOf("全家便利店\n合计 38.50"))
    }

    @Test
    fun `现金小票取应收而不是实收`() {
        // **写这条时才发现原来想错了。** "实收 100.00" 是收到的现金,
        // 找零 50 之后你花的是 50 —— 拿实收当消费额会把这张小票记成一百块,
        // 而那笔账在列表里看起来完全正常。所以"实收"根本不在关键词表里。
        val text = "小馆子\n应收 50.00\n实收 100.00\n找零 50.00"
        assertEquals(BigDecimal("50.00"), amountOf(text))
    }

    @Test
    fun `优惠那一行不是金额`() {
        assertNull(amountOf("某某店\n优惠 6.50\n积分 120"))
    }

    @Test
    fun `桌号和订单号不是金额`() {
        assertNull(amountOf("面馆\n桌号 12\n订单号 20260908001"))
    }

    @Test
    fun `没有关键词就不猜`() {
        // **"最大的那个数"这种规则会在有余额的小票上稳定地挑错**,
        // 而挑错的结果是一笔看起来完全正常的错账
        assertNull(amountOf("某某超市\n可乐 3.50\n面包 12.00\n牛奶 8.90"))
    }

    @Test
    fun `一行里有件数时取带小数的那个`() {
        // 小票排版是"说明在左,金额在右",而件数不带小数
        assertEquals(BigDecimal("38.50"), amountOf("合计 2 件 38.50"))
    }

    @Test
    fun `货币符号和千分位都认`() {
        assertEquals(BigDecimal("1234.56"), amountOf("实付 ¥1,234.56"))
        assertEquals(BigDecimal("38.50"), amountOf("实付 CNY 38.50"))
    }

    @Test
    fun `优惠后实付这种排版仍然认得出`() {
        // 同一行里既有"实付"又有"优惠" —— 按关键词优先级已经选对了行,
        // 不能再因为出现"优惠"就把它跳过
        assertEquals(BigDecimal("38.50"), amountOf("优惠后实付 38.50"))
    }

    @Test
    fun `商户名在最前面几行且不含数字`() {
        val parsed = ReceiptText.parse("星巴克咖啡\n2026-09-08 12:30\n实付 38.50")
        assertEquals("星巴克咖啡", parsed.merchant)
    }

    @Test
    fun `抠不出商户就空着`() {
        // 硬编一个会污染商户规则表 —— 那张表一条错规则会影响往后每一笔
        val parsed = ReceiptText.parse("2026-09-08 12:30\n实付 38.50")
        assertNull(parsed.merchant)
    }

    @Test
    fun `什么都认不出时结果是没用的`() {
        // 界面上要如实说"没认出来,你自己填",而不是弹一个空表单让人猜哪里出了错
        val parsed = ReceiptText.parse("")
        assertTrue(!parsed.useful)
    }

    @Test
    fun `认出金额就算有用`() {
        assertTrue(ReceiptText.parse("实付 38.50").useful)
    }
}
