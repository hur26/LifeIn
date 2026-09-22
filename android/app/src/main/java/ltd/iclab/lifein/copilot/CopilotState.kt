package ltd.iclab.lifein.copilot

import android.content.Context
import android.provider.Settings

/**
 * 副驾自己的状态 —— **App 里那一页和悬浮窗的错误提示都读它。**
 *
 * 和 [ltd.iclab.lifein.collect.CollectorState] 同一个角色,但**兜底方式相反**
 * (架构 §8.7):采集器掉线靠服务端心跳在一小时内告警;副驾坏了**用户当场就看见了**
 * —— 他正开着聊天窗等悬浮窗出来。所以这里存的不是"给服务端报的状态",
 * 而是"当场要显示给用户看的那句话"。
 *
 * ## 这里不存任何聊天内容
 *
 * 只存**计数、时间、和一个诊断码**。不存标题、不存原话 ——
 * 标题是联系人名字,而这个文件会一直躺在手机上,
 * 而副驾的对话本该只在 Room 那张有上限、能一键清空的表里(ADR-038)。
 *
 * ## 诊断码为什么必须分这么细
 *
 * 架构 §8.7 那条写得很直白:**"读到了空节点"和"这个 App 还没适配"必须显示成
 * 两句不同的话。** 前者是 ADR-035 的重评触发条件(微信又改了混淆方式,
 * 那时要停下来判断还追不追),后者只是没写适配器。
 * 混成一句"读取失败"等于把那个触发条件藏起来了 —— 而它是这条通道的退出闸门。
 */
object CopilotState {

    /** 一切正常,读到了消息。 */
    const val OK = "ok"

    /** 在聊天窗里,但树里一句正文都没有。**这一条是 ADR-035 的重评触发条件。** */
    const val EMPTY_TREE = "empty_tree"

    /** 前台这个 App 没有适配器。只是没写,不是坏了。 */
    const val NO_ADAPTER = "no_adapter"

    /** 有适配器,但用户没在设置里放行它(默认拒绝)。 */
    const val NOT_ALLOWED = "not_allowed"

    /**
     * 系统设置里有没有勾上这个无障碍服务。**问系统,不问自己。**
     *
     * 和 [serviceConnected] 是两个不同的问题,而**它们不一致的那种情况
     * 恰好是最需要被看见的**:系统里勾着、服务却没在跑 = 被 ROM 的省电策略
     * 冻住了(ADR-021 的 2026-09-22 追加)。那时候用户去系统设置里看,
     * 开关是开着的,于是他会以为一切正常。
     *
     * [serviceConnected] 自己还有一个毛病:进程被杀时 `onDestroy` 不一定跑得到,
     * 那个 true 就留在那儿了。所以"权限给没给"这个问题只能由系统回答。
     */
    fun accessibilityEnabled(context: Context): Boolean {
        val enabled = runCatching {
            Settings.Secure.getString(
                context.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            )
        }.getOrNull().orEmpty()
        val component = "${context.packageName}/$SERVICE_CLASS"
        // 冒号分隔的一串。**按整段比,不用 contains** ——
        // 子串匹配会让一个包名以我们为前缀的服务顺带算数
        return enabled.split(':').any { it.trim() == component }
    }

    /**
     * 注册在清单里的那个类名。**是伪装过的那个**(ADR-035)。
     *
     * 写死在这里而不是 `SelectToSpeakService::class.java.name`:引用那个类
     * 会把它从伪装包里拖进这个文件的依赖里,而这里只需要一个字符串。
     * 改清单注册名时这里要跟着改 —— 不过 ADR-035 说了不要改。
     */
    private const val SERVICE_CLASS =
        "com.google.android.accessibility.selecttospeak.SelectToSpeakService"

    fun setServiceConnected(context: Context, connected: Boolean) =
        prefs(context).edit().putBoolean(KEY_CONNECTED, connected).apply()

    /**
     * 默认 false:无障碍权限从来没给过的那台手机不该报"一切正常"。
     *
     * 和 `CollectorState.listenerEnabled` 默认 false 同一个理由,
     * 但这里还多一层 —— 无障碍服务被 ROM 冻结之后**不会自己回来**,
     * 而系统设置里那个开关看起来还是开着的。
     */
    fun serviceConnected(context: Context): Boolean =
        prefs(context).getBoolean(KEY_CONNECTED, false)

    /** 记一次读屏结果。[diagnosis] 取上面那几个常量之一。 */
    fun recordCapture(context: Context, diagnosis: String, messageCount: Int, at: Long) =
        prefs(context).edit()
            .putString(KEY_DIAGNOSIS, diagnosis)
            .putInt(KEY_COUNT, messageCount)
            .putLong(KEY_AT, at)
            .apply()

    fun lastDiagnosis(context: Context): String? = prefs(context).getString(KEY_DIAGNOSIS, null)

    fun lastMessageCount(context: Context): Int = prefs(context).getInt(KEY_COUNT, 0)

    fun lastCaptureAt(context: Context): Long = prefs(context).getLong(KEY_AT, 0L)

    private const val FILE = "lifein.copilot.state"
    private const val KEY_CONNECTED = "service_connected"
    private const val KEY_DIAGNOSIS = "last_diagnosis"
    private const val KEY_COUNT = "last_message_count"
    private const val KEY_AT = "last_capture_at"

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(FILE, Context.MODE_PRIVATE)
}
