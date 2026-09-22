package ltd.iclab.lifein.copilot

import android.accessibilityservice.AccessibilityService
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.net.CopilotAnalyzeBody
import ltd.iclab.lifein.net.CopilotMsgBody
import ltd.iclab.lifein.net.LifeInApi

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

    /** 去抖窗口里等着被分析的那一份。 */
    private var pendingSnapshot: ChatSnapshot? = null

    /** 屏幕上现在是什么。自动触发没开时,点悬浮球分析的就是它。 */
    private var currentSnapshot: ChatSnapshot? = null

    private val debounce = Runnable { pendingSnapshot?.let { analyze(it) } }

    /**
     * 网络和填入都跑在这上面。**单线程,不是线程池** ——
     * 两次分析并发跑没有意义(后一次的结果会盖掉前一次),
     * 而排队还顺带给了一层"用户连点也只打一次模型"的保护。
     */
    private val worker = Executors.newSingleThreadExecutor()

    private var overlay: CopilotOverlay? = null
    private val filler by lazy { CopilotFill(this) }

    /** 正在分析。连点悬浮球不该变成连打三次模型。 */
    @Volatile
    private var analyzing = false

    override fun onServiceConnected() {
        super.onServiceConnected()
        CopilotState.setServiceConnected(this, true)
        overlay = CopilotOverlay(this, prefs).apply {
            onTapAnalyze = {
                // 手动点:有快照就分析,没有就把"为什么没有"当场说出来。
                // **这是架构 §8.7 那条"悬浮窗当场说明原因"的落点**
                val snapshot = pendingSnapshot ?: currentSnapshot
                if (snapshot == null) {
                    showMessage(CopilotWording.diagnosis(lastDiagnosis))
                } else {
                    analyze(snapshot)
                }
            }
            onFill = { text -> fill(text) }
        }
        // 无障碍服务被 ROM 冻结之后不会自己回来(ADR-021 的 2026-09-22 追加)。
        // 起不来不算错 —— 只是少了一层保护,不该让整个服务连不上
        if (prefs.enabled) runCatching { CopilotKeepAlive.start(this) }
        Log.i(TAG, "副驾读屏服务已连接,放行 ${prefs.allowedApps.size} 个 App")
    }

    private fun submit(task: () -> Unit) {
        // 服务已经被拆掉之后,一个迟到的悬浮窗回调不该把进程带崩
        runCatching { worker.execute(task) }
            .onFailure { if (it !is RejectedExecutionException) throw it }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        if (!prefs.enabled) {
            // 用户在 App 里把副驾关掉了,而这个服务还绑着。
            // 悬浮窗要跟着收走,否则它会一直挂在那里说自己在工作
            leaveChat()
            return
        }
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
        // 悬浮窗权限没给的时候一个字都不读。读到了也没地方显示,
        // 而那意味着一次白花钱的模型调用
        if (overlay?.hasPermission() != true) return

        val root = rootInActiveWindow ?: return
        val pkg = root.packageName?.toString() ?: return

        val adapter = adapters[pkg]
        if (adapter == null) {
            // 没适配器不是"坏了",只是没写。**这两件事要显示成两句不同的话**
            // (架构 §8.7),所以诊断码也必须是两个
            noteDiagnosis(CopilotState.NO_ADAPTER, 0)
            leaveChat()
            return
        }
        if (!prefs.allows(pkg)) {
            noteDiagnosis(CopilotState.NOT_ALLOWED, 0)
            leaveChat()
            return
        }

        // 不在聊天窗(会话列表、朋友圈、设置页)→ 收起悬浮窗,但**不记状态**:
        // 这一条每秒会走很多次,记下来只会把真正有用的那条诊断冲掉
        val raw = adapter.extract(root, resources)
        if (raw == null) {
            leaveChat()
            return
        }
        val snapshot = stabilizeTitle(pkg, raw)

        if (snapshot.messages.isEmpty()) {
            // 在聊天窗,但树里一句正文都没有。**这是 ADR-035 的重评触发条件** ——
            // 微信可能又改了混淆方式。截屏 OCR 兜底是 P5 第三片。
            // 悬浮球留着:点它能看到那句"多半是这个 App 改了防护",
            // 而那句话是这个触发条件唯一会被人看见的地方
            noteDiagnosis(CopilotState.EMPTY_TREE, 0)
            currentSnapshot = null
            pendingSnapshot = null
            main.post { overlay?.showIdle(null) }
            return
        }

        // 换到另一个放行的 App 要把指纹清掉:两个 App 最后几条恰好一样时
        // (比如同一个人在微信和 QQ 上发了同一句话),不清的那一边会被当成
        // "没变化"而整个吞掉
        if (pkg != activePkg) {
            activePkg = pkg
            lastSignature = ""
        }

        currentSnapshot = snapshot
        val signature = snapshot.signature()
        if (signature == lastSignature) {
            // 内容没变,但悬浮窗可能被 ROM 干掉了(或者切走又切回来)。
            // **只把球放回去,不重新分析** —— 那是一次花钱的调用
            if (overlay?.isShowing() != true) main.post { overlay?.showIdle(snapshot.title) }
            return
        }
        lastSignature = signature
        // **只记条数和谁说的,不记一个字。** 这行日志会进 logcat,
        // 而 logcat 是这台手机上最不受控的一个地方
        Log.d(
            TAG,
            "读到 $pkg:${snapshot.messages.size} 条," +
                snapshot.messages.takeLast(6).joinToString("|") { "${it.side}:${it.text.length}" },
        )
        noteDiagnosis(CopilotState.OK, snapshot.messages.size)

        // 换了一段对话,先把上一段的判断和候选清掉 ——
        // 留着的话,用户会在新对话上看到一条为旧对话起草的句子,
        // 而那条句子看起来完全正常
        main.post { overlay?.collapse() }

        // **自动触发只在最后一条是对方说的时候。** 自己刚发完一句话,
        // 没有什么需要回的 —— 那时候弹出三条候选只会挡住屏幕
        if (snapshot.latestFrom != ChatShaping.SIDE_OTHER || !prefs.autoAnalyze) {
            main.post { overlay?.showIdle(snapshot.title) }
            return
        }

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

    /** 离开聊天窗:把悬浮窗收走,并且忘掉手上这份快照。
     *
     *  **忘掉这一步不能省。** 留着的话,用户在别处点一下悬浮球,
     *  会拿到一份为上一个聊天窗起草的候选 —— 而那三条句子看起来完全正常。 */
    private fun leaveChat() {
        currentSnapshot = null
        pendingSnapshot = null
        main.post { overlay?.hide() }
    }

    /**
     * 拿这一屏去换判断和三条候选。
     *
     * **一次请求在服务端要打三次模型**,所以这里有两道闸:[analyzing] 挡连点,
     * 上面的去抖挡一条消息引发的连串事件。漏掉任何一道的表现都是账单上看得见、
     * 代码里看不见。
     */
    private fun analyze(snapshot: ChatSnapshot) {
        if (analyzing) return
        analyzing = true
        val app = adapters[activePkg]?.serverApp
        if (app == null) {
            analyzing = false
            return
        }
        overlay?.showLoading()
        submit {
            val message = runCatching {
                val enrollment = LifeInApp.instance.secrets.load()
                    ?: return@runCatching "还没配码。先在 LifeIn 里扫一次配置二维码"
                val result = LifeInApi(enrollment).copilotAnalyze(
                    CopilotAnalyzeBody(
                        deviceId = enrollment.deviceId,
                        app = app,
                        title = snapshot.title.orEmpty(),
                        messages = snapshot.messages.map { CopilotMsgBody(it.side, it.text) },
                        captureNote = snapshot.note.orEmpty(),
                    )
                )
                main.post { overlay?.showResult(result) }
                null
            }.getOrElse { error ->
                // 状态码不能糊成一句"失败":404 是服务端没开副驾,
                // 401 是凭据被吊销了,而这两个再点一百次也不会变
                val code = (error as? LifeInApi.HttpError)?.code
                Log.w(TAG, "副驾分析失败:code=$code ${error::class.simpleName}")
                CopilotWording.failure(code)
            }
            analyzing = false
            if (message != null) main.post { overlay?.showMessage(message) }
        }
    }

    /**
     * 把一条候选填进输入框。**动节点的活全在 [CopilotFill] 里**,
     * 这里只负责挪到工作线程、把结果说给用户听。
     *
     * 填完不发。这一条不在这个方法里,在这个功能的定义里(01 §8)。
     */
    private fun fill(text: String) {
        submit {
            val outcome = runCatching { filler.fill(text) }
                .getOrElse { CopilotFill.Outcome.Copied(it::class.simpleName ?: "填不进去") }
            main.post {
                when (outcome) {
                    is CopilotFill.Outcome.Filled -> overlay?.toast("已填入,确认之后自己发送")
                    is CopilotFill.Outcome.Copied ->
                        overlay?.toast("${outcome.why},已复制,长按输入框粘贴")
                }
            }
        }
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
        // 先切回调再拆窗口:一个迟到的按钮点击不该回到一个已经死掉的实例上,
        // 而那个实例手里还攥着 rootInActiveWindow
        overlay?.onTapAnalyze = null
        overlay?.onFill = null
        overlay?.hide()
        overlay = null
        worker.shutdownNow()
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
