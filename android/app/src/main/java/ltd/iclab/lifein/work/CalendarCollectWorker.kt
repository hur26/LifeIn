package ltd.iclab.lifein.work

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import java.io.IOException
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.calendar.CalendarChoice
import ltd.iclab.lifein.calendar.CalendarReader
import ltd.iclab.lifein.calendar.CalendarWriter
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.net.LifeInApi

/**
 * 把系统日历里的日程读出来报给服务端(06 §6.14)。
 *
 * **这是企微日程的替代**(ADR-026)。它和 [CalendarSyncWorker] 方向相反:
 * 那个把服务端记下的日程**写进**系统日历,这个把系统日历里的日程**读出来**。
 * 两个任务同时存在,所以回环是必须挡的 —— 见下。
 *
 * ## 为什么是"每次都报全窗口",而不是"只报新的"
 *
 * 日程会被改:时间挪了、地点变了、会被取消。**只报新的就永远看不到那些改动**。
 * 全窗口重报的代价是绝大多数条目在服务端撞去重键,而那条键本来就是为这个
 * 场景设计的(06 §2.6 第一层)—— 服务端回的 `duplicates` 通常远大于
 * `accepted`,那是正常的,不是浪费。
 *
 * > 代价说清楚:**改动目前不会更新已入库的那条**(去重键撞上就跳过)。
 * > 一场挪了时间的会在服务端仍然是老时间。这是已知的缺口,
 * > 而它比"只报新的"那条路好 —— 后者连挪没挪都不知道。
 *
 * ## 回环:自己写进去的不能再读回来
 *
 * `CalendarSyncWorker` 把提取出来的日程写进了系统日历,而这个任务读同一个
 * 日历。不排除的话:
 *
 *     邮件里提到"周三三点开会" → 提取 → todo → 写进系统日历
 *                                             ↓
 *                         这个任务读到它 → raw_events → 提取 agent 又看见
 *                                             ↓
 *                                   再建一条 todo → 再写进日历 → ……
 *
 * 一条日程会**指数级地繁殖**,而每一轮都长得像真的。挡它的是
 * `calendar_links` 那张表 —— App 写进去的事件 id 全在里面。
 */
class CalendarCollectWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val enrollment = LifeInApp.instance.secrets.load() ?: return Result.success()

        val selected = CalendarChoice.selected(applicationContext)
        if (selected.isEmpty()) {
            // 一个都没勾。**不是错误** —— 默认就是一个都不读(06 §6.14),
            // 而"没勾"和"读了但是空的"在状态页上要分得开
            return Result.success()
        }

        if (!CalendarWriter(applicationContext).hasPermission()) {
            // 和写日历共用同一个权限判断:少一处会漂移的检查
            CollectorState.recordError(applicationContext, "没有日历权限,日程读不出来")
            return Result.success()
        }

        val loopback = LifeInDatabase.get(applicationContext).calendarLinks()
            .writtenEventIds()
            .toSet()

        val events = CalendarReader.read(applicationContext, selected, loopback)
        if (events.isEmpty()) return Result.success()

        return try {
            val result = LifeInApi(enrollment).uploadCalendar(events)
            Log.i(
                TAG,
                "日历上报:读到 ${events.size} 收下 ${result.accepted} " +
                    "重复 ${result.duplicates} 解不开 ${result.unusable}",
            )
            if (result.unusable > 0) {
                // **解不开的不是丢了**(服务端照样入库,能修好重跑),
                // 但它意味着这一侧报上去的形状有问题 —— 要看得见
                CollectorState.recordError(
                    applicationContext,
                    "有 ${result.unusable} 条日程服务端解不开",
                )
            }
            Result.success()
        } catch (e: LifeInApi.HttpError) {
            // 认证问题重试多少次都一样,而网络问题下一轮就好了
            if (e.isAuth) Result.failure() else Result.retry()
        } catch (e: IOException) {
            Result.retry()
        }
    }

    private companion object {
        const val TAG = "CalendarCollect"
    }
}
