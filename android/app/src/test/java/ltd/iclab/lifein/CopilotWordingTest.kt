package ltd.iclab.lifein

import ltd.iclab.lifein.copilot.CopilotState
import ltd.iclab.lifein.copilot.CopilotWording
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 悬浮窗上那几句话。
 *
 * 一句提示写错不会崩,也不会留下任何日志 —— 它只会让用户在**真该警觉的时候**
 * 以为是自己网不好,或者对着一个永远不会变的错误一直重试。
 * 这一层没有别的办法被发现,所以它有用例。
 */
class CopilotWordingTest {

    @Test
    fun `an empty tree and a missing adapter are two different sentences`() {
        // **架构 §8.7 的硬要求。** 前者是 ADR-035 的重评触发条件
        // (微信又改了混淆方式,那时要停下来判断还追不追),后者只是没写适配器。
        // 混成一句"读取失败"等于把那个退出闸门藏起来
        val emptyTree = CopilotWording.diagnosis(CopilotState.EMPTY_TREE)
        val noAdapter = CopilotWording.diagnosis(CopilotState.NO_ADAPTER)
        val notAllowed = CopilotWording.diagnosis(CopilotState.NOT_ALLOWED)

        assertNotEquals(emptyTree, noAdapter)
        assertNotEquals(noAdapter, notAllowed)
        assertNotEquals(emptyTree, notAllowed)
    }

    @Test
    fun `the empty-tree sentence does not blame the user`() {
        // 这一条是"对方改了防线",不是"你哪里没配好"。
        // 说成后者的话,用户会去反复检查权限,而权限一直是对的
        val text = CopilotWording.diagnosis(CopilotState.EMPTY_TREE)
        assertTrue(text, text.contains("不是你的问题"))
    }

    @Test
    fun `http failures that cannot be retried say so differently`() {
        // 404 是服务端没开副驾,401 是凭据被吊销 —— 这两个再点一百次也一样。
        // 503 才是"再试一下真的可能成"
        assertNotEquals(CopilotWording.failure(404), CopilotWording.failure(503))
        assertNotEquals(CopilotWording.failure(401), CopilotWording.failure(404))
        assertTrue(CopilotWording.failure(404).contains("COPILOT_ENABLED"))
        assertEquals("连不上服务器", CopilotWording.failure(null))
    }

    @Test
    fun `running out of quota is not described as a failure`() {
        // 额度用完不是故障,是这个月的钱花完了,下个月自己就好。
        // 说成"失败"会让用户去重试,而重试不会有任何不同
        val text = CopilotWording.degradedNote("quota")
        assertTrue(text, text!!.contains("下个月"))
        assertNull(CopilotWording.degradedNote(null))
        assertNull(CopilotWording.degradedNote(""))
    }

    @Test
    fun `every degradation the server can send has its own sentence`() {
        // 服务端会给这四种。任何两种说成同一句话,就等于少了一种可观察的状态
        val notes = listOf("quota", "draft", "draft_short", "rank").map {
            CopilotWording.degradedNote(it)
        }
        assertEquals(notes.size, notes.toSet().size)
        // 没见过的那种也要说点什么,而且要把原值带出来 —— 服务端可能比 App 新
        assertTrue(CopilotWording.degradedNote("something_new")!!.contains("something_new"))
    }

    @Test
    fun `an OCR capture admits it may have misread, but not that it lost the sides`() {
        // 自动那条 OCR 是按气泡矩形逐个识别的,**位置本身就回答了谁说的** ——
        // 所以这句话要认的是"可能认错字",不是"分不清谁说的"。
        // 承认一个其实没犯的错,只会让用户白白不信任结果
        val text = CopilotWording.captureNote("ocr")
        assertTrue(text, text!!.contains("认错"))
        assertTrue(text, !text.contains("分不清"))
        // 正常读树的时候不用说话:每一条都说等于没有一条被读
        assertNull(CopilotWording.captureNote(""))
        assertNull(CopilotWording.captureNote("tree"))
    }

    @Test
    fun `danger buckets are three, not ten`() {
        assertEquals(CopilotWording.Heat.CALM, CopilotWording.heat(0))
        assertEquals(CopilotWording.Heat.CALM, CopilotWording.heat(2))
        assertEquals(CopilotWording.Heat.WARN, CopilotWording.heat(3))
        assertEquals(CopilotWording.Heat.WARN, CopilotWording.heat(5))
        assertEquals(CopilotWording.Heat.HOT, CopilotWording.heat(6))
        assertEquals(CopilotWording.Heat.HOT, CopilotWording.heat(9))
    }

    @Test
    fun `an unknown enum reads like uncertainty, not like an error`() {
        // 服务端的枚举里 unknown 是一个正经结果("模型拿不准"),不是故障。
        // 显示成"未知"会让用户以为 App 坏了,而那时他最该做的是自己看一眼对话
        assertEquals("说不好", CopilotWording.needs("unknown"))
        assertEquals("看不太出来", CopilotWording.intent("unknown"))
        assertEquals("自己拿捏", CopilotWording.bestAction("unknown"))
        // 服务端加了新枚举而 App 还没跟上时,走的是同一条路,不是崩
        assertEquals("说不好", CopilotWording.needs("some_new_enum"))
    }

    @Test
    fun `a hidden meaning is called out, a literal one is not`() {
        val hidden = CopilotWording.summary("explanation", "explain", literal = false)
        val plain = CopilotWording.summary("explanation", "explain", literal = true)
        assertTrue(hidden, hidden.contains("不止字面意思"))
        assertTrue(plain, !plain.contains("不止字面意思"))
    }

    @Test
    fun `should_reply_now is about what to say, not how fast`() {
        // 对方说"我在开会晚点细说"的时候,回一句收到是对的,
        // 回一段方案是把话堵回去。这一行不能写成"待会儿再回"
        val text = CopilotWording.replyTiming(false)
        assertTrue(text, text!!.contains("别给实质内容"))
        assertNull(CopilotWording.replyTiming(true))
    }

    @Test
    fun `using no memory at all is stated, not hidden`() {
        // 陌生人也能用副驾,那不是错 —— 但"这次没用上记忆"要说出来,
        // 否则用户会以为它记得而其实没有
        assertEquals("没用上记忆和历史", CopilotWording.contextNote(0, 0))
        assertTrue(CopilotWording.contextNote(3, 12).contains("3"))
    }
}
