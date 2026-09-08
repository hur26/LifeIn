package ltd.iclab.lifein.work

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.io.IOException
import java.util.concurrent.TimeUnit
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.data.QueuedEvent
import ltd.iclab.lifein.net.IngestEvent
import ltd.iclab.lifein.net.LifeInApi

/**
 * 把队列里的通知送上去。
 *
 * **送成功了才删。** 送之前删等于网络一抖就丢数据,而丢掉的那条在手机上
 * 已经不存在了 —— 通知栏里的东西划掉就没了,没有第二次机会。
 *
 * 三种失败分得很清楚:
 *
 * | 情况 | 怎么办 | 为什么 |
 * | --- | --- | --- |
 * | 网络不通、服务端 5xx | `retry()`,指数退避 | 会好的 |
 * | 401 | `failure()`,记下来给状态页 | 凭据被吊销或时钟偏了,重试多少次都一样 |
 * | 攒够了 `MAX_ATTEMPTS` 还送不出去 | 丢掉 | 三个月前的群消息补报进来只会让提取拿到一堆过期的东西 |
 *
 * 服务端把这一批全丢了(白名单没放行)也算成功:**那是服务端的决定**,
 * 留着重发只会无限循环。它会记进状态页,因为"一切正常但什么都没有"
 * 是这条链路上最难自己想明白的一种表现。
 */
class UploadWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val enrollment = LifeInApp.instance.secrets.load() ?: return Result.success()
        val dao = LifeInDatabase.get(applicationContext).queuedEvents()
        dao.dropExhausted(MAX_ATTEMPTS)

        val api = LifeInApi(enrollment)
        var accepted = 0
        var dropped = 0

        while (true) {
            val batch = dao.batch(BATCH_SIZE)
            if (batch.isEmpty()) break

            try {
                val result = api.ingest(enrollment.deviceId, batch.map { it.toDto() })
                dao.drop(batch.map { it.id })
                accepted += result.accepted
                dropped += result.dropped.values.sum()
            } catch (e: LifeInApi.HttpError) {
                dao.countAttempt(batch.map { it.id })
                CollectorState.recordError(applicationContext, describe(e))
                // 401 不重试:凭据被吊销、或者手机时钟偏了超过五分钟。
                // 两种都要人去处理,而不是让手机在后台一遍遍重试到没电
                return if (e.isAuth) Result.failure() else Result.retry()
            } catch (e: IOException) {
                dao.countAttempt(batch.map { it.id })
                CollectorState.recordError(applicationContext, "网络不通:${e.message}")
                return Result.retry()
            }
        }

        if (accepted > 0 || dropped > 0) {
            CollectorState.recordUpload(
                applicationContext,
                "收下 $accepted 条,服务端丢弃 $dropped 条",
            )
            if (accepted == 0 && dropped > 0) {
                // 最常见的原因是服务端白名单还没放行那个来源
                Log.i(TAG, "这一批全被服务端丢了,去看看 allow-source")
            }
        }
        return Result.success()
    }

    private fun QueuedEvent.toDto() = IngestEvent(
        channel = channel,
        sourceApp = sourceApp,
        sender = sender,
        postedAt = postedAt,
        title = title,
        text = text,
        externalId = externalId,
    )

    private fun describe(e: LifeInApi.HttpError): String = when (e.code) {
        // 401 的响应体是空的(服务端有意的),所以这句话是猜的 ——
        // 但它猜的是那两个真正常见的原因,比"HTTP 401"有用
        401 -> "服务端拒绝了这台设备:凭据被吊销,或手机时钟偏差超过五分钟"
        else -> "上报失败:HTTP ${e.code}"
    }

    companion object {
        private const val TAG = "LifeIn/upload"

        /** 和服务端的 `MAX_BATCH`(200)对齐留一半余量:超了那边直接 422。 */
        const val BATCH_SIZE = 100

        /** 退避之后大约是几天。到这儿还送不出去,那条消息也过期了。 */
        const val MAX_ATTEMPTS = 20
    }
}

/**
 * 有新东西要送 —— 通知监听那边调它。
 *
 * `KEEP`:一串消息连着进来时不该排出十个任务。真漏掉的那次由
 * [Schedules] 里那个周期任务兜底。
 */
object UploadTrigger {

    private const val WORK_NAME = "lifein.upload.now"

    fun request(context: Context) {
        val request = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(
                Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            )
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, request)
    }
}
