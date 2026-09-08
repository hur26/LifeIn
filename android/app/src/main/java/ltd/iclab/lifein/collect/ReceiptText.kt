package ltd.iclab.lifein.collect

import java.math.BigDecimal

/**
 * 从小票的文字里挑出金额和商户(P2 第 14 片)。
 *
 * **这是纯函数,不碰相机也不碰 ML Kit。** 识别出来的那一大段文字进来,
 * 结构化的结果出去 —— 因为**错都在这一步**,而这一步这样才测得了。
 * 拍照、调 ML Kit 那部分在 [ReceiptScanner] 里,那部分没什么可测的。
 *
 * ## 小票上的数字不止一个
 *
 * 一张小票上通常有:小计、优惠、实付、找零、桌号、订单号、时间里的数字。
 * **挑错一个就是一笔错账**,而错账和对账在列表里长得一模一样。
 * 所以这里的规则是:
 *
 * 1. **按关键词挑,不按位置或大小挑。** "实付""应收""合计"这些词决定哪个数
 *    是要的那个;没有关键词就返回 null,不猜"最大的那个"
 * 2. **实付优先于合计。** 有优惠券时两者不同,而你花掉的是实付那个
 * 3. **"实收"不算金额。** 现金小票上它是收到的现金,找零之后才是你花的钱
 * 4. **认不出就返回 null。** 03 那条"误记率 = 0"不区分错误来自模型还是 OCR,
 *    而返回 null 的结果是让人手填 —— 多点几下,不会记错
 *
 * 无论认出与否,结果都进待确认队列,不直接入账(ADR-021 的 2026-09 补充)。
 */
object ReceiptText {

    /**
     * 强关键词:它们直接命名"这笔交易多少钱",**任何修饰都不改变这一点**。
     * 所以带这些词的行不受下面那张排除表约束 —— "优惠后实付 38.50"
     * 里的 38.50 就是你花的钱,尽管这一行上有"优惠"两个字。
     *
     * **"实收"不在里面。** 现金小票上它是"收到的现金",找零之后才是你花的钱 ——
     * 拿它当消费额会把一张一百块付五十块的小票记成一百块,而那笔账看起来完全正常。
     */
    private val STRONG_MARKERS = listOf("实付", "应付", "应收", "付款金额", "消费金额")

    /**
     * 弱关键词:**它们能被修饰**,而修饰之后说的就不是这笔交易了 ——
     * "优惠合计 6.50" 里的 6.50 是优惠了多少,不是花了多少。
     * 所以带这些词的行还要过一遍排除表。
     */
    private val WEAK_MARKERS = listOf("合计", "总计", "小计")

    private val AMOUNT_MARKERS = STRONG_MARKERS + WEAK_MARKERS

    /** 这些行上的数字**一定不是**要找的金额。 */
    private val NEVER_AMOUNT = listOf(
        "找零", "抹零", "优惠", "折扣", "减免", "积分", "余额", "桌号", "台号",
        "订单号", "流水号", "单号", "电话", "税号", "会员",
    )

    private val AMOUNT = Regex("""(?:¥|￥|RMB|CNY)?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2})""")

    /** 商户名多半在最前面几行,而且不含数字。 */
    private const val MERCHANT_SCAN_LINES = 4

    data class Parsed(val amount: BigDecimal?, val merchant: String?) {
        /** 金额都没认出来的话,这次识别没帮上任何忙 —— 界面上要如实说。 */
        val useful: Boolean get() = amount != null
    }

    fun parse(text: String): Parsed {
        val lines = text.lines().map { it.trim() }.filter { it.isNotEmpty() }
        return Parsed(amount = findAmount(lines), merchant = findMerchant(lines))
    }

    private fun findAmount(lines: List<String>): BigDecimal? {
        for (marker in AMOUNT_MARKERS) {
            for (line in lines) {
                if (!line.contains(marker)) continue
                // 强关键词不过排除表:"优惠后实付 38.50" 这种排版是存在的,
                // 而那一行上的"优惠"两个字改不了 38.50 就是你花的钱
                if (marker in WEAK_MARKERS && isNeverAmount(line)) continue
                amountIn(line)?.let { return it }
            }
        }
        // 没有任何关键词就不猜。**"最大的那个数"这种规则会在有余额的小票上
        // 稳定地挑错**,而挑错的结果是一笔看起来完全正常的错账
        return null
    }

    private fun isNeverAmount(line: String): Boolean = NEVER_AMOUNT.any { line.contains(it) }

    private fun amountIn(line: String): BigDecimal? {
        // 一行里可能有好几个数("实付 38.50 找零 1.50"已经被上面挡了,
        // 但"合计 2 件 38.50"没有)—— 取**最后一个带小数的**:
        // 小票的排版是"说明在左,金额在右",而件数不带小数
        val candidates = AMOUNT.findAll(line)
            .mapNotNull { runCatching { BigDecimal(it.groupValues[1].replace(",", "")) }.getOrNull() }
            .filter { it.signum() > 0 }
            .toList()
        return candidates.lastOrNull { it.scale() > 0 } ?: candidates.lastOrNull()
    }

    private fun findMerchant(lines: List<String>): String? =
        lines.take(MERCHANT_SCAN_LINES).firstOrNull { line ->
            line.length in 2..20 &&
                !line.any { it.isDigit() } &&
                NEVER_AMOUNT.none { line.contains(it) } &&
                AMOUNT_MARKERS.none { line.contains(it) }
        }
}
