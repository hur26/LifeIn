package ltd.iclab.lifein.work

import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.time.LocalTime
import java.time.format.DateTimeFormatter
import java.util.concurrent.TimeUnit
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.ui.Repository
import ltd.iclab.lifein.widget.TodayWidget

/**
 * 小组件的刷新 —— **这个 App 里"主动去拉"的第二处**(第一处是心跳)。
 *
 * 拉不到就画缓存里那份,并且把"刷新于"那一行留在旧时间上:
 * **宁可显示旧的并说清它是旧的,也不要显示空白**。
 * 桌面上一片空白会让人以为系统坏了,而实际上多半只是地铁里没信号。
 */
class WidgetWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val repo = Repository(applicationContext)
        val dao = LifeInDatabase.get(applicationContext).cachedTodos()

        val fresh = runCatching { repo.todos() }.isSuccess
        val cached = dao.top(limit = 8)

        TodayWidget.render(
            context = applicationContext,
            todos = cached,
            updatedAt = if (fresh) LocalTime.now().format(HHMM) else "更早(拉取失败)",
        )
        // 失败也算成功:重试的意义不大,下一个周期就到了,
        // 而重试一串任务会在没信号的地方空耗电池
        return Result.success()
    }

    private companion object {
        val HHMM: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm")
    }
}

/**
 * 小组件的排期。**只有桌面上真的摆了小组件才排** ——
 * 没人看的时候每半小时拉一次是白耗电。
 */
object WidgetRefresh {

    private const val PERIODIC = "lifein.widget.refresh"
    private const val ONCE = "lifein.widget.now"

    fun schedule(context: Context) {
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            PERIODIC,
            ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<WidgetWorker>(30, TimeUnit.MINUTES)
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                )
                .build(),
        )
        now(context)
    }

    fun cancel(context: Context) {
        WorkManager.getInstance(context).cancelUniqueWork(PERIODIC)
    }

    /** 立刻刷一次:添加小组件、系统要求更新、或者刚在 App 里改完待办。 */
    fun now(context: Context) {
        WorkManager.getInstance(context).enqueueUniqueWork(
            ONCE,
            ExistingWorkPolicy.REPLACE,
            OneTimeWorkRequestBuilder<WidgetWorker>().build(),
        )
    }
}
