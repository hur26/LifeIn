package ltd.iclab.lifein.copilot

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder

/**
 * 一个只为"把进程钉在前台优先级"而存在的前台服务。
 *
 * **采集器没有这个,副驾有 —— 两者不矛盾**(ADR-021 的 2026-09-22 追加)。
 * `NotificationListenerService` 是系统绑定的,被杀了系统会重新绑;
 * **无障碍服务没有这个待遇**,被 ROM 的省电策略冻结之后不会自己回来,
 * 而系统设置里那个开关看起来还开着。
 *
 * 它治不好这个病,只是把它拖慢:用户还是要去开自启动、关电池优化。
 * 但一个**看得见的常驻通知**至少让"副驾还在跑着"这件事有个凭据 ——
 * 而它消失的时候,用户会先于任何告警注意到。
 *
 * 只在副驾开着的时候起。关掉副驾要把它一起停掉,否则通知栏里会留一条
 * 说着"副驾在运行"而副驾其实已经关了的常驻通知。
 */
class CopilotKeepAlive : Service() {

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            // IMPORTANCE_MIN:不响、不弹、在通知栏最底下。
            // 这条通知的作用是"能被查到",不是"要被看到"
            NotificationChannel(CHANNEL, "副驾运行中", NotificationManager.IMPORTANCE_MIN).apply {
                setShowBadge(false)
            }
        )
        val notification: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("LifeIn 副驾")
            .setContentText("读当前聊天窗,给回复候选。发送键仍然由你按")
            .setSmallIcon(android.R.drawable.ic_menu_edit)
            .setOngoing(true)
            .build()
        startForeground(NOTIFICATION_ID, notification)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL = "lifein_copilot"

        /** 1 被别人用了会互相顶掉,给它一个不像默认值的号。 */
        private const val NOTIFICATION_ID = 4101

        fun start(context: Context) {
            context.startForegroundService(Intent(context, CopilotKeepAlive::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, CopilotKeepAlive::class.java))
        }
    }
}
