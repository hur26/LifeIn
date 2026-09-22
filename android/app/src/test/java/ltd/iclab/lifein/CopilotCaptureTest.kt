package ltd.iclab.lifein

import ltd.iclab.lifein.copilot.ChatShaping
import ltd.iclab.lifein.copilot.ChatShaping.RawBubble
import ltd.iclab.lifein.copilot.ChatShaping.TitleCandidate
import ltd.iclab.lifein.copilot.ChatSnapshot
import ltd.iclab.lifein.copilot.Msg
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 副驾读屏的整理这一步(`ChatShaping`)。
 *
 * **走节点树那部分没有用例,是有意的**:那部分只是一个带护栏的深度优先遍历,
 * 而它要的 `AccessibilityNodeInfo` 在普通 JUnit 里造不出来。
 * 真正会出错的是整理 —— 谁说的判反了、把群公告当成了标题、
 * 把一条时间戳当成了一句话。这几种错在真机上的表现都一样:
 * 候选答得驴唇不对马嘴,而看不出是哪一步坏的。
 *
 * 和 `ReceiptTextTest` 同一个分法。
 */
class CopilotCaptureTest {

    private val width = 1200
    private val height = 2670

    private fun bubble(text: String, top: Int, left: Int, right: Int) =
        RawBubble(top = top, left = left, right = right, text = text)

    // ---------- 谁说的 ----------

    @Test
    fun `wechat side comes from which half the bubble sits in`() {
        // 微信气泡最宽也就屏幕的七成,所以中心点一定落在自己那一半
        assertEquals("me", ChatShaping.sideByCenter(bubble("嗯", 0, 700, 1100), width))
        assertEquals("other", ChatShaping.sideByCenter(bubble("在吗", 0, 100, 500), width))
    }

    @Test
    fun `a long incoming QQ message crosses the midline and still reads as other`() {
        // **这条是 QQ 不能用中心点的全部理由。** 对方发的一大段话从左边一直铺
        // 过屏幕中线,中心点在右半边 —— 按微信那套规则会判成"我说的",
        // 而那会让副驾以为对方什么都没说,于是什么都不做
        val long = bubble("这个报价比我们预期高了不少而且交付周期也长", top = 300, left = 180, right = 1100)
        assertEquals("me", ChatShaping.sideByCenter(long, width))
        assertEquals("other", ChatShaping.sideByAvatarColumn(long, width))
    }

    @Test
    fun `my own QQ bubble hugs the right avatar column`() {
        val mine = bubble("好", top = 400, left = 700, right = 1044)
        assertEquals("me", ChatShaping.sideByAvatarColumn(mine, width))
    }

    // ---------- 整理成消息 ----------

    @Test
    fun `messages come out top to bottom, whatever order the tree gave them`() {
        // 深度优先遍历给出来的顺序和屏幕上的顺序没有关系。**顺序错了,
        // "最后一条是谁说的"就错了**,而自动触发只看那一个值
        val msgs = ChatShaping.toMessages(
            listOf(
                bubble("第三句", top = 900, left = 100, right = 500),
                bubble("第一句", top = 300, left = 100, right = 500),
                bubble("第二句", top = 600, left = 700, right = 1100),
            )
        ) { ChatShaping.sideByCenter(it, width) }

        assertEquals(listOf("第一句", "第二句", "第三句"), msgs.map { it.text })
        assertEquals(listOf("other", "me", "other"), msgs.map { it.side })
    }

    @Test
    fun `a verification code on screen is dropped here too`() {
        // 铁律 11 不因通道不同而放宽。屏幕上出现验证码的概率比通知栏低,
        // 但不是零 —— 短信可以被转发进聊天窗,而整屏 OCR 会把顶上飘过的
        // 通知横幅一起读进来
        val msgs = ChatShaping.toMessages(
            listOf(
                bubble("你的验证码是 481923,请勿告诉他人", top = 100, left = 100, right = 500),
                bubble("晚上吃什么", top = 200, left = 100, right = 500),
            )
        ) { ChatShaping.sideByCenter(it, width) }

        assertEquals(listOf("晚上吃什么"), msgs.map { it.text })
    }

    @Test
    fun `a bare timestamp is dropped but a message that mentions a time is not`() {
        // **"明天15:00开会"正是副驾最该读懂的那一类。**
        // 这条用例故意写成不带空格的九个字:这一步曾经按长度判
        // ("短又带时间的就是分隔符"),而那个规则正好把这句话丢掉
        val msgs = ChatShaping.toMessages(
            listOf(
                bubble("14:05", top = 100, left = 500, right = 700),
                bubble("昨天 09:12", top = 150, left = 500, right = 700),
                bubble("9月3日", top = 160, left = 500, right = 700),
                bubble("明天15:00开会", top = 200, left = 100, right = 500),
            )
        ) { ChatShaping.sideByCenter(it, width) }

        assertEquals(listOf("明天15:00开会"), msgs.map { it.text })
    }

    @Test
    fun `a one-word reply that happens to be a weekday survives`() {
        // "星期三"更可能是一句真的短回答(回"哪天?"的那种)。
        // 把它当分隔符丢掉的话,最后一条消息没了,"最后一条是谁说的"跟着判错,
        // 而自动触发只看那一个值
        val msgs = ChatShaping.toMessages(
            listOf(bubble("星期三", top = 100, left = 100, right = 400))
        ) { ChatShaping.sideByCenter(it, width) }

        assertEquals(listOf("星期三"), msgs.map { it.text })
    }

    @Test
    fun `blank bubbles are dropped, not turned into empty messages`() {
        val msgs = ChatShaping.toMessages(
            listOf(
                bubble("   ", top = 100, left = 100, right = 500),
                bubble("在吗", top = 200, left = 100, right = 500),
            )
        ) { ChatShaping.sideByCenter(it, width) }

        assertEquals(1, msgs.size)
    }

    // ---------- 标题 ----------

    private fun title(text: String, top: Int, centerX: Int = 600) =
        TitleCandidate(text = text, top = top, bottom = top + 60, centerX = centerX)

    @Test
    fun `a pinned announcement never wins the wechat title`() {
        // **这条是真实界面逼出来的。** 群置顶公告和刚好停在顶上的一条消息
        // 会正好落进"最靠上、够短、居中"这个筛子。标题是服务端查记忆的 key,
        // 挑错了会把张三的事实检索到李四的对话里
        val picked = ChatShaping.pickWeChatTitle(
            listOf(title("我有企微，但是用不习惯", top = 40), title("张三", top = 120)),
            firstBubbleTop = 500,
            screenWidth = width,
            screenHeight = height,
        )
        assertEquals("张三", picked)
    }

    @Test
    fun `a group title with a member count beats a plain one`() {
        val picked = ChatShaping.pickWeChatTitle(
            listOf(title("返回", top = 100), title("项目群(12)", top = 140)),
            firstBubbleTop = 500,
            screenWidth = width,
            screenHeight = height,
        )
        assertEquals("项目群(12)", picked)
    }

    @Test
    fun `nothing qualifying gives null, never a guess`() {
        // 猜一个错标题比没有标题糟得多:没有标题只是查不到记忆
        // (陌生人也能用副驾),错标题会检索出别人的事实
        assertNull(
            ChatShaping.pickWeChatTitle(
                listOf(title("我有企微，但是用不习惯", top = 40)),
                firstBubbleTop = 500,
                screenWidth = width,
                screenHeight = height,
            )
        )
    }

    @Test
    fun `text below the action bar is not a title`() {
        // 第一条气泡以下的东西一律不是标题,哪怕它又短又居中
        assertNull(
            ChatShaping.pickTitle(
                listOf(title("好的", top = 900)),
                firstBubbleTop = 800,
                screenWidth = width,
                screenHeight = height,
            )
        )
    }

    @Test
    fun `off-center text is not a title`() {
        // 左上角那个返回箭头旁边的文字、右上角的菜单,都在动作栏那一条里
        assertNull(
            ChatShaping.pickTitle(
                listOf(title("返回", top = 100, centerX = 80)),
                firstBubbleTop = 500,
                screenWidth = width,
                screenHeight = height,
            )
        )
    }

    @Test
    fun `placeholder titles are recognised so they cannot overwrite a real one`() {
        assertTrue(ChatShaping.isTransientTitle("连接中…"))
        assertTrue(ChatShaping.isTransientTitle("加载中..."))
        assertTrue(ChatShaping.isTransientTitle("  "))
        assertTrue(ChatShaping.isTransientTitle(null))
        assertFalse(ChatShaping.isTransientTitle("张三"))
        // 名字里带"中"的人不该被当成占位
        assertFalse(ChatShaping.isTransientTitle("田中"))
    }

    // ---------- 变没变 ----------

    @Test
    fun `the signature only looks at the tail, so scrolling up is not a change`() {
        // 上滑翻历史会让整个列表变化,但**对话本身没有变** ——
        // 那时重新分析一次纯属白花钱。新消息一定落在末尾
        val tail = (1..6).map { Msg("other", "第 $it 句") }
        val short = ChatSnapshot("张三", tail)
        val withHistory = ChatSnapshot("张三", (1..20).map { Msg("other", "老的 $it") } + tail)
        assertEquals(short.signature(), withHistory.signature())
    }

    @Test
    fun `the same words from the other side are a different signature`() {
        // "好啊"是我说的还是对方说的,决定要不要弹候选。
        // 指纹里不带 side 的话,适配器判反边这件事连指纹都察觉不到
        assertTrue(
            ChatSnapshot("张三", listOf(Msg("me", "好啊"))).signature() !=
                ChatSnapshot("张三", listOf(Msg("other", "好啊"))).signature()
        )
    }

    @Test
    fun `latestFrom is what the auto trigger reads`() {
        assertEquals(
            "other",
            ChatSnapshot("张三", listOf(Msg("me", "我下午给你回"), Msg("other", "那个事呢"))).latestFrom
        )
        assertNull(ChatSnapshot("张三", emptyList()).latestFrom)
    }
}
