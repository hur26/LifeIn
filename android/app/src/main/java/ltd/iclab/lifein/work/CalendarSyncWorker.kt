package ltd.iclab.lifein.work

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import java.io.IOException
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.calendar.CalendarWriter
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.data.CalendarLink
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.net.CalendarReportBody
import ltd.iclab.lifein.net.LifeInApi

/**
 * 把服务端记下的日程写进系统日历,并**回报那条事件的 id**。
 *
 * 这是 ADR-020 里"副作用发生在服务端之外"那件事的设备侧。服务端只能保证
 * "意图已记录",这个任务是"设备已执行"的唯一来源;它不跑,那些日程就一直
 * 停在 `synced_at IS NULL` —— 而那个状态在 App 和小组件上都看得见,
 * **看得见的延迟可以接受,静默丢失不行**。
 *
 * 幂等责任在这一侧(06 §6.8):**先查本地那张对应表,再写日历**。
 * 上一次写成了但回报没送到时,这里重报旧 id,而不是写第二条 ——
 * 否则日历里会留下一条谁也删不掉的孤儿事件。
 */
class CalendarSyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val enrollment = LifeInApp.instance.secrets.load() ?: return Result.success()
        val writer = CalendarWriter(applicationContext)
        if (!writer.hasPermission()) {
            // 没权限不是错误,是"还没授权"。记下来让状态页说得出话 ——
            // 不记的话表现是"日程一直没进日历",而原因谁也看不见
            CollectorState.recordError(applicationContext, "没有日历权限,日程写不进去")
            return Result.success()
        }

        val api = LifeInApi(enrollment)
        val links = LifeInDatabase.get(applicationContext).calendarLinks()

        val queue = try {
            api.calendarQueue()
        } catch (e: LifeInApi.HttpError) {
            return if (e.isAuth) Result.failure() else Result.retry()
        } catch (e: IOException) {
            return Result.retry()
        }

        var written = 0
        var removed = 0

        for (item in queue.toCreate) {
            val start = item.startsAt ?: continue // 没有时间就写不进日历,服务端也不该给
            try {
                val existing = links.find(item.todoId)
                val eventId = existing?.eventId ?: writer.insert(
                    title = item.title,
                    startsAt = start,
                    endsAt = item.endsAt,
                    notes = item.notes,
                ).also {
                    links.put(CalendarLink(item.todoId, it, System.currentTimeMillis()))
                }
                api.reportCalendar(
                    CalendarReportBody(todoId = item.todoId, action = "created", deviceRef = eventId)
                )
                written++
            } catch (e: Exception) {
                // 一条失败不拖累其它条:剩下的日程还有价值,而这一条下次还会出现在队列里
                Log.w(TAG, "写日历失败 todo=${item.todoId}:${e.message}")
                CollectorState.recordError(applicationContext, "写日历失败:${e.message}")
            }
        }

        for (item in queue.toDelete) {
            try {
                writer.delete(item.deviceRef)
                links.forget(item.todoId)
                api.reportCalendar(
                    CalendarReportBody(todoId = item.todoId, action = "deleted")
                )
                removed++
            } catch (e: Exception) {
                Log.w(TAG, "删日历事件失败 todo=${item.todoId}:${e.message}")
            }
        }

        if (written > 0 || removed > 0) {
            CollectorState.recordUpload(
                applicationContext,
                "写进日历 $written 条,删掉 $removed 条",
            )
            // 桌面上那份"未入日历"的标记该消失了
            WidgetRefresh.now(applicationContext)
        }
        return Result.success()
    }

    private companion object {
        const val TAG = "LifeIn/calendar"
    }
}
