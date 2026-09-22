package ltd.iclab.lifein.copilot

import ltd.iclab.lifein.collect.VerificationCode

/**
 * 把"从节点树上扒下来的一堆矩形和文字"整理成消息列表 —— 副驾读屏这一路的
 * **全部判断都在这里**,而且是纯函数。
 *
 * 和 `ReceiptText` 同一个理由:碰 `AccessibilityNodeInfo` 的那部分没什么可测的
 * (遍历一棵树),**错都在整理这一步** —— 谁说的判反了、把群公告当成了标题、
 * 把时间戳当成了一条消息。那些错在真机上表现得都很隐晦:
 * 候选答得驴唇不对马嘴,但看不出是哪一步坏了。
 *
 * 所以适配器只负责"走树、收集矩形",整理交给这里,用例是普通 JUnit 跑的。
 */
object ChatShaping {

    /**
     * 一条最多带多少条消息上路。**这不是进 prompt 的窗口** ——
     * 那个在服务端(`COPILOT_MAX_MESSAGES`,默认 40),从最旧的一头切。
     *
     * 这个数只管"别让请求体无限大",和服务端 `api/copilot.MAX_INBOUND` 是同一个数,
     * 超了服务端会 422。屏幕上通常只有十几条,真正会撑到这里的是历史拼进来之后。
     */
    const val MAX_BUBBLES = 200

    /** 一个气泡:屏幕坐标 + 它的文字。 */
    data class RawBubble(val top: Int, val left: Int, val right: Int, val text: String) {
        val centerX: Int get() = (left + right) / 2
    }

    /** 一个可能是标题的文字节点。 */
    data class TitleCandidate(val text: String, val top: Int, val bottom: Int, val centerX: Int)

    /** 遍历节点树的护栏。树坏掉(自引用)时不至于把主线程转死。 */
    const val NODE_GUARD = 5000

    /** 标题不会比这更长。超了的多半是一条消息或者一段公告。 */
    const val TITLE_MAX_CHARS = 24

    /** 动作栏最多占屏幕高度的这么多 —— 标题只可能在这一条里。 */
    private const val ACTION_BAR_RATIO = 0.14

    /**
     * 动作栏的下边界。适配器走树时用它先粗筛一遍标题候选 ——
     * 那一步筛出来的是 [inActionBar] 所需的超集,这里精筛还会再收一次。
     */
    fun actionBarBottom(screenHeight: Int): Int = (screenHeight * ACTION_BAR_RATIO).toInt()

    /** "14:05""9月3日""昨天" —— 这些是分隔符,不是谁说的话。 */
    private val TIMESTAMP = Regex("""\d{1,2}[:：]\d{2}|\d+月\d+日""")

    /**
     * 时间戳能由哪些碎片拼出来。**把它们全部抠掉之后什么都不剩的那一行,
     * 才是一个纯粹的时间分隔符。**
     *
     * 判据故意不是"长度"。按长度判的话"明天15:00开会"只有九个字,会被当成
     * 时间戳丢掉 —— 而那正是副驾最该读懂的一类消息。
     *
     * **"星期三""周三"不在里面,是有意的。** 它们更可能是一句真的短回答
     * (回"哪天?"的那种),而真实的日期分隔符总会带上一个具体钟点。
     * 代价是"星期三"这样一条分隔符会被当成消息留下来 —— 那只是一点噪音,
     * 而丢掉最后一条真消息会让"最后一条是谁说的"整个判错。
     */
    private val TIMESTAMP_TOKENS = Regex(
        """昨天|今天|前天|上午|下午|凌晨|中午|晚上|\d{4}年|\d{1,2}月|\d{1,2}日|\d{1,2}[:：]\d{2}|\s+"""
    )

    /** 中文句读。**一句话里有它,一个标题里不会有。** */
    private val SENTENCE_PUNCT = Regex("""[，。？！、；]""")

    /** 群名后面那个成员数,半角全角都有。 */
    private val GROUP_COUNT_SUFFIX = Regex("""[（(]\d+[）)]""")

    /**
     * 这一行里**有**时间戳。松的那个判据,只给挑标题用 ——
     * 一个会话标题里不会出现钟点。
     */
    fun isTimestamp(text: String): Boolean =
        TIMESTAMP.containsMatchIn(text) || text.trim() == "昨天" || text.trim() == "今天"

    /**
     * 这一行**只是**一个时间戳。紧的那个判据,给挑消息用。
     *
     * 两个判据不能合成一个:标题那边要的是"沾到时间就不是标题",
     * 消息这边要的是"整条都是时间才不是消息"。合起来的那一刻,
     * 要么标题会挑到一条消息,要么"明天15:00开会"会被丢掉。
     */
    fun isPureTimestamp(text: String): Boolean {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return false
        return TIMESTAMP_TOKENS.replace(trimmed, "").isEmpty()
    }

    /**
     * 靠气泡中心在屏幕的哪一半判断谁说的 —— 微信用这个。
     *
     * 微信把自己发的气泡靠右贴,对方的靠左贴,而**气泡宽度上限只有屏幕的七成左右**,
     * 所以中心点永远落在自己那一半里。
     */
    fun sideByCenter(bubble: RawBubble, screenWidth: Int): String =
        if (bubble.centerX > screenWidth / 2) SIDE_ME else SIDE_OTHER

    /**
     * 靠气泡哪一边贴着头像列判断谁说的 —— QQ 用这个。
     *
     * **不能用中心点。** QQ 的头像钉在各自那一侧的最外边,而一条长消息的气泡
     * 会从对方那侧一直铺过屏幕中线 —— 那时中心点在右边,但说话的是对方。
     * 比"哪条边离自己那侧的头像列更近"才稳。
     */
    fun sideByAvatarColumn(bubble: RawBubble, screenWidth: Int): String {
        val avatarEdge = (screenWidth * QQ_AVATAR_COLUMN_RATIO).toInt()
        val toLeftColumn = kotlin.math.abs(bubble.left - avatarEdge)
        val toRightColumn = kotlin.math.abs((screenWidth - avatarEdge) - bubble.right)
        return if (toRightColumn < toLeftColumn) SIDE_ME else SIDE_OTHER
    }

    private const val QQ_AVATAR_COLUMN_RATIO = 0.13

    /**
     * 气泡 → 消息列表。**从上到下排好,该丢的丢掉。**
     *
     * 丢三种:空白的、看起来是时间戳的、[VerificationCode] 认出来的。
     *
     * **最后那一种在这条通道上同样要有**([铁律 11](AGENTS.md) 不因通道不同而放宽)。
     * 屏幕上出现验证码的概率比通知栏低,但不是零 —— 短信也能在聊天窗里被转发,
     * 而整屏 OCR 会把顶上飘过的通知横幅一起读进来。
     * 命中的**整条不要**,不上传也不写本地历史。
     */
    fun toMessages(bubbles: List<RawBubble>, side: (RawBubble) -> String): List<Msg> =
        bubbles
            .sortedBy { it.top }
            .mapNotNull { bubble ->
                val text = bubble.text.trim()
                when {
                    text.isEmpty() -> null
                    isPureTimestamp(text) -> null
                    VerificationCode.matches(text) -> null
                    else -> Msg(side(bubble), text)
                }
            }
            .takeLast(MAX_BUBBLES)

    /**
     * 动作栏里的会话标题:第一条气泡上方、大致居中、最靠上的那个短文字。
     *
     * QQ 拿不到标题 id 时退到这里。[centerFrom]/[centerTo] 是"大致居中"的宽窄 ——
     * 有的 App 把标题左对齐,那时候要放宽。
     */
    fun pickTitle(
        candidates: List<TitleCandidate>,
        firstBubbleTop: Int,
        screenWidth: Int,
        screenHeight: Int,
        centerFrom: Double = 0.25,
        centerTo: Double = 0.75,
    ): String? =
        inActionBar(candidates, firstBubbleTop, screenWidth, screenHeight, centerFrom, centerTo)
            .minByOrNull { it.top }
            ?.text

    /**
     * 微信的会话标题。**比通用那个多两条规矩,两条都是被真实界面逼出来的:**
     *
     * 1. **像句子的不算。** 群置顶公告和刚好停在顶上的一条消息,会正好落在
     *    "最靠上、够短、居中"这个筛子里。带中文句读的一律排除 ——
     *    标题里不会有逗号句号
     * 2. **带成员数的优先。** 群标题长成"项目群(12)",而它旁边可能还有别的短文字。
     *    那个括号数字是个很强的信号
     *
     * 一个都不合格就返回 null,**不要退而求其次挑一个** ——
     * 调用方会沿用上一个稳定标题,那比猜一个错的强:标题是服务端查记忆的 key,
     * 猜错了会把张三的事实检索到李四的对话里。
     */
    fun pickWeChatTitle(
        candidates: List<TitleCandidate>,
        firstBubbleTop: Int,
        screenWidth: Int,
        screenHeight: Int,
    ): String? {
        val usable = inActionBar(candidates, firstBubbleTop, screenWidth, screenHeight, 0.25, 0.75)
            .filter { !SENTENCE_PUNCT.containsMatchIn(it.text) }
        val grouped = usable.filter { GROUP_COUNT_SUFFIX.containsMatchIn(it.text) }
        return (grouped.minByOrNull { it.top } ?: usable.minByOrNull { it.top })?.text
    }

    private fun inActionBar(
        candidates: List<TitleCandidate>,
        firstBubbleTop: Int,
        screenWidth: Int,
        screenHeight: Int,
        centerFrom: Double,
        centerTo: Double,
    ): List<TitleCandidate> {
        val barBottom = minOf(firstBubbleTop, actionBarBottom(screenHeight))
        val minCenterX = (screenWidth * centerFrom).toInt()
        val maxCenterX = (screenWidth * centerTo).toInt()
        return candidates.filter {
            it.text.isNotBlank() &&
                it.text.length <= TITLE_MAX_CHARS &&
                !isTimestamp(it.text) &&
                it.bottom in 1 until barBottom &&
                it.centerX in minCenterX..maxCenterX
        }
    }

    /**
     * 这个标题是不是 App 还没加载完时的占位。
     *
     * **不能让它盖掉真标题**:占位文字会被当成会话名传给服务端去查记忆,
     * 查不到倒是小事 —— 更糟的是它会被写进本地历史,变成一个叫"连接中"的联系人。
     * 空的也算占位,这样调用方只有一条退路要处理。
     */
    fun isTransientTitle(title: String?): Boolean {
        val trimmed = title?.trim()?.trimEnd('…', '.')?.trim()
        if (trimmed.isNullOrEmpty()) return true
        return TRANSIENT_WORDS.any { trimmed.contains(it, ignoreCase = true) }
    }

    private val TRANSIENT_WORDS = listOf(
        "连接中", "正在连接", "未连接", "加载中", "同步中",
        "Connecting", "Loading", "Syncing",
    )

    const val SIDE_ME = "me"
    const val SIDE_OTHER = "other"
}
