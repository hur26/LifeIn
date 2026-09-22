package ltd.iclab.lifein.copilot

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import ltd.iclab.lifein.net.CopilotAnalyzeResult
import ltd.iclab.lifein.net.CopilotCandidate
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * 副驾的展示面 —— **一个悬浮窗**([ADR-036](docs/04-tech-decisions.md))。
 *
 * ## 为什么是悬浮窗
 *
 * 被否决的几个里最可惜的是**输入法**:体验最好,填入是它的本职。
 * 但一个输入法会看见你在**每一个 App** 里敲的**每一个键** ——
 * 而副驾只该看见你主动打开的那个聊天窗。那不是一个能靠自觉守住的边界,
 * 所以 09-privacy 那一页容不下它。
 *
 * App 内页面也不行:副驾的全部价值在于"不用切出去"。
 *
 * ## 两条硬约束
 *
 * **面板最高只占屏幕的四成。** 高过这个数就把聊天挡住了,而副驾的
 * 前提是你还看着那段对话 —— 挡住它之后三条候选就没有参照了。
 *
 * **窗口不抢焦点**(`FLAG_NOT_FOCUSABLE`)。抢了焦点输入法会收起来,
 * 而收起来之后 `ACTION_SET_TEXT` 那条快路就走不通了,每次都要退到剪贴板。
 *
 * ## 这里没有"发送"
 *
 * 每条候选下面只有一个按钮:**填入**。这不是还没做,是 01 §8 那条定义 ——
 * **发送键永远是人按的。**
 *
 * 填不进去的时候 [CopilotFill] 会自动退到剪贴板,所以不需要再摆一个"复制":
 * 多一个按钮就多一个位置,而那个位置正好在发送键旁边。
 *
 * 填完之后面板**自动收起**。那是 ADR-037 第三条防线:候选进了输入框之后,
 * 把它从一个"待点的按钮"变回一段"要读的文字" —— 注入最怕的就是人真的读一眼。
 */
class CopilotOverlay(
    private val service: AccessibilityService,
    private val prefs: CopilotPrefs,
) {

    /** 点悬浮球:手动要一次分析。 */
    var onTapAnalyze: (() -> Unit)? = null

    /** 点"填入"。**这个回调走到的地方是 [CopilotFill],那是唯一能动节点的地方。** */
    var onFill: ((String) -> Unit)? = null

    private val windowManager =
        service.getSystemService(Context.WINDOW_SERVICE) as WindowManager

    private var root: LinearLayout? = null
    private var panel: LinearLayout? = null
    private var body: LinearLayout? = null
    private var bubble: TextView? = null

    /**
     * 当前会话的标题,显示在面板脚注里。
     *
     * 看起来多余(用户正看着那个聊天窗),但它是**适配器有没有挑对标题**的
     * 唯一凭据 —— 而那个标题是服务端查记忆的 key。挑错的表现原本是
     * "候选里提到了另一个人的事",到那一步已经很难往回查了。
     */
    private var title: String? = null

    private val density = service.resources.displayMetrics.density
    private val screenHeight = service.resources.displayMetrics.heightPixels

    private fun dp(value: Int): Int = (value * density).roundToInt()

    fun isShowing(): Boolean = root != null

    /**
     * 悬浮窗权限给没给。**没给的时候一个字都不该读** ——
     * 读到了也没地方显示,而那意味着一次白花钱的模型调用。
     */
    fun hasPermission(): Boolean = Settings.canDrawOverlays(service)

    // ---------- 四种状态 ----------

    /** 待命:只有悬浮球,面板收着。 */
    fun showIdle(title: String?) {
        ensureAttached()
        this.title = title
        collapse()
        bubble?.text = "副"
        paintBubble(Color.parseColor(BRAND))
    }

    fun showLoading() {
        ensureAttached()
        paintBubble(Color.parseColor(BRAND))
        expand()
        body?.let { container ->
            container.removeAllViews()
            container.addView(label("在想了…", size = 14f, dim = true))
        }
    }

    /**
     * 读不到、或者出错。**原因要当场说出来。**
     *
     * 这一条是架构 §8.7 的落点:采集器坏了靠服务端心跳在一小时内告警,
     * 副驾坏了**用户正看着聊天窗等结果** —— 十秒就算坏,而发现它的人是他自己。
     * 面板上什么都不写的话,他会以为是网慢。
     */
    fun showMessage(text: String) {
        ensureAttached()
        paintBubble(Color.parseColor(DIM))
        expand()
        body?.let { container ->
            container.removeAllViews()
            container.addView(label(text, size = 14f))
        }
    }

    fun showResult(result: CopilotAnalyzeResult) {
        ensureAttached()
        val judgement = result.judgement
        paintBubble(heatColor(judgement.dangerLevel))
        expand()

        val container = body ?: return
        container.removeAllViews()

        container.addView(
            chipRow(
                CopilotWording.heatLabel(judgement.dangerLevel) to heatColor(judgement.dangerLevel),
                CopilotWording.intent(judgement.trueIntent) to Color.parseColor(DIM),
            )
        )
        container.addView(
            label(
                CopilotWording.summary(
                    judgement.needs,
                    judgement.bestAction,
                    judgement.literal,
                ),
                size = 15f,
                bold = true,
            )
        )
        CopilotWording.replyTiming(judgement.shouldReplyNow)?.let {
            container.addView(label(it, size = 13f, dim = true))
        }

        result.candidates.sortedBy { it.rank }.forEach { container.addView(candidateCard(it)) }
        if (result.candidates.isEmpty()) {
            container.addView(label("这次没给出候选", size = 14f, dim = true))
        }

        // 脚注:降级、采集方式、用上了多少背景。**每一条都要能被分辨**
        listOfNotNull(
            CopilotWording.degradedNote(result.degraded),
            CopilotWording.captureNote(result.captureNote),
            if (result.dropped > 0) "有 ${result.dropped} 条读坏了,已跳过" else null,
            title?.let { "会话:$it" },
            CopilotWording.contextNote(result.context.factsUsed, result.context.historyUsed),
        ).forEach { container.addView(label(it, size = 12f, dim = true)) }
    }

    fun toast(text: String) {
        Toast.makeText(service, text, Toast.LENGTH_SHORT).show()
    }

    fun hide() {
        root?.let { runCatching { windowManager.removeView(it) } }
        root = null
        panel = null
        body = null
        bubble = null
    }

    fun collapse() {
        panel?.visibility = View.GONE
    }

    private fun expand() {
        panel?.visibility = View.VISIBLE
    }

    // ---------- 搭窗口 ----------

    private fun ensureAttached() {
        if (root != null) return

        val layout = LinearLayout(service).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.END
        }

        val content = LinearLayout(service).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = rounded(Color.parseColor(SURFACE), dp(18))
            elevation = dp(6).toFloat()
        }
        body = content

        // 面板高度封到屏幕的四成(ADR-036)。超出的部分滚动 ——
        // 不是省略:三条候选里最长的那条也要能被读完再发,
        // 而"读完再发"正是 R3 那条防线本身
        val scroller = CappedScrollView(service, (screenHeight * PANEL_MAX_RATIO).toInt()).apply {
            addView(
                content,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                )
            )
        }

        val panelBox = LinearLayout(service).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            addView(
                scroller,
                LinearLayout.LayoutParams(dp(300), ViewGroup.LayoutParams.WRAP_CONTENT)
            )
        }
        panel = panelBox
        layout.addView(panelBox)

        val ball = TextView(service).apply {
            text = "副"
            setTextColor(Color.WHITE)
            textSize = 15f
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
        }
        bubble = ball
        layout.addView(
            ball,
            LinearLayout.LayoutParams(dp(46), dp(46)).apply {
                topMargin = dp(8)
                gravity = Gravity.END
            }
        )
        paintBubble(Color.parseColor(BRAND))

        val windowParams = WindowManager.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            // **不抢焦点。** 抢了焦点输入法会收起来,而收起来之后
            // ACTION_SET_TEXT 那条快路就走不通,每次都得退到剪贴板
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            android.graphics.PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = if (prefs.bubbleX >= 0) prefs.bubbleX else dp(280)
            y = if (prefs.bubbleY >= 0) prefs.bubbleY else screenHeight / 3
        }
        ball.setOnTouchListener(DragOrTap(windowParams, layout))

        runCatching { windowManager.addView(layout, windowParams) }
            .onSuccess { root = layout }
    }

    /**
     * 悬浮球的手势:拖动要能拖走,点一下要能触发分析。
     *
     * 区分两者靠位移阈值。**阈值不能太小**:手指按在一个 46dp 的球上一定会
     * 抖几个像素,判成拖动的话点不动它;也不能太大,否则轻轻挪一下就
     * 触发一次分析,而那是一次花钱的模型调用。
     */
    private inner class DragOrTap(
        private val windowParams: WindowManager.LayoutParams,
        private val view: View,
    ) : View.OnTouchListener {
        private var startX = 0
        private var startY = 0
        private var touchX = 0f
        private var touchY = 0f
        private var moved = false

        override fun onTouch(v: View, event: MotionEvent): Boolean {
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    startX = windowParams.x
                    startY = windowParams.y
                    touchX = event.rawX
                    touchY = event.rawY
                    moved = false
                }

                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - touchX).toInt()
                    val dy = (event.rawY - touchY).toInt()
                    if (abs(dx) > dp(TAP_SLOP_DP) || abs(dy) > dp(TAP_SLOP_DP)) moved = true
                    if (moved) {
                        windowParams.x = startX + dx
                        windowParams.y = startY + dy
                        runCatching { windowManager.updateViewLayout(view, windowParams) }
                    }
                }

                MotionEvent.ACTION_UP -> {
                    if (moved) {
                        prefs.bubbleX = windowParams.x
                        prefs.bubbleY = windowParams.y
                    } else if (panel?.visibility == View.VISIBLE) {
                        collapse()
                    } else {
                        onTapAnalyze?.invoke()
                    }
                }
            }
            return true
        }
    }

    // ---------- 零件 ----------

    private fun candidateCard(candidate: CopilotCandidate): View =
        LinearLayout(service).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), dp(8), dp(10), dp(8))
            background = rounded(Color.parseColor(SURFACE_ALT), dp(12))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(8) }

            addView(label(candidate.text, size = 15f))
            addView(shareBar(candidate.share))
            addView(
                LinearLayout(service).apply {
                    orientation = LinearLayout.HORIZONTAL
                    // **只有这两个按钮。** 这里不存在第三个
                    addView(
                        button("填入") {
                            onFill?.invoke(candidate.text)
                            // ADR-037 第三条防线:填完把面板收起来,
                            // 让候选从"待点的按钮"变回"要读的文字"
                            collapse()
                        }
                    )
                }
            )
        }

    /** 排序给出的占比。**排序失败时服务端会把 degraded 置成 rank**,脚注会说。 */
    private fun shareBar(share: Double): View {
        val clamped = share.coerceIn(0.0, 1.0).toFloat()
        return LinearLayout(service).apply {
            orientation = LinearLayout.HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(3)
            ).apply { topMargin = dp(6); bottomMargin = dp(4) }
            addView(
                View(service).apply { background = rounded(Color.parseColor(BRAND), dp(2)) },
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, clamped)
            )
            addView(
                View(service),
                LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f - clamped)
            )
        }
    }

    private fun button(text: String, onClick: () -> Unit): View =
        TextView(service).apply {
            this.text = text
            textSize = 13f
            setTextColor(Color.WHITE)
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
            setPadding(dp(16), dp(7), dp(16), dp(7))
            background = rounded(Color.parseColor(BRAND), dp(14))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = dp(4) }
            setOnClickListener { onClick() }
        }

    private fun label(
        text: String,
        size: Float,
        dim: Boolean = false,
        bold: Boolean = false,
    ): View = TextView(service).apply {
        this.text = text
        textSize = size
        setTextColor(Color.parseColor(if (dim) DIM else INK))
        if (bold) typeface = Typeface.DEFAULT_BOLD
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(4) }
    }

    private fun chipRow(vararg chips: Pair<String, Int>): View =
        LinearLayout(service).apply {
            orientation = LinearLayout.HORIZONTAL
            chips.forEach { (text, color) ->
                addView(
                    TextView(service).apply {
                        this.text = text
                        textSize = 11f
                        setTextColor(Color.WHITE)
                        setPadding(dp(8), dp(3), dp(8), dp(3))
                        background = rounded(color, dp(9))
                        layoutParams = LinearLayout.LayoutParams(
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                            ViewGroup.LayoutParams.WRAP_CONTENT,
                        ).apply { rightMargin = dp(6) }
                    }
                )
            }
        }

    private fun paintBubble(color: Int) {
        bubble?.background = rounded(color, dp(23))
    }

    private fun rounded(color: Int, radius: Int): GradientDrawable =
        GradientDrawable().apply {
            shape = GradientDrawable.RECTANGLE
            cornerRadius = radius.toFloat()
            setColor(color)
        }

    /**
     * 危险度的颜色。
     *
     * 和 App 里那套色板同一个逻辑(`ui/theme/Theme.kt`):**最重要的颜色是红**,
     * 而主色是一个偏冷的绿 —— 它在色环上离红最远,所以并排出现时
     * "这一条不对劲"一眼就看得出来,不用看两眼。
     */
    private fun heatColor(dangerLevel: Int): Int = Color.parseColor(
        when (CopilotWording.heat(dangerLevel)) {
            CopilotWording.Heat.HOT -> DANGER
            CopilotWording.Heat.WARN -> WARN
            CopilotWording.Heat.CALM -> BRAND
        }
    )

    /**
     * 一个有高度上限的 `ScrollView`。
     *
     * `ScrollView` 没有 `maxHeight`,而面板的四成上限(ADR-036)必须是硬的 ——
     * 靠固定高度会让只有一条候选时面板下半截是空的。
     */
    private class CappedScrollView(
        context: Context,
        private val maxHeight: Int,
    ) : ScrollView(context) {
        override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
            super.onMeasure(
                widthMeasureSpec,
                MeasureSpec.makeMeasureSpec(maxHeight, MeasureSpec.AT_MOST),
            )
        }
    }

    private companion object {
        /** 面板最多占屏幕高度的四成(ADR-036)。高过这个数就把对话挡住了。 */
        const val PANEL_MAX_RATIO = 0.40

        /** 判"这是拖不是点"的位移阈值,dp。 */
        const val TAP_SLOP_DP = 8

        // 和 ui/theme/Theme.kt 那套色板同一组值。**有意重复的一份** ——
        // 悬浮窗是纯 View,读不到 Compose 的 ColorScheme(colors.xml 里那三个
        // 小组件颜色是同一个情况)。改主色时三处一起改
        const val BRAND = "#FF2C6B5C"
        const val WARN = "#FFB26A00"
        const val DANGER = "#FFA52C26"
        const val SURFACE = "#FFFFFFFF"
        const val SURFACE_ALT = "#FFEEF1EF"
        const val INK = "#FF191D1B"
        const val DIM = "#FF56635D"
    }
}
