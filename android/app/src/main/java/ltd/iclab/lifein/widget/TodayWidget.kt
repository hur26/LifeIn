package ltd.iclab.lifein.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import ltd.iclab.lifein.R
import ltd.iclab.lifein.data.CachedTodo
import ltd.iclab.lifein.ui.MainActivity
import ltd.iclab.lifein.work.WidgetRefresh

/**
 * 桌面小组件:今天的待办与日程。
 *
 * **它不违反[铁律 10](../../../../../../../AGENTS.md)。** 那条禁的是"接收推送" ——
 * 不集成厂商推送 SDK、不让第三方推送服务成为触达链路的一环。
 * 这里是反过来的:`AppWidgetProvider` + `WorkManager` **定时去拉**,
 * 没有任何一方向这台手机推送(ADR-020 写得很清楚)。
 *
 * 也正因为是拉的,它受各家省电策略限制。所以**它是"看一眼"的入口,
 * 不是提醒机制** —— 真正的提醒仍然走消息通道(微信,邮件兜底)。
 * 界面上那行"最后刷新"就是这条限制的诚实表达。
 */
class TodayWidget : AppWidgetProvider() {

    override fun onEnabled(context: Context) {
        // 第一个小组件被添加。在这之前不排刷新任务 —— 桌面上没有它的时候,
        // 每半小时拉一次是白耗电
        WidgetRefresh.schedule(context)
    }

    override fun onDisabled(context: Context) {
        // 最后一个被移走
        WidgetRefresh.cancel(context)
    }

    override fun onUpdate(
        context: Context,
        manager: AppWidgetManager,
        appWidgetIds: IntArray,
    ) {
        // 系统要求更新时(添加、重启、尺寸变化)先拉一次:
        // 桌面上摆着一份空白比摆着一份旧的更让人不信任
        WidgetRefresh.now(context)
    }

    companion object {

        private const val MAX_ROWS = 4
        private val ROW_IDS = intArrayOf(
            R.id.widget_row_0,
            R.id.widget_row_1,
            R.id.widget_row_2,
            R.id.widget_row_3,
        )

        /**
         * 画一遍。由刷新任务在后台线程上调 —— 它拿到的是**缓存里那份**,
         * 而不是现去请求:小组件的绘制不该等一次网络往返。
         */
        fun render(context: Context, todos: List<CachedTodo>, updatedAt: String) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, TodayWidget::class.java))
            if (ids.isEmpty()) return

            val views = RemoteViews(context.packageName, R.layout.widget_today)
            val shown = todos.take(MAX_ROWS)

            ROW_IDS.forEachIndexed { index, viewId ->
                val todo = shown.getOrNull(index)
                views.setTextViewText(viewId, todo?.let(::line) ?: "")
            }
            if (shown.isEmpty()) {
                views.setTextViewText(ROW_IDS[0], "今天没有安排")
            }
            views.setTextViewText(R.id.widget_updated, "刷新于 $updatedAt")

            // 点一下打开 App:小组件放不下的、以及要操作的都在那边
            views.setOnClickPendingIntent(
                R.id.widget_title,
                PendingIntent.getActivity(
                    context,
                    0,
                    Intent(context, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE,
                ),
            )

            ids.forEach { manager.updateAppWidget(it, views) }
        }

        private fun line(todo: CachedTodo): String {
            val time = todo.startsAt?.let { at -> at.substringAfter('T').take(5) + " " } ?: ""
            // 没写进日历的标一下:那正是 ADR-020 要求"在 App 上看得见"的状态,
            // 而小组件是最常被看见的那一处
            val mark = if (todo.awaitingCalendar) " ·未入日历" else ""
            return "$time${todo.title}$mark"
        }
    }
}
