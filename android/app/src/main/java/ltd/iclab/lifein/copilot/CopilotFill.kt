package ltd.iclab.lifein.copilot

import android.accessibilityservice.AccessibilityService
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.util.Log
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo

/**
 * 把一条候选填进聊天输入框。**副驾唯一的写动作,而且到此为止。**
 *
 * ## 这个文件是被单独圈出来的
 *
 * 副驾包里**只有这个文件允许出现 `performAction`**,而且只能作用在输入框上
 * —— `CopilotNeverSendsTest` 盯着这两条。
 *
 * 圈它出来不是因为这里的代码危险,是因为**发送键就在输入框旁边**:
 * 一个为了修"填入之后光标没进去"而顺手加的 `ACTION_CLICK`,只要节点取错一层,
 * 就会变成"副驾自己把话发出去了"。那种改动在 code review 里看起来完全无害,
 * 而它破掉的是 01 §8 那条定义 —— **发送键永远是人按的**。
 *
 * 所以这里的做法是:**能被操作的节点只有一种类型**([EditableBox]),
 * 而它只能从 [findEditable] 拿到。一个叫 `sendButton` 的变量在这里编译得过,
 * 但过不了那个用例。
 *
 * ## 为什么要三层
 *
 * `ACTION_SET_TEXT` 在输入框已经有焦点、而且输入法没有在组词的时候好用。
 * 另外两种情况都真实存在:
 *
 * 1. **框没焦点。** `SET_TEXT` 会**返回 true 然后什么都不做** ——
 *    所以每一层之后都要读回来核对,不能信返回值
 * 2. **输入法在组词。** 微信配某些输入法时,组词区会把 `SET_TEXT` 吃掉。
 *    那时只剩剪贴板 + `ACTION_PASTE`
 *
 * 一层都不成就把文字放进剪贴板,让用户自己长按粘贴。**那也是一种成功** ——
 * 副驾的价值是"想好说什么",填进去只是省几下手。
 */
class CopilotFill(private val service: AccessibilityService) {

    /**
     * 输入框。**唯一能被操作的节点类型。**
     *
     * 用 value class 包一层不是为了类型安全上的洁癖:它让"对一个非输入框的节点
     * 做动作"这件事**在这个文件里写不出来** —— 没有别的地方能造出 [EditableBox]。
     */
    @JvmInline
    value class EditableBox(val node: AccessibilityNodeInfo)

    sealed interface Outcome {
        /** 填进去了,而且读回来核对过。 */
        data object Filled : Outcome

        /** 填不进去,已经放进剪贴板。[why] 给面板上那句提示用。 */
        data class Copied(val why: String) : Outcome
    }

    /**
     * 填。**阻塞,要在工作线程上调** —— 里面有 `Thread.sleep`:
     * 点一下输入框之后,输入法要几百毫秒才把焦点交出来,而这中间没有回调可等。
     */
    fun fill(text: String): Outcome {
        if (trySetText(text)) return Outcome.Filled

        // 第二层:先点一下输入框把焦点拿过来,再填一次。
        // **这是整个副驾里唯一一次 ACTION_CLICK,而它点的是输入框。**
        // 取不到输入框就别乱点 —— 屏幕上任何一个别的位置都可能是发送键
        val box = findEditable() ?: return copyOut(text, "没找到输入框")
        box.node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        Thread.sleep(FOCUS_WAIT_MS)
        if (trySetText(text)) return Outcome.Filled

        // 第三层:剪贴板 + 粘贴。**粘之前先清空** ——
        // 上面那次 SET_TEXT 可能其实成功了只是核对没过,不清的话会变成两遍
        copyToClipboard(text)
        val focused = findEditable() ?: box
        setTextRaw(focused, "")
        val pasted = focused.node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        Thread.sleep(READBACK_WAIT_MS)
        val after = readBack()
        Log.i(TAG, "填入:paste=$pasted 读回长度=${after?.length ?: -1} 目标长度=${text.length}")
        // 读回来含上了就算成;读不回来但粘贴动作报成功也算 ——
        // 有些输入框拿不到 text,那时候硬判失败会让用户白复制一次
        return if ((after != null && after.contains(text)) || (pasted && after == null)) {
            Outcome.Filled
        } else {
            Outcome.Copied("输入法把填入吃掉了")
        }
    }

    /**
     * 填一次并**读回来核对**。
     *
     * 核对这一步不能省:`ACTION_SET_TEXT` 对一个没有焦点的输入框
     * **会返回 true 然后什么都不做**。信了返回值的表现是"提示已填入,
     * 但输入框是空的" —— 而用户那时已经把注意力移到发送键上了。
     */
    private fun trySetText(text: String): Boolean {
        val box = findEditable() ?: return false
        if (!setTextRaw(box, text)) return false
        Thread.sleep(READBACK_WAIT_MS)
        return readBack() == text
    }

    private fun setTextRaw(box: EditableBox, text: String): Boolean {
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return box.node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    /**
     * 输入框现在的内容。**要先 `refresh()`** ——
     * 动作刚做完时节点缓存里还是旧值(多半是空的),而那会让一次成功的填入
     * 看起来像失败,于是第三层再粘一遍,结果是两份文字叠在一起。
     */
    private fun readBack(): String? {
        val box = findEditable() ?: return null
        runCatching { box.node.refresh() }
        return box.node.text?.toString()
    }

    /**
     * 找当前窗口里的输入框。**深度优先,第一个可编辑的就算。**
     *
     * 聊天窗里通常只有一个可编辑节点。真有第二个(比如搜索框同时在树上)时
     * 这里会挑错 —— 那时的表现是"填到了别的框里",用户一眼就看得见,
     * 而这比"往下继续找、找到了发送键那一层"安全得多。
     */
    private fun findEditable(): EditableBox? {
        val root = service.rootInActiveWindow ?: return null
        val stack = ArrayDeque<AccessibilityNodeInfo>()
        stack.addLast(root)
        var guard = 0
        while (stack.isNotEmpty() && guard < ChatShaping.NODE_GUARD) {
            guard++
            val node = stack.removeLast()
            if (node.isEditable) return EditableBox(node)
            for (i in node.childCount - 1 downTo 0) node.getChild(i)?.let { stack.addLast(it) }
        }
        return null
    }

    private fun copyOut(text: String, why: String): Outcome {
        copyToClipboard(text)
        return Outcome.Copied(why)
    }

    /**
     * 放进剪贴板。
     *
     * **这是一次内容离开副驾的动作,要如实说。** Android 10 起剪贴板只有
     * 当前获得焦点的 App 读得到,所以风险比早年小 —— 但它仍然会被输入法的
     * 剪贴板历史记下来,而那份历史不在这个项目的控制范围内。
     * 09 §3 那张"什么东西会离开"的表里应当有这一行。
     */
    private fun copyToClipboard(text: String) {
        val manager = service.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        manager.setPrimaryClip(ClipData.newPlainText(CLIP_LABEL, text))
    }

    private companion object {
        const val TAG = "LifeIn/copilot"
        const val CLIP_LABEL = "lifein_copilot"

        /** 点完输入框到焦点真的到手之间的空档。这中间没有回调可等。 */
        const val FOCUS_WAIT_MS = 300L

        /** 动作到节点上的值更新之间的空档。 */
        const val READBACK_WAIT_MS = 150L
    }
}
