package ltd.iclab.lifein.calendar

import android.Manifest
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract
import androidx.core.content.ContextCompat
import java.time.OffsetDateTime
import java.util.TimeZone

/**
 * 写系统日历 —— **L2 的副作用真正发生的地方**([ADR-020](../../../../../../../docs/04-tech-decisions.md))。
 *
 * 服务端那边写的只是 `todos` 里的一行,它保证的是"意图已记录";
 * 保证不了"设备已执行"。执行在这里,执行完要把系统日历那条事件的 id
 * 回报回去 —— **那个 id 就是回滚信息**(06 §2.11)。
 *
 * 选哪个日历:优先主日历,其次第一个可写的。写进 `CalendarContract` 之后
 * 小米、华为、三星自带的日历都看得见,而且不要求你装企微 ——
 * 这正是 ADR-020 选它的理由。
 */
class CalendarWriter(private val context: Context) {

    class NoCalendar : RuntimeException("这台手机上没有可写的日历账户")

    fun hasPermission(): Boolean =
        listOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR).all {
            ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
        }

    /**
     * 写一条事件,返回它的 id。
     *
     * 没有结束时间就按一小时:日历里没有"零长度"的事件,
     * 而一个瞬时点在周视图上根本看不见。
     */
    fun insert(title: String, startsAt: String, endsAt: String?, notes: String?): String {
        val calendarId = pickCalendar() ?: throw NoCalendar()
        val start = epoch(startsAt)
        val end = endsAt?.let(::epoch) ?: (start + DEFAULT_DURATION_MS)

        val values = ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, calendarId)
            put(CalendarContract.Events.TITLE, title)
            put(CalendarContract.Events.DESCRIPTION, notes ?: FOOTER)
            put(CalendarContract.Events.DTSTART, start)
            put(CalendarContract.Events.DTEND, end)
            // 时区必填,而且要写设备的:不写的话某些 ROM 会按 UTC 摆,
            // 一场下午三点的会出现在晚上十一点
            put(CalendarContract.Events.EVENT_TIMEZONE, TimeZone.getDefault().id)
        }

        val uri = context.contentResolver.insert(CalendarContract.Events.CONTENT_URI, values)
            ?: throw NoCalendar()
        return ContentUris.parseId(uri).toString()
    }

    /**
     * 删掉一条。返回是否真的删掉了。
     *
     * 删不掉也不当失败:用户可能自己在日历里删过了,那时候要的结果已经达成 ——
     * 而把它当失败会让这条永远留在"要删"的队列里(06 §6.8)。
     */
    fun delete(eventId: String): Boolean {
        val id = eventId.toLongOrNull() ?: return false
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, id)
        return context.contentResolver.delete(uri, null, null) > 0
    }

    private fun pickCalendar(): Long? {
        val projection = arrayOf(
            CalendarContract.Calendars._ID,
            CalendarContract.Calendars.IS_PRIMARY,
            CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL,
        )
        context.contentResolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            projection,
            "${CalendarContract.Calendars.VISIBLE} = 1",
            null,
            null,
        )?.use { cursor ->
            var fallback: Long? = null
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val primary = cursor.getInt(1) == 1
                val writable = cursor.getInt(2) >=
                    CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR
                if (!writable) continue
                if (primary) return id
                if (fallback == null) fallback = id
            }
            return fallback
        }
        return null
    }

    private fun epoch(iso: String): Long = OffsetDateTime.parse(iso).toInstant().toEpochMilli()

    private companion object {
        const val DEFAULT_DURATION_MS = 60 * 60 * 1000L
        const val FOOTER = "由 LifeIn 写入"
    }
}
