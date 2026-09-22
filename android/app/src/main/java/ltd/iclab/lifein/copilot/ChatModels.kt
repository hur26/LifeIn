package ltd.iclab.lifein.copilot

import android.graphics.Rect

/**
 * 副驾从屏幕上读到的一条消息。
 *
 * `side` 只有 `"me"` 和 `"other"` 两个值,**没有第三种**(06 §6.16)。
 * 服务端收到别的值会把这一行丢掉并计数 —— 那个计数是适配器判错边
 * (比如左右反了)唯一能被发现的地方。
 */
data class Msg(val side: String, val text: String)

/**
 * 树能定位、但读不出文字的一个气泡。[rect] 是**屏幕坐标**。
 *
 * 微信 8.0.52 起会对普通无障碍服务隐藏节点文本 —— 那时树里还有气泡的位置,
 * 只是没有内容。截屏 OCR 兜底(P5 第三片)逐个矩形去识别,靠的就是这个。
 */
data class BubbleRect(val rect: Rect, val side: String)

/**
 * 当前聊天窗的一张快照。
 *
 * **[ChatAppAdapter.extract] 的返回值是三态的**(架构 §9.5),而三态是靠这个类
 * 和 `null` 一起表达的:
 *
 * | 返回 | 含义 | 下游 |
 * | --- | --- | --- |
 * | `null` | 不在聊天窗 | 什么都不做 |
 * | [messages] 是空的 | **在**聊天窗,但树里没正文 | 触发截屏 OCR 兜底 |
 * | [messages] 非空 | 正常 | 走分析 |
 *
 * 把第二种写成 `null`,自绘正文的 App 就永远走不到 OCR;
 * 写成抛异常,悬浮窗会在正常滚动时不停闪。
 *
 * [note] 是"这张快照是怎么来的"的说明,要**原样显示在面板上** ——
 * OCR 分不出谁说的,那件事必须让用户看见,而不是悄悄当成对方说的。
 */
data class ChatSnapshot(
    val title: String?,
    val messages: List<Msg>,
    val bubbleRects: List<BubbleRect> = emptyList(),
    val note: String? = null,
) {
    /** 最后一条是谁说的。**自动触发只看这一个值。** */
    val latestFrom: String? get() = messages.lastOrNull()?.side

    /**
     * 最后几条的指纹,用来判断"屏幕上是不是真的变了"。
     *
     * 只取末尾几条而不是全部:上滑翻历史会让列表整体变化,但**对话本身没有变**,
     * 那时候重新分析一次纯属白花钱。新消息一定落在末尾,所以末尾几条够用。
     */
    fun signature(): String =
        messages.takeLast(SIGNATURE_TAIL).joinToString("|") { "${it.side}:${it.text}" }

    private companion object {
        const val SIGNATURE_TAIL = 6
    }
}
