package ltd.iclab.lifein.copilot

import android.content.res.Resources
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 一个聊天 App 的读屏规则 —— **架构 §9.5 那个扩展点**。
 *
 * 加一个聊天 App = 写一个实现 + 在 [ChatCaptureService] 的分发表里加一行。
 * 判断、起草、排序、悬浮窗、填入全部不用动。
 *
 * [extract] 的返回值**是三态的**,契约写在 [ChatSnapshot] 上。
 *
 * 真正的成本不在这几十行代码,在拿 `adb shell uiautomator dump` 把目标 App 的
 * 节点结构摸清楚,而且对方大版本更新之后要重摸(ADR-035 把这笔账记成
 * "每年几次、每次半天")。所以**这里的每个 id 都要写清楚是在哪个版本上验的** ——
 * 将来读不到的时候,第一个要查的就是"我们当时验的是哪一版"。
 */
interface ChatAppAdapter {

    /** 包名。分发表的 key。 */
    val pkg: String

    /** 服务端认得的 App 标识,进 `POST /app/copilot/analyze` 的 `app` 字段。 */
    val serverApp: String

    /** 设置页上显示的名字。 */
    val label: String

    fun extract(root: AccessibilityNodeInfo, res: Resources): ChatSnapshot?
}

/**
 * 一次遍历收集到的东西。
 *
 * **一棵树只走一遍。** 标题、气泡、输入框各走一遍的话,这个函数会在
 * 每秒好几次的 content-changed 事件上跑三遍 —— 而它跑在主线程上。
 */
internal class TreeScan {
    val bubbles = ArrayList<ChatShaping.RawBubble>()
    val titleCandidates = ArrayList<ChatShaping.TitleCandidate>()

    /** 见到过气泡容器 —— **哪怕它的文字是空的**。这是"在不在聊天窗"的答案。 */
    var sawBubble = false

    /** 见到过输入框。QQ 全程一个 Activity,只能靠这个判断在不在聊天窗。 */
    var sawInput = false

    /** 从专门的标题 id 上直接读到的标题。有它就不用猜了。 */
    var titleById: String? = null

    var firstBubbleTop = Int.MAX_VALUE
}

/**
 * 走一遍节点树。
 *
 * 用显式栈不用递归:聊天列表的层级可以很深,而这个函数跑在主线程上,
 * `StackOverflowError` 在这里的表现是整个无障碍服务被系统解绑。
 * [ChatShaping.NODE_GUARD] 是第二道 —— 树自引用的时候不至于转死。
 */
internal fun scanTree(
    root: AccessibilityNodeInfo,
    bubbleId: String,
    titleBandBottom: Int,
    titleId: String? = null,
    inputId: String? = null,
): TreeScan {
    val scan = TreeScan()
    val stack = ArrayDeque<AccessibilityNodeInfo>()
    stack.addLast(root)
    var guard = 0
    while (stack.isNotEmpty() && guard < ChatShaping.NODE_GUARD) {
        guard++
        val node = stack.removeLast()
        val id = node.viewIdResourceName
        val text = node.text?.toString()

        if (id == bubbleId) {
            // **文字是空的也要记下来见过它。** 微信 8.0.52 起对普通无障碍服务
            // 隐藏节点文本,那时树里只剩下空气泡 —— 那正是 OCR 兜底存在的理由,
            // 而把它当成"不在聊天窗"的话,兜底永远不会被触发
            scan.sawBubble = true
            if (!text.isNullOrBlank()) {
                val b = Rect()
                node.getBoundsInScreen(b)
                scan.bubbles.add(ChatShaping.RawBubble(b.top, b.left, b.right, text))
                if (b.top < scan.firstBubbleTop) scan.firstBubbleTop = b.top
            }
        }
        if (inputId != null && !scan.sawInput && id == inputId) scan.sawInput = true
        if (titleId != null && id == titleId && scan.titleById == null && !text.isNullOrBlank()) {
            scan.titleById = text
        }
        // 标题候选**在这里就按"够短、在动作栏那一条里"筛一遍**。
        // 不筛的话,一个几十个节点的聊天窗每秒要白造几十个 Rect,
        // 而这个函数跑在主线程上、一秒可能跑好几次。
        // 这里筛出来的是 `ChatShaping.inActionBar` 所需的超集,它会再精筛一次
        if (!text.isNullOrBlank() && text.length <= ChatShaping.TITLE_MAX_CHARS) {
            val b = Rect()
            node.getBoundsInScreen(b)
            if (b.bottom in 1 until titleBandBottom) {
                scan.titleCandidates.add(
                    ChatShaping.TitleCandidate(text, b.top, b.bottom, b.centerX())
                )
            }
        }

        for (i in node.childCount - 1 downTo 0) node.getChild(i)?.let { stack.addLast(it) }
    }
    return scan
}

/**
 * 微信(com.tencent.mm)。
 *
 * 气泡容器是 `id/bkl`,谁说的看气泡贴哪边([ChatShaping.sideByCenter])。
 *
 * **"在聊天窗"的判据只有一条:树里有 `id/bkl`。** 不能用"有没有输入框" ——
 * 会话列表顶上那个搜索框也是可编辑的,拿它当判据的话,一打开微信主界面
 * 就会开始截屏 OCR。
 *
 * 节点 id 是在**微信 8.0.78** 上验的(ADR-035)。读不到的时候先确认版本:
 * 这些 id 是混淆产物,大版本更新后会变。
 */
class WeChatAdapter : ChatAppAdapter {

    override val pkg = "com.tencent.mm"
    override val serverApp = "wechat"
    override val label = "微信"

    override fun extract(root: AccessibilityNodeInfo, res: Resources): ChatSnapshot? {
        val height = res.displayMetrics.heightPixels
        val scan = scanTree(
            root,
            bubbleId = BUBBLE_ID,
            titleBandBottom = ChatShaping.actionBarBottom(height),
        )
        if (!scan.sawBubble) return null

        val width = res.displayMetrics.widthPixels
        val title = ChatShaping.pickWeChatTitle(
            scan.titleCandidates,
            firstBubbleTop = scan.firstBubbleTop,
            screenWidth = width,
            screenHeight = height,
        )
        // 在聊天窗但一句都读不出来 → 空快照,那是 OCR 兜底的信号,不是失败
        val messages = ChatShaping.toMessages(scan.bubbles) {
            ChatShaping.sideByCenter(it, width)
        }
        return ChatSnapshot(title, messages)
    }

    private companion object {
        const val BUBBLE_ID = "com.tencent.mm:id/bkl"
    }
}

/**
 * 手机 QQ(com.tencent.mobileqq)。
 *
 * 正文是普通 TextView,带 `id/mjn` —— 只收这一个 id 就已经把时间戳、
 * 群昵称(`id/mjq`)、系统提示条排除在外了。
 *
 * **QQ 全程只有一个 SplashActivity**(fragment 架构),所以"在不在聊天窗"
 * 只能问树:没有输入框 `id/input` 就不是聊天窗。
 *
 * 谁说的看气泡贴哪边的头像列,不看中心点 —— 理由在
 * [ChatShaping.sideByAvatarColumn] 上。
 *
 * 节点 id 是在 **QQ 9.3.50 / 1200×2670** 上验的。QQ 不混淆节点,
 * 所以这几个 id 比微信那个稳。
 */
class QQAdapter : ChatAppAdapter {

    override val pkg = "com.tencent.mobileqq"
    override val serverApp = "qq"
    override val label = "QQ"

    override fun extract(root: AccessibilityNodeInfo, res: Resources): ChatSnapshot? {
        val height = res.displayMetrics.heightPixels
        val scan = scanTree(
            root,
            bubbleId = BUBBLE_ID,
            titleBandBottom = ChatShaping.actionBarBottom(height),
            titleId = TITLE_ID,
            inputId = INPUT_ID,
        )
        if (scan.bubbles.isEmpty() && !scan.sawInput) return null

        val width = res.displayMetrics.widthPixels
        val title = scan.titleById ?: ChatShaping.pickTitle(
            scan.titleCandidates,
            firstBubbleTop = scan.firstBubbleTop,
            screenWidth = width,
            screenHeight = height,
        )
        val messages = ChatShaping.toMessages(scan.bubbles) {
            ChatShaping.sideByAvatarColumn(it, width)
        }
        return ChatSnapshot(title, messages)
    }

    private companion object {
        const val BUBBLE_ID = "com.tencent.mobileqq:id/mjn"
        const val TITLE_ID = "com.tencent.mobileqq:id/371"
        const val INPUT_ID = "com.tencent.mobileqq:id/input"
    }
}

/**
 * 认得的聊天 App。**加新 App 只动这一行**(架构 §9.5)。
 *
 * 和服务端 `api/copilot.ALLOWED_APPS` 是两份名单,故意的:
 * 手机端多一个而服务端还没认的话,那次请求会 422 而不是被当成微信处理。
 */
val CHAT_ADAPTERS: List<ChatAppAdapter> = listOf(WeChatAdapter(), QQAdapter())
