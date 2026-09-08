package ltd.iclab.lifein.work

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

/**
 * 周期任务的排期表。**这里是这个 App 唯一"主动做事"的地方**,
 * 而它做的每一件事都是**去拉**,没有一件是等着被推
 * ([铁律 10](../../../../../../../AGENTS.md) / ADR-020)。
 *
 * 在 `Application.onCreate` 里调一次。不需要开机广播:
 * WorkManager 自己会在重启后把周期任务恢复回来,而进程每次起来都会经过这里,
 * 少一个权限、少一个能出错的地方。
 *
 * **十五分钟是系统的下限**,不是我们挑的。所以小组件不是提醒机制 ——
 * 它是"看一眼"的入口(ADR-020),真正的提醒走消息通道。
 */
object Schedules {

    private const val HEARTBEAT = "lifein.heartbeat"
    private const val UPLOAD_SWEEP = "lifein.upload.sweep"

    private val onNetwork = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    fun ensure(context: Context) {
        val manager = WorkManager.getInstance(context)

        // 心跳:掉线要在一小时内被发现,而服务端的判据是"多久没收到"。
        // 十五分钟一次,给网络抖动留了三次机会
        manager.enqueueUniquePeriodicWork(
            HEARTBEAT,
            ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<HeartbeatWorker>(15, TimeUnit.MINUTES)
                .setConstraints(onNetwork)
                .build(),
        )

        // 上报兜底:通知进来时会立刻触发一次(UploadTrigger),
        // 这个周期任务管的是那次没成的、以及离线期间攒下的
        manager.enqueueUniquePeriodicWork(
            UPLOAD_SWEEP,
            ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<UploadWorker>(30, TimeUnit.MINUTES)
                .setConstraints(onNetwork)
                .build(),
        )
    }
}
