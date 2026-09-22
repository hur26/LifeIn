package ltd.iclab.lifein.copilot

import android.accessibilityservice.AccessibilityService
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent

/**
 * 副驾的读屏服务 —— **这条通道的入口**(ADR-035,架构 §8.7)。
 *
 * 它读前台那个聊天 App 的当前窗口,交给适配器抽成 [ChatSnapshot],
 * 然后交给 [deliver]。**每个 App 的节点规则都在 [ChatAppAdapter] 里,
 * 这个文件里一行都不该出现具体 id。**
 *
 * ## 它注册的名字不是这个类
 *
 * 清单里注册的是 `com.google.android.accessibility.selecttospeak.SelectToSpeakService`
 * —— 一个空的子类,唯一的区别是类名(见那个文件)。
 * 微信 8.0.52 起对普通命名的无障碍服务隐藏节点文本,而这是目前公开的绕法。
 *
 * **ADR-035 记着一条承诺:如果微信把这条也堵了,这个项目不追。** 不做新的绕法、
 * 不做协议逆向、不做 Xposed。那时表现会是 [CopilotState.EMPTY_TREE],
 * 而那正是重评触发条件。
 *
 * ## 它永远不按发送键
 *
 * 这条不是"目前没实现",是这个功能的定义(01 §8)。
 * 唯一的写动作是往输入框里 `ACTION_SET_TEXT`(P5 第二片),
 * 而**代码里不存在指向发送按钮的 `ACTION_CLICK`,这一条有测试盯着**
 * (`CopilotNeverSendsTest`)。
 *
 * ## 三个默认拒绝叠在一起
 *
 * 1. 系统的无障碍权限 —— 用户自己去设置里开,这个服务才会被绑上来
 * 2. [CopilotPrefs.enabled] —— App 里的总开关,默认关
 * 3. [CopilotPrefs.allowedApps] —— 逐个 App 放行,**空集合 = 一个都不放行**
 *
 * 三道全通了才会读一个字。
 */
open class ChatCaptureService : AccessibilityService() {

    private val main = Handler(Looper.getMainLooper())

    /** 分发表。加一个聊天 App 只动 [CHAT_ADAPTERS]。 */
    private val adapters = CHAT_ADAPTERS.associateBy { it.pkg }

    /** 用 lazy 不用 lateinit:事件回调有可能早于 `onServiceConnected` 到达,
     *  而那时 lateinit 抛出的异常会让系统直接解绑这个服务。 */
    private val prefs by lazy { CopilotPrefs(this) }

    /** 上一次真正处理过的那段对话的指纹。见 [ChatSnapshot.signature]。 */
    private var lastSignature = ""

    /** 上一次处理的是哪个 App。**换 App 要把指纹清掉**,理由在 [maybeCapture]。 */
    private var activePkg: String? = null

    /**
     * 每个 App 上一个**稳定**的会话标题。
     *
     * 需要它是因为有些 App 刚打开聊天页时会短暂显示"连接中…"之类的占位
     * ([ChatShaping.isTransientTitle])。标题是服务端查记忆的 key ——
     * 让占位盖掉真标题的后果是把张三的事实检索到李四的对话里。
     *
     * 换 App 时**不清**:那个 App 下次给出真标题时自然会覆盖掉。
     */
    private val lastGoodTitle = HashMap<String, String>()

    private var pendingSnapshot: ChatSnapshot? = null
    private val debounce = Runnable { pendingSnapshot?.let { deliver(it) } }

    override fun onServiceConnected() {
        super.onServiceConnected()
        CopilotState.setServiceConnected(this, true)
        // 无障碍服务被 ROM 冻结之后不会自己回来(ADR-021 的 2026-09-22 追加)。
        // 起不来不算错 —— 只是少了一层保护,不该让整个服务连不上
        if (prefs.enabled) runCatching { CopilotKeepAlive.start(this) }
        Log.i(TAG, "副驾读屏服务已连接,放行 ${prefs.allowedApps.size} 个 App")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        if (!prefs.enabled) return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            -> maybeCapture()
        }
    }

    /**
     * 读一次当前窗口。**每秒可能被调好几次**,所以每一步都要尽早返回。
     *
     * 前台包名取自 `rootInActiveWindow`,**不是 `event.packageName`** ——
     * 键盘弹出来的时候事件的包名是输入法(比如 `com.tencent.wetype`),
     * 而聊天 App 仍然在前台。按事件包名判断的表现是悬浮窗在打字时不停闪。
     */
    private fun maybeCapture() {
        val root = rootInActiveWindow ?: return
        val pkg = root.packageName?.toString() ?: return

        val adapter = adapters[pkg]
        if (adapter == null) {
            // 没适配器不是"坏了",只是没写。**这两件事要显示成两句不同的话**
            // (架构 §8.7),所以诊断码也必须是两个
            noteDiagnosis(CopilotState.NO_ADAPTER, 0)
            return
        }
        if (!prefs.allows(pkg)) {
            noteDiagnosis(CopilotState.NOT_ALLOWED, 0)
            return
        }

        // 不在聊天窗(会话列表、朋友圈、设置页)→ 什么都不做,连状态都不记:
        // 这一条每秒会走很多次,记下来只会把真正有用的那条诊断冲掉
        val raw = adapter.extract(root, resources) ?: return
        val snapshot = stabilizeTitle(pkg, raw)

        if (snapshot.messages.isEmpty()) {
            // 在聊天窗,但树里一句正文都没有。**这是 ADR-035 的重评触发条件** ——
            // 微信可能又改了混淆方式。截屏 OCR 兜底是 P5 第三片
            noteDiagnosis(CopilotState.EMPTY_TREE, 0)
            return
        }

        // 换到另一个放行的 App 要把指纹清掉:两个 App 最后几条恰好一样时
        // (比如同一个人在微信和 QQ 上发了同一句话),不清的那一边会被当成
        // "没变化"而整个吞掉
        if (pkg != activePkg) {
            activePkg = pkg
            lastSignature = ""
        }

        val signature = snapshot.signature()
        if (signature == lastSignature) return
        lastSignature = signature
        // **只记条数和谁说的,不记一个字。** 这行日志会进 logcat,
        // 而 logcat 是这台手机上最不受控的一个地方
        Log.d(
            TAG,
            "读到 $pkg:${snapshot.messages.size} 条," +
                snapshot.messages.takeLast(6).joinToString("|") { "${it.side}:${it.text.length}" },
        )
        noteDiagnosis(CopilotState.OK, snapshot.messages.size)

        // **自动触发只在最后一条是对方说的时候。** 自己刚发完一句话,
        // 没有什么需要回的 —— 那时候弹出三条候选只会挡住屏幕
        if (snapshot.latestFrom != ChatShaping.SIDE_OTHER || !prefs.autoAnalyze) return

        pendingSnapshot = snapshot
        // 一条消息到达会连着触发好几个 content-changed 事件(气泡动画、
        // 已读状态、时间戳)。不去抖的话一条消息会打三次模型
        main.removeCallbacks(debounce)
        main.postDelayed(debounce, DEBOUNCE_MS)
    }

    /**
     * 把占位标题换成这个 App 上一个稳定的标题,顺便把当前这个记下来。
     *
     * 一个都没有就原样返回 —— **不猜**。服务端拿不到标题只是查不到记忆
     * (陌生人也能用副驾),而拿到一个错标题会检索出别人的事实。
     */
    private fun stabilizeTitle(pkg: String, snapshot: ChatSnapshot): ChatSnapshot {
        if (ChatShaping.isTransientTitle(snapshot.title)) {
            val good = lastGoodTitle[pkg] ?: return snapshot
            return snapshot.copy(title = good)
        }
        snapshot.title?.let { lastGoodTitle[pkg] = it }
        return snapshot
    }

    /**
     * 这一屏读完了,该去分析了。
     *
     * **目前只落状态。** 悬浮窗和 `POST /app/copilot/analyze` 是 P5 的第二片,
     * 本地历史和 OCR 兜底是第三片 —— 它们都挂在这个方法上。
     * 先把读屏这一段跑通再往上接,是因为读屏是唯一一段**只能在真机上验**的:
     * 节点 id 对不对、谁说的判得准不准,单元测试答不了。
     */
    private fun deliver(snapshot: ChatSnapshot) {
        Log.i(TAG, "待分析:${snapshot.messages.size} 条,最后一条来自 ${snapshot.latestFrom}")
    }

    private fun noteDiagnosis(diagnosis: String, messageCount: Int) {
        if (diagnosis == lastDiagnosis && messageCount == lastDiagnosisCount) return
        lastDiagnosis = diagnosis
        lastDiagnosisCount = messageCount
        CopilotState.recordCapture(this, diagnosis, messageCount, System.currentTimeMillis())
    }

    /** 上一次写进 [CopilotState] 的诊断。**一样的就不再写** —— 那是一次磁盘写,
     *  而这个方法挂在每秒好几次的事件回调上。 */
    private var lastDiagnosis: String? = null
    private var lastDiagnosisCount = -1

    override fun onInterrupt() {}

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacks(debounce)
        // 服务没了就是读不到了。**默认 false 那一条在这里闭环**:
        // 状态页显示的"副驾在跑"不能是一个从来没被翻回去的 true
        CopilotState.setServiceConnected(this, false)
    }

    private companion object {
        const val TAG = "LifeIn/copilot"

        /** 去抖窗口。太短会为一条消息打三次模型,太长会让用户觉得它没反应。 */
        const val DEBOUNCE_MS = 800L
    }
}
