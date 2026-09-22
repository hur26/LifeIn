package ltd.iclab.lifein.copilot

/**
 * 悬浮窗上那几句话 —— **枚举怎么变成人话,以及出事时说什么。**
 *
 * 单独拎出来是因为架构 §8.7 给这件事定了一条硬要求:
 * **"读到了空节点"和"这个 App 还没适配"必须显示成两句不同的话。**
 * 前者是 ADR-035 的重评触发条件(微信又改了混淆方式,那时要停下来判断还追不追),
 * 后者只是没写适配器。混成一句"读取失败"等于把那个退出闸门藏起来。
 *
 * 一句提示写错不会崩,也不会有任何日志 —— 它只是让用户在真该警觉的时候
 * 以为是自己网不好。所以这一层要有用例盯着。
 */
object CopilotWording {

    /** 危险度的三档。角标颜色和这三档一一对应。 */
    enum class Heat { CALM, WARN, HOT }

    /**
     * 危险度分档。**阈值照搬参考实现的标注校准**,不是拍脑袋定的。
     *
     * 分三档而不是直接显示 0-9:一个数字要人换算成"要不要紧",
     * 而副驾出现的时机正是用户没空换算的时候。
     */
    fun heat(dangerLevel: Int): Heat = when {
        dangerLevel >= 6 -> Heat.HOT
        dangerLevel >= 3 -> Heat.WARN
        else -> Heat.CALM
    }

    fun heatLabel(dangerLevel: Int): String = when (heat(dangerLevel)) {
        Heat.HOT -> "要紧"
        Heat.WARN -> "留意"
        Heat.CALM -> "平常"
    }

    /**
     * 对方要的是什么。
     *
     * **认不出的时候说"说不好",不说"未知"。** 这一行是给人看的,
     * 而"未知"读起来像出错了 —— 实际上模型拿不准是个正常结果,
     * 而且那时候用户更应该自己看一眼对话,而不是以为 App 坏了。
     */
    fun needs(code: String): String = when (code) {
        "apology" -> "一句认错"
        "action" -> "你去办件事"
        "explanation" -> "一个说法"
        "care" -> "你在不在乎"
        "nothing" -> "没要什么"
        else -> "说不好"
    }

    fun intent(code: String): String = when (code) {
        "confirm_you_care" -> "在确认你还在不在乎"
        "vent_anger" -> "在发泄"
        "request_action" -> "在要你办事"
        "seek_explanation" -> "在要一个说法"
        "casual_chat" -> "闲聊"
        "close_topic" -> "想把话题收了"
        else -> "看不太出来"
    }

    fun bestAction(code: String): String = when (code) {
        "check_history" -> "先翻翻之前说过什么"
        "apologize" -> "先认错"
        "give_commitment" -> "给个准话"
        "explain" -> "说明原因"
        "acknowledge" -> "先接住"
        "say_less" -> "少说两句"
        "make_plan" -> "定个安排"
        else -> "自己拿捏"
    }

    /**
     * 判断那一行。**先说对方要什么,再说你该做什么** ——
     * 顺序反过来会变成"命令用户",而这个功能的定位是副驾不是教练。
     */
    fun summary(needsCode: String, bestActionCode: String, literal: Boolean): String {
        val head = "对方要的是${needs(needsCode)},最该${bestAction(bestActionCode)}"
        return if (literal) head else "$head。这句话不止字面意思"
    }

    /**
     * "这一条该不该给实质内容"。
     *
     * 注意它问的不是"要不要马上回" —— 对方说了"我在开会晚点细说"的时候,
     * 回一句收到是对的,回一段方案是把话堵回去。
     */
    fun replyTiming(shouldReplyNow: Boolean): String? =
        if (shouldReplyNow) null else "这会儿一句收到就够,别给实质内容"

    /**
     * 降级了的话,说清降的是哪一级。**每一种都要能被分辨。**
     *
     * 尤其是额度:那不是故障,是这个月的钱花完了,下个月自己就好 ——
     * 给用户一个含糊的"失败"会让他去重试,而重试不会有任何不同。
     */
    fun degradedNote(code: String?): String? = when (code) {
        null, "" -> null
        "quota" -> "这个月的模型额度用完了,下个月自动恢复"
        "draft" -> "没起草出候选,只有判断"
        "draft_short" -> "候选不足三条,补了一条保守的"
        "rank" -> "没排出先后,下面这个顺序不作数"
        else -> "这次降级了($code)"
    }

    /**
     * 这一屏是怎么读来的。**只有不是正常读树时才说** ——
     * 每一条都说等于没有一条被读。
     *
     * OCR 这句要说"可能有认错的字",**不能说"分不清谁说的"**:
     * 自动那条路是按气泡矩形逐个识别的,位置本身就回答了谁说的。
     * 说成后者是在承认一个其实没犯的错,而那会让用户白白不信任结果。
     */
    fun captureNote(note: String): String? = when (note) {
        "ocr" -> "这一屏的字是截图认出来的,可能有认错的地方"
        "", "tree" -> null
        else -> "采集方式:$note"
    }

    /**
     * 读不到东西时说什么。**这是这个文件存在的主要理由。**
     *
     * 四句话彼此不能换:用户看到哪一句,决定他下一步该做什么 ——
     * 去开权限、去放行、去反馈,还是**什么都别做,因为这条路到头了**。
     */
    fun diagnosis(code: String?): String = when (code) {
        CopilotState.OK -> "读到了"
        // ADR-035 的重评触发条件。**不要把它说成"失败"** ——
        // 它说明的是"对方改了防线",而那要的是一个判断,不是一次重试
        CopilotState.EMPTY_TREE -> "在聊天窗里,但一个字都读不到。多半是这个 App 改了防护,不是你的问题"
        CopilotState.NO_ADAPTER -> "这个 App 还没适配,副驾读不了它"
        CopilotState.NOT_ALLOWED -> "这个 App 还没放行。去 LifeIn 的副驾页面打开它"
        else -> "还没读过任何东西"
    }

    /**
     * 请求没成的时候说什么。[httpCode] 为 null 表示根本没连上。
     *
     * **状态码在这里不能糊成一句"失败"**,因为它们要的下一步动作完全不同:
     * 404 是服务端根本没开副驾(去改 `COPILOT_ENABLED`),
     * 401 是这台设备的凭据被吊销了(重试一百次也一样),
     * 503 是这次没读懂(再试一下真的可能成)。
     *
     * 说错一句的代价很具体:用户对着一个永远不会变的 404 一直点。
     */
    fun failure(httpCode: Int?): String = when (httpCode) {
        null -> "连不上服务器"
        401 -> "这台设备的凭据用不了了,去 App 里重新配一次"
        404 -> "服务端没开副驾(COPILOT_ENABLED)"
        422 -> "这一屏读出来的东西服务端不认,多半是适配器读串了"
        503 -> "这次没读懂,再试一下"
        else -> "服务器返回 $httpCode"
    }

    /** 这次用上了多少背景。0 也要说 —— 陌生人也能用副驾,那不是错。 */
    fun contextNote(factsUsed: Int, historyUsed: Int): String = when {
        factsUsed == 0 && historyUsed == 0 -> "没用上记忆和历史"
        else -> "用上 $factsUsed 条记忆、$historyUsed 条历史"
    }
}
