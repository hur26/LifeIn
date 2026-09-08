package ltd.iclab.lifein.work

import android.content.Context
import android.os.Build
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import java.io.IOException
import ltd.iclab.lifein.BuildConfig
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.net.HeartbeatBody
import ltd.iclab.lifein.net.LifeInApi

/**
 * 心跳 —— **这个 App 里最不起眼、也最不能省的一件事**。
 *
 * 采集器静默掉线时系统表现得一切正常,只是提醒和摘要悄悄变少
 * (架构 §8.6)。心跳是那件事唯一的探测器:服务端超过
 * `COLLECTOR_HEARTBEAT_TIMEOUT_M` 没收到就告警(06 §6.5),
 * P1 的验收标准要求一小时内。
 *
 * **`listener_enabled` 一起报上去**:进程活着但通知监听权限被系统收走,
 * 后果和掉线一样,而表现更隐蔽 —— 心跳照发,就是采不到东西。
 *
 * 心跳失败**不重试**:十五分钟后本来就还有一次。为它排一串重试,
 * 只会在信号不好的地铁里空耗电池。
 */
class HeartbeatWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val enrollment = LifeInApp.instance.secrets.load() ?: return Result.success()

        return try {
            val result = LifeInApi(enrollment).heartbeat(
                HeartbeatBody(
                    deviceId = enrollment.deviceId,
                    appVersion = BuildConfig.VERSION_NAME,
                    androidVersion = Build.VERSION.RELEASE,
                    listenerEnabled = CollectorState.listenerEnabled(applicationContext),
                )
            )
            CollectorState.recordHeartbeat(
                applicationContext,
                result.serverTime ?: "已上报",
            )
            Result.success()
        } catch (e: LifeInApi.HttpError) {
            CollectorState.recordError(applicationContext, "心跳被拒:HTTP ${e.code}")
            // 401 也只是记下来:十五分钟后再试一次的成本约等于零,
            // 而这条链路断了的后果是"服务端以为你掉线了"
            Result.success()
        } catch (e: IOException) {
            CollectorState.recordError(applicationContext, "心跳发不出去:${e.message}")
            Result.success()
        }
    }
}
