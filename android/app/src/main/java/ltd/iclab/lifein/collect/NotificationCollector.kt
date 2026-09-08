package ltd.iclab.lifein.collect

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.data.QueuedEvent
import ltd.iclab.lifein.work.UploadTrigger

/**
 * 通知监听 —— 这个 App 存在的根本原因(架构 §8)。
 *
 * **只做三件事:过滤、转成一条记录、进队列。**
 * 不解析、不归类、不判断(架构 §8.1)—— 语义处理全在服务端。
 * 手机端逻辑越薄,越不需要跟着业务改动重新发版,而发版意味着要去摸一次
 * 那台正在采集的主力机。
 *
 * 过滤的顺序和服务端那边一模一样,而且**两边都做**:
 *
 *     自己发的 → 分组汇总/常驻 → 白名单(默认拒绝) → 验证码 → 进队列
 *
 * 验证码那道在这里就丢,连本地库都不进([铁律 11](../../../../../../../AGENTS.md))。
 *
 * **没有前台服务。** 系统绑定这个服务,被杀了会重绑;为它挂一个常驻通知
 * 换来的保活收益说不清(ADR-021)。真掉线了靠服务端的心跳告警发现,
 * 一小时内 —— 那是 P1 的验收标准之一。
 */
class NotificationCollector : NotificationListenerService() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val decision = screen(sbn) ?: return
        scope.launch {
            runCatching { LifeInDatabase.get(applicationContext).queuedEvents().enqueue(decision) }
                .onFailure { Log.w(TAG, "写队列失败:${it::class.simpleName}") }
        }
        // 上报本身交给 WorkManager:这个回调跑在系统给的线程上,
        // 在这里发网络请求会把通知栏的响应一起拖住
        UploadTrigger.request(applicationContext)
    }

    /** 筛一条通知。返回 null 表示不上报。**能在这里挡住的就不要送出去。** */
    private fun screen(sbn: StatusBarNotification): QueuedEvent? {
        if (sbn.packageName == packageName) return null

        val notification = sbn.notification ?: return null
        val flags = notification.flags
        if (flags and Notification.FLAG_GROUP_SUMMARY != 0) {
            // 分组汇总是那几条消息的摘要,内容和被汇总的重复
            return null
        }
        if (flags and Notification.FLAG_ONGOING_EVENT != 0) {
            // 常驻通知(音乐、导航、下载)不是消息,而且它每秒都在更新
            return null
        }

        if (!Whitelist.load(applicationContext).allows(sbn.packageName, sender = null)) {
            return null
        }

        val extras = notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        // 优先取 BIG_TEXT:折叠状态下的 EXTRA_TEXT 常常只有一行,
        // 而"明天下午三点开会"很可能正好在被折起来的那部分里
        val text = (
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT)
                ?: extras.getCharSequence(Notification.EXTRA_TEXT)
            )?.toString().orEmpty()

        if (title.isBlank() && text.isBlank()) return null

        if (VerificationCode.matches(title, text)) {
            // 只记一行,不记内容:记下来等于把刚拦住的东西写进另一个地方
            Log.i(TAG, "丢弃一条疑似验证码的通知,来源 ${sbn.packageName}")
            return null
        }

        return QueuedEvent(
            channel = CHANNEL_NOTIFICATION,
            sourceApp = sbn.packageName,
            sender = null,
            postedAt = iso(sbn.postTime),
            title = title,
            text = text,
            // sbn.key 里有包名、id、tag、用户;加上 postTime 之后,
            // 同一条通知被更新时算新的一条,而重复 post 同一份不会
            externalId = "${sbn.key}#${sbn.postTime}",
        )
    }

    override fun onListenerConnected() {
        // 权限被打开、或者系统重新绑上来。心跳里的 listener_enabled 靠它翻面
        CollectorState.setListenerEnabled(applicationContext, true)
    }

    override fun onListenerDisconnected() {
        // 权限被收走时会走这里。**这种掉线最隐蔽** ——
        // 进程还活着、心跳照发,就是再也读不到东西(06 §6.5)
        CollectorState.setListenerEnabled(applicationContext, false)
    }

    private fun iso(epochMillis: Long): String =
        DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(
            Instant.ofEpochMilli(epochMillis).atZone(ZoneId.systemDefault())
        )

    private companion object {
        const val TAG = "LifeIn/collector"
        const val CHANNEL_NOTIFICATION = "notification"
    }
}
