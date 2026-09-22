package ltd.iclab.lifein

import ltd.iclab.lifein.copilot.ChatLogMerge
import ltd.iclab.lifein.copilot.ChatShaping
import ltd.iclab.lifein.copilot.Msg
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 本地历史的合并(ADR-038)。
 *
 * **这一层错起来完全静默。** 不崩、不报错、日志里看不出来 ——
 * 只是送进模型的历史里同一句话出现了十几遍,而模型会当真,
 * 以为对方在反复强调。等到候选开始变怪的时候,已经很难往回查了。
 *
 * 读屏拿到的是**一屏**,不是"新到的那几条"。每来一条消息就重读一次整屏,
 * 而那一屏里上面十几条都是刚才记过的 —— 这就是为什么需要这个东西。
 */
class CopilotHistoryMergeTest {

    private fun other(text: String) = Msg("other", text)
    private fun me(text: String) = Msg("me", text)

    @Test
    fun `the first screen goes in whole`() {
        val screen = listOf(other("在吗"), me("在"))
        val plan = ChatLogMerge.plan(stored = emptyList(), screen = screen)
        assertEquals(screen, plan.append)
        assertEquals(0, plan.historyBefore)
    }

    @Test
    fun `a screen that scrolled by two only appends those two`() {
        // 屏幕往下滚了两条:上面那截历史里已经有了
        val stored = listOf(other("一"), me("二"), other("三"))
        val screen = listOf(me("二"), other("三"), me("四"), other("五"))
        val plan = ChatLogMerge.plan(stored, screen)
        assertEquals(listOf(me("四"), other("五")), plan.append)
        // 历史里能送出去的只有第一条 —— 后两条已经在 messages 里了
        assertEquals(1, plan.historyBefore)
    }

    @Test
    fun `the same screen twice writes nothing`() {
        val stored = listOf(other("一"), me("二"))
        val plan = ChatLogMerge.plan(stored, screen = stored)
        assertTrue(plan.append.isEmpty())
        // 整屏都在 messages 里,一条历史都送不出去
        assertEquals(0, plan.historyBefore)
    }

    @Test
    fun `scrolling up into old messages writes nothing and sends no history`() {
        // **这是最要紧的一条。** 少了它,用户往上一翻,一整屏旧消息就会被
        // 当成新的追加到结尾 —— 而它们看起来和真的新消息一模一样
        val stored = listOf(other("一"), me("二"), other("三"), me("四"), other("五"))
        val screen = listOf(other("一"), me("二"), other("三"))
        val plan = ChatLogMerge.plan(stored, screen)
        assertTrue(plan.reason, plan.append.isEmpty())
        // 屏幕上那些本身就是历史,截不出一个"排在它们之前"的区间
        assertEquals(0, plan.historyBefore)
    }

    @Test
    fun `a brand new conversation appends everything and keeps all history`() {
        // 键不同的对话各存各的,但同一个键下换了话题(比如中间隔了好几天)
        // 也可能一条都对不上 —— 那时整屏都是新的
        val stored = listOf(other("上周的事"), me("好"))
        val screen = listOf(other("今天有空吗"), me("有"))
        val plan = ChatLogMerge.plan(stored, screen)
        assertEquals(screen, plan.append)
        assertEquals(2, plan.historyBefore)
    }

    @Test
    fun `identical lines on one screen stay two entries`() {
        // 单位是序列不是集合。一屏上两句"嗯"是两条 ——
        // 折成一条会让历史里的对话错位,而错位之后的对话读起来仍然通顺
        val screen = listOf(other("嗯"), other("嗯"))
        val plan = ChatLogMerge.plan(emptyList(), screen)
        assertEquals(2, plan.append.size)
    }

    @Test
    fun `the overlap is the largest one, not the first one found`() {
        // 历史结尾是"嗯",而这一屏开头第三条也是"嗯"。取最小的重合
        // 会接在第一个"嗯"后面,把中间两条整个吞掉
        val stored = listOf(me("甲"), other("乙"), me("嗯"))
        val screen = listOf(me("嗯"), other("丙"), me("嗯"), other("丁"))
        val plan = ChatLogMerge.plan(stored, screen)
        assertEquals(listOf(other("丙"), me("嗯"), other("丁")), plan.append)
    }

    @Test
    fun `who said it is part of the comparison key`() {
        // 同一句"好啊",我说的和对方说的是两条不同的消息。
        // 键里不带 side 的话,历史里的一问一答会被合成一句
        assertTrue(ChatLogMerge.key(me("好啊")) != ChatLogMerge.key(other("好啊")))
    }

    @Test
    fun `blank lines never reach the log`() {
        val plan = ChatLogMerge.plan(emptyList(), listOf(other("  "), other("在吗")))
        assertEquals(listOf(other("在吗")), plan.append)
    }
}

/**
 * 截屏 OCR 那条路上的文字清理。
 *
 * 读树很少见到这些尾巴,**OCR 几乎每条都有** —— 一个气泡矩形里认出来的字
 * 会把角落里那个"14:05"和"已读"一起收进来,而它们会被模型当成对方说的话。
 */
class CopilotBubbleTextTest {

    @Test
    fun `a trailing timestamp and read receipt come off`() {
        assertEquals("这个周末有空吗", ChatShaping.cleanBubbleText("这个周末有空吗 14:05 已读"))
        assertEquals("好的", ChatShaping.cleanBubbleText("好的 已读"))
        assertEquals("好的", ChatShaping.cleanBubbleText("好的 9:30"))
    }

    @Test
    fun `a time in the middle of a sentence stays`() {
        // **"明天15:00开会"里那个钟点是内容,不是角标。**
        // 剥的规则只认结尾,不认句中
        assertEquals("明天15:00开会", ChatShaping.cleanBubbleText("明天15:00开会"))
        assertEquals("15:00 那场会改了", ChatShaping.cleanBubbleText("15:00 那场会改了"))
    }

    @Test
    fun `stacked tails come off one by one`() {
        assertEquals("行吧", ChatShaping.cleanBubbleText("行吧 已读 14:05 已读"))
    }

    @Test
    fun `a bubble that is nothing but chrome ends up empty`() {
        assertEquals("", ChatShaping.cleanBubbleText(" 已读 "))
    }
}
