package ltd.iclab.lifein.copilot

import android.accessibilityservice.AccessibilityService
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Display
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 截一张当前窗口的图,走无障碍服务自己的截屏能力。
 *
 * **不用 MediaProjection**,那是 ADR-021 的 2026-09-22 追加里把 minSdk 抬到 30
 * 的理由:`MediaProjection` 每次都要弹一次系统授权框,而副驾是"打开聊天窗
 * 就该有反应"的东西 —— 每次点确认等于把这个功能废掉。
 *
 * 要 `android:canTakeScreenshot="true"`,而**那个标志要用户把无障碍服务
 * 关掉再打开才生效**。升级 App 之后第一次不好使,原因多半就是这个
 * (04 的 ADR-021 附注里记着)。
 *
 * ## 三件被平台逼出来的事
 *
 * **拿到的是 `HardwareBuffer`,要立刻拷成软件位图再关掉它。**
 * 漏掉几个就会把系统合成器饿死,表现是整个手机开始掉帧 ——
 * 而那时候没人会想到是这个 App。
 *
 * **系统自己会限流(错误码 3)。** 所以这里先自己限:两次之间至少一秒,
 * 连续失败时退避 1s→2s→4s…封到 30 秒。少了这一层,一个"树永远读不到字"的
 * 聊天 App 会变成一台截屏机枪 —— 每个 content-changed 事件截一张。
 *
 * **我们自己的悬浮窗也在屏幕上**,不先藏起来会被拍进图里,
 * 然后被 OCR 认成聊天内容。
 *
 * ## 图片不出手机
 *
 * 截下来的位图只进本机的 ML Kit(打包版,离线),识别完就 recycle。
 * **不上传、不落盘**(09 §3)。
 */
class CopilotScreenshot(
    private val service: AccessibilityService,
    private val hideOverlay: () -> Unit = {},
    private val restoreOverlay: () -> Unit = {},
) {

    sealed interface Result {
        /**
         * [scaleX]/[scaleY] 是"位图尺寸 ÷ 实际拍到的区域尺寸",
         * [originX]/[originY] 是那块区域在屏幕上的起点。
         *
         * **窗口截图不等于整屏截图**:分屏时、或者窗口不含状态栏时,
         * 图既比屏幕小又有偏移。所以节点矩形要这样映射过去:
         * `位图X = (屏幕X - originX) × scaleX`。整屏截图时 origin 是 (0,0)。
         */
        data class Ok(
            val bitmap: Bitmap,
            val scaleX: Float,
            val scaleY: Float,
            val originX: Int,
            val originY: Int,
        ) : Result

        data class Failed(val code: Int, val message: String) : Result
    }

    private val main = Handler(Looper.getMainLooper())

    /** 截一张。[onResult] **在主线程上、而且只会被调一次**。 */
    fun capture(onResult: (Result) -> Unit) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastAttemptAt < requiredInterval()) {
            onResult(Result.Failed(CODE_THROTTLED, "截屏太频繁"))
            return
        }
        lastAttemptAt = now

        val done = AtomicBoolean(false)
        val finish: (Result) -> Unit = { result ->
            if (done.compareAndSet(false, true)) {
                restoreOverlay()
                failStreak = if (result is Result.Ok) 0 else (failStreak + 1).coerceAtMost(MAX_STREAK)
                onResult(result)
            }
        }

        // 先把悬浮窗藏起来,给合成器一帧的时间把它撤掉,再按快门
        runCatching { hideOverlay() }
        main.postDelayed({ shoot(finish, done) }, HIDE_SETTLE_MS)
    }

    private fun shoot(finish: (Result) -> Unit, done: AtomicBoolean) {
        val executor = service.mainExecutor
        var windowBounds: Rect? = null

        // **系统有可能根本不回调**(拍到一个受保护窗口、或者正在转场时见过)。
        // 没有这条看门狗的话,悬浮窗会一直藏着、忙标志一直不落,
        // 直到服务被销毁为止 —— 而用户看到的是"副驾不见了"
        val watchdog = Runnable { finish(Result.Failed(CODE_TIMEOUT, "截屏超时")) }
        val callback = object : AccessibilityService.TakeScreenshotCallback {
            override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                main.removeCallbacks(watchdog)
                if (done.get()) {
                    // 已经超时判负了。这张图作废,但**缓冲区必须关** ——
                    // 不关就是在漏,而漏的代价是整个系统掉帧
                    runCatching { result.hardwareBuffer.close() }
                    return
                }
                finish(toBitmap(result, windowBounds))
            }

            override fun onFailure(errorCode: Int) {
                main.removeCallbacks(watchdog)
                finish(Result.Failed(errorCode, humanMessage(errorCode)))
            }
        }

        // API 34 起可以只拍当前窗口:更省,而且有些 ROM 拒绝整屏截图却允许窗口截图
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val node = runCatching { service.rootInActiveWindow }.getOrNull()
            val windowId = node?.windowId
            if (windowId != null && windowId != -1) {
                windowBounds = runCatching {
                    Rect().also { node.window?.getBoundsInScreen(it) }
                }.getOrNull()?.takeIf { it.width() > 0 && it.height() > 0 }
                try {
                    service.takeScreenshotOfWindow(windowId, executor, callback)
                    main.postDelayed(watchdog, TIMEOUT_MS)
                    return
                } catch (e: Throwable) {
                    windowBounds = null
                    Log.w(TAG, "窗口截图不可用,退回整屏:${e::class.simpleName}")
                }
            }
        }
        try {
            service.takeScreenshot(Display.DEFAULT_DISPLAY, executor, callback)
            main.postDelayed(watchdog, TIMEOUT_MS)
        } catch (e: Throwable) {
            finish(Result.Failed(CODE_INTERNAL, "截屏失败:${e::class.simpleName}"))
        }
    }

    /** `HardwareBuffer` → 软件位图。**无论走哪条路,缓冲区都要关。** */
    private fun toBitmap(result: AccessibilityService.ScreenshotResult, window: Rect?): Result {
        val buffer = result.hardwareBuffer
        return try {
            val hardware = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace)
            val copy = hardware?.copy(Bitmap.Config.ARGB_8888, false)
            runCatching { hardware?.recycle() }
            if (copy == null) {
                Result.Failed(CODE_INTERNAL, "截屏失败:拿到的画面读不出来")
            } else {
                val metrics = service.resources.displayMetrics
                val width = window?.width() ?: metrics.widthPixels
                val height = window?.height() ?: metrics.heightPixels
                Result.Ok(
                    bitmap = copy,
                    scaleX = if (width > 0) copy.width / width.toFloat() else 1f,
                    scaleY = if (height > 0) copy.height / height.toFloat() else 1f,
                    originX = window?.left ?: 0,
                    originY = window?.top ?: 0,
                )
            }
        } catch (e: Throwable) {
            Result.Failed(CODE_INTERNAL, "截屏失败:${e::class.simpleName}")
        } finally {
            runCatching { buffer.close() }
        }
    }

    companion object {
        private const val TAG = "LifeIn/copilot"

        /** 我们自己的限流,不是平台的码。 */
        const val CODE_THROTTLED = -1

        /** 我们自己的看门狗:平台压根没回调。 */
        const val CODE_TIMEOUT = -2

        /** 平台的"间隔太短"。和 [CODE_THROTTLED] 一样属于时机问题,不值得打扰用户。 */
        const val CODE_TOO_SOON = 3

        private const val CODE_INTERNAL = 1

        private const val MIN_INTERVAL_MS = 1000L
        private const val MAX_BACKOFF_MS = 30_000L
        private const val MAX_STREAK = 6
        private const val HIDE_SETTLE_MS = 120L
        private const val TIMEOUT_MS = 3000L

        // **故意是进程级的**:系统那个限流是按服务算的,
        // 而调用方可能每次新建一个 CopilotScreenshot
        @Volatile
        private var lastAttemptAt = 0L

        @Volatile
        private var failStreak = 0

        /** 平时一秒;连续失败时 1s、2s、4s…封到 30 秒。 */
        private fun requiredInterval(): Long {
            if (failStreak <= 0) return MIN_INTERVAL_MS
            val shifted = MIN_INTERVAL_MS shl (failStreak - 1).coerceAtMost(MAX_STREAK)
            return shifted.coerceAtMost(MAX_BACKOFF_MS)
        }

        /** 这个码值不值得打扰用户。时机问题会一直出现,说了等于噪音。 */
        fun isTransient(code: Int): Boolean = code == CODE_THROTTLED || code == CODE_TOO_SOON

        /** 平台错误码,翻成用户能照着做点什么的话。 */
        fun humanMessage(code: Int): String = when (code) {
            CODE_THROTTLED -> "截屏太频繁"
            CODE_TIMEOUT -> "截屏超时"
            1 -> "截屏失败:系统拒绝了这次请求"
            2 -> "截屏失败:去系统设置里把 LifeIn 的无障碍关掉再打开一次"
            CODE_TOO_SOON -> "截屏失败:间隔太短,等一秒再试"
            4 -> "截屏失败:没有有效的显示"
            6 -> "截屏失败:这个界面禁止截屏,拿不到画面"
            else -> "截屏失败(码 $code)"
        }
    }
}
