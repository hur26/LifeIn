package ltd.iclab.lifein.collect

import android.content.Context

/**
 * 采集器自己的状态 —— 状态页和心跳都读它。
 *
 * 存的都是"丢了重来一次就好"的东西(和服务端 `channel_state` 同一个判据):
 * 丢了之后最多是状态页上少一行"上次上报"的记录,采集本身不受影响。
 *
 * **`listenerEnabled` 是这里最重要的一个值。** 权限被系统收走时
 * `onListenerDisconnected` 会把它翻成 false,心跳把它带给服务端,
 * 服务端据此告警(06 §6.5)—— 这种掉线最隐蔽:进程活着、心跳照发,
 * 就是再也读不到东西。
 */
object CollectorState {

    private const val FILE = "lifein.collector"
    private const val KEY_LISTENER = "listener_enabled"
    private const val KEY_LAST_UPLOAD = "last_upload"
    private const val KEY_LAST_HEARTBEAT = "last_heartbeat"
    private const val KEY_LAST_ERROR = "last_error"

    fun setListenerEnabled(context: Context, enabled: Boolean) =
        prefs(context).edit().putBoolean(KEY_LISTENER, enabled).apply()

    /**
     * 默认 false:没被系统绑上来过就是没在采集。
     * 反过来默认 true 的话,权限从来没给过的那台手机会报"一切正常"。
     */
    fun listenerEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_LISTENER, false)

    fun recordUpload(context: Context, summary: String) =
        prefs(context).edit().putString(KEY_LAST_UPLOAD, summary).remove(KEY_LAST_ERROR).apply()

    fun recordHeartbeat(context: Context, summary: String) =
        prefs(context).edit().putString(KEY_LAST_HEARTBEAT, summary).apply()

    /** 失败要留下来给状态页看。**看不见的失败才是真失败**(R8)。 */
    fun recordError(context: Context, message: String) =
        prefs(context).edit().putString(KEY_LAST_ERROR, message).apply()

    fun lastUpload(context: Context): String? = prefs(context).getString(KEY_LAST_UPLOAD, null)

    fun lastHeartbeat(context: Context): String? =
        prefs(context).getString(KEY_LAST_HEARTBEAT, null)

    fun lastError(context: Context): String? = prefs(context).getString(KEY_LAST_ERROR, null)

    private fun prefs(context: Context) =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
}
