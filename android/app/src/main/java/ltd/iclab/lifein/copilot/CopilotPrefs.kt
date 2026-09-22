package ltd.iclab.lifein.copilot

import android.content.Context

/**
 * 副驾在手机上的开关 —— **四道门里的后两道**(07 §2.9)。
 *
 * 第一道是服务端的 `COPILOT_ENABLED`,第二、三道是系统权限(无障碍、悬浮窗),
 * 那两道不归这里管,要用户自己去系统设置里开。这里管第四道:
 * **哪些聊天 App 放行。**
 *
 * ## 默认拒绝,和采集白名单同一个思路
 *
 * [allowedApps] 默认是**空集合,意思是一个都不放行** —— 不是"全放行"。
 * 这一条要写在这里而不是靠调用方记得:`Whitelist` 那边曾经因为一个
 * 硬编码的闸门,让配好的规则在手机上被静默丢掉(ADR-032),
 * 而那种错的表现是"看起来配好了,一条都收不到"。
 *
 * 反过来,**空集合当成全放行**的错更糟:装上 App、开了无障碍权限,
 * 副驾就会开始读每一个聊天窗,而用户以为自己还没打开它。
 *
 * ## 这里不存任何凭据
 *
 * 和 JARVIS 不一样:模型调用在服务端,手机上没有模型密钥。
 * 设备凭据在 Keystore 里([ltd.iclab.lifein.data.Secrets]),不在这个文件。
 */
class CopilotPrefs(context: Context) {

    private val sp = context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    /**
     * 副驾总开关。**默认关。**
     *
     * 关掉之后悬浮窗不出来、不读屏、不发请求。无障碍权限可以留着 ——
     * 那是系统层面的事,用户想彻底断开随时可以去收回(09 §5)。
     */
    var enabled: Boolean
        get() = sp.getBoolean(K_ENABLED, false)
        set(v) = sp.edit().putBoolean(K_ENABLED, v).apply()

    /**
     * 放行的聊天 App,存包名。**空 = 一个都不放行。**
     *
     * 见类注释:这一条是默认拒绝,不是默认放行。
     */
    var allowedApps: Set<String>
        get() = sp.getStringSet(K_ALLOWED_APPS, emptySet()) ?: emptySet()
        set(v) = sp.edit().putStringSet(K_ALLOWED_APPS, v.toSet()).apply()

    /**
     * 对方发来新消息时自动分析。关掉的话悬浮窗只是待命,要用户点一下才分析。
     *
     * **默认开**,因为"一键填入"的价值全在那个"一键" —— 打开聊天窗还要先点一下
     * 才开始想,等于把两步变成三步。关掉它的理由是省钱(每次分析打三次模型)。
     */
    var autoAnalyze: Boolean
        get() = sp.getBoolean(K_AUTO, true)
        set(v) = sp.edit().putBoolean(K_AUTO, v).apply()

    /**
     * 树里读不到正文时,截屏 + 本机离线 OCR 兜底。
     *
     * **只 OCR 气泡矩形,不整屏识别**(05 R3 的缓解措施)。整屏那条只在用户
     * 手动点"截屏识别一次"时跑,而那是另一个入口,不受这个开关控制。
     *
     * 图片不出手机 —— ML Kit 是打包版,离线跑(ADR-021 的 2026-09 补充)。
     */
    var ocrFallback: Boolean
        get() = sp.getBoolean(K_OCR_FALLBACK, true)
        set(v) = sp.edit().putBoolean(K_OCR_FALLBACK, v).apply()

    /**
     * 把对话记在手机本地,分析时一起带上(ADR-038)。
     *
     * **默认开,但只写本地。** 服务端一行都不存 —— 它读记忆,不写记忆。
     * 关掉之后每次分析只有屏幕上那几条,没有长上下文。
     */
    var keepHistory: Boolean
        get() = sp.getBoolean(K_KEEP_HISTORY, true)
        set(v) = sp.edit().putBoolean(K_KEEP_HISTORY, v).apply()

    /** 悬浮球记住的位置(px)。-1 = 还没拖过,用默认位置。 */
    var bubbleX: Int
        get() = sp.getInt(K_BUBBLE_X, -1)
        set(v) = sp.edit().putInt(K_BUBBLE_X, v).apply()

    var bubbleY: Int
        get() = sp.getInt(K_BUBBLE_Y, -1)
        set(v) = sp.edit().putInt(K_BUBBLE_Y, v).apply()

    /** 这个 App 放行了吗。**总开关关着的时候一律不放行。** */
    fun allows(pkg: String?): Boolean =
        enabled && !pkg.isNullOrBlank() && pkg in allowedApps

    fun setAppAllowed(pkg: String, allowed: Boolean) {
        allowedApps = if (allowed) allowedApps + pkg else allowedApps - pkg
    }

    private companion object {
        const val FILE = "lifein.copilot"
        const val K_ENABLED = "enabled"
        const val K_ALLOWED_APPS = "allowed_apps"
        const val K_AUTO = "auto_analyze"
        const val K_OCR_FALLBACK = "ocr_fallback"
        const val K_KEEP_HISTORY = "keep_history"
        const val K_BUBBLE_X = "bubble_x"
        const val K_BUBBLE_Y = "bubble_y"
    }
}
