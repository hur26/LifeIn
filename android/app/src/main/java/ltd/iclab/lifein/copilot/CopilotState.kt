package ltd.iclab.lifein.copilot

import android.content.Context

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
