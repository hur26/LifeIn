package ltd.iclab.lifein.calendar

import android.content.Context
import android.database.Cursor
import android.provider.CalendarContract
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import ltd.iclab.lifein.net.CalendarEventBody

/**
 * 从系统日历读日程,报给服务端(06 §6.14)。
 *
 * **这是企微日程的替代**(ADR-026),而且严格更好:系统日历里有企微同步过来的、
 * 飞书的、订阅的、手动建的**全部**日程,企微那条只看得见企微自己那一个。
 * 权限也没新增 —— `READ_CALENDAR` 本来就要有,App 得往里写日程(ADR-020)。
 *
 * ## 三件只有手机做得了的事,都在这里
 *
 * 服务端那半只做归一化(`sources/device_calendar.py`),因为下面这三件它做不了:
 *
 * 1. **谁是本人。** `Events.ORGANIZER` 要和这个日历的 `OWNER_ACCOUNT` 比,
 *    而服务端不知道这台手机上哪个账户是本人
 * 2. **回环过滤。** App 自己写进去的那些日程不能再读回来 —— 见 [loopback]
 * 3. **读哪几个日历。** 用户勾的,存在这台手机上
 *
 * ## 去重键:`_ID` 靠不住
 *
 * `Events._ID` 是**本机自增**的,换手机、重装、清数据之后同一场会议会拿到
 * 一个新号,于是它作为一条新事件再进来一次 —— 表现是某天的摘要里每件事
 * 出现两次,而两条内容一模一样。
 *
 * `_SYNC_ID` 跨设备稳定,但**本地建的日程没有**。所以按 `_SYNC_ID → _ID`
 * 的顺序取,并且**一律带上账户名前缀**:两个日历各自的 `_ID` 都从 1 开始,
 * 不带前缀的话"公司日历的第 5 条"和"个人日历的第 5 条"是同一个键。
 */
object CalendarReader {

    /** 往前一天、往后三十天。理由见 06 §6.14:再往前是历史,再往后还会改。 */
    const val LOOK_BACK_DAYS = 1L
    const val LOOK_AHEAD_DAYS = 30L

    /** 一次最多报多少条。和服务端 `MAX_BATCH` 对齐,分批的责任在设备端。 */
    const val MAX_EVENTS = 200

    private val PROJECTION = arrayOf(
        CalendarContract.Events._ID,
        CalendarContract.Events._SYNC_ID,
        CalendarContract.Events.CALENDAR_ID,
        CalendarContract.Events.TITLE,
        CalendarContract.Events.DESCRIPTION,
        CalendarContract.Events.EVENT_LOCATION,
        CalendarContract.Events.DTSTART,
        CalendarContract.Events.DTEND,
        CalendarContract.Events.ORGANIZER,
        CalendarContract.Events.STATUS,
        CalendarContract.Events.DELETED,
    )

    /**
     * 读一批要上报的日程。
     *
     * @param selected 用户勾过的日历 id。**空集合返回空列表** ——
     *   默认一个都不读是有意的(06 §6.14):手机上常有生日、节假日、
     *   订阅的球赛,全读会把摘要淹掉,而淹掉的摘要等于没有摘要。
     * @param loopback App 自己写进系统日历的那些事件 id(`_ID`)。见 [Loopback]。
     */
    fun read(
        context: Context,
        selected: Set<Long>,
        loopback: Set<String>,
        now: Instant = Instant.now(),
    ): List<CalendarEventBody> {
        if (selected.isEmpty()) return emptyList()

        val owners = ownerAccounts(context, selected)
        if (owners.isEmpty()) return emptyList()

        val from = now.minusSeconds(LOOK_BACK_DAYS * 86_400).toEpochMilli()
        val until = now.plusSeconds(LOOK_AHEAD_DAYS * 86_400).toEpochMilli()

        val ids = selected.joinToString(",")
        val cursor = context.contentResolver.query(
            CalendarContract.Events.CONTENT_URI,
            PROJECTION,
            // 参数化只用在时间上:日历 id 是我们自己从系统里读出来的 Long,
            // 而 selection args 在 IN 子句里要一个一个占位,拼出来更难读
            "${CalendarContract.Events.CALENDAR_ID} IN ($ids)" +
                " AND ${CalendarContract.Events.DTSTART} >= ?" +
                " AND ${CalendarContract.Events.DTSTART} <= ?",
            arrayOf(from.toString(), until.toString()),
            "${CalendarContract.Events.DTSTART} ASC",
        ) ?: return emptyList()

        return cursor.use { rows -> collect(rows, owners, loopback) }
    }

    private fun collect(
        rows: Cursor,
        owners: Map<Long, Owner>,
        loopback: Set<String>,
    ): List<CalendarEventBody> {
        val out = mutableListOf<CalendarEventBody>()
        while (rows.moveToNext() && out.size < MAX_EVENTS) {
            val localId = rows.getLong(0).toString()

            // **回环:自己写进去的不能再读回来。**
            // 漏了这一条,一条日程会指数级地繁殖(见 Loopback 的说明)
            if (localId in loopback) continue
            if (rows.getInt(10) != 0) continue // DELETED

            val calendarId = rows.getLong(2)
            val owner = owners[calendarId] ?: continue
            val start = rows.getLong(6)
            if (start <= 0L) continue

            val syncId = rows.getString(1)?.takeIf { it.isNotBlank() }
            val organizer = rows.getString(8)?.trim().orEmpty()

            out.add(
                CalendarEventBody(
                    // 账户名前缀不能省 —— 见类文档"去重键"那一段
                    eventId = "${owner.account}/${syncId ?: localId}",
                    calendar = owner.displayName,
                    title = rows.getString(3)?.trim().orEmpty(),
                    description = rows.getString(4)?.trim(),
                    location = rows.getString(5)?.trim(),
                    startsAt = iso(start),
                    endsAt = rows.getLong(7).takeIf { it > 0L }?.let(::iso),
                    organizer = organizer.ifBlank { null },
                    // 组织者就是这个日历的主人 = 自己建的。服务端不重算这一条:
                    // "本人是谁"只有手机知道(06 §6.14)
                    selfOrganized = organizer.isBlank() ||
                        organizer.equals(owner.account, ignoreCase = true),
                    cancelled = rows.getInt(9) == CalendarContract.Events.STATUS_CANCELED,
                )
            )
        }
        return out
    }

    private data class Owner(val account: String, val displayName: String)

    private fun ownerAccounts(context: Context, selected: Set<Long>): Map<Long, Owner> {
        val cursor = context.contentResolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            arrayOf(
                CalendarContract.Calendars._ID,
                CalendarContract.Calendars.OWNER_ACCOUNT,
                CalendarContract.Calendars.ACCOUNT_NAME,
                CalendarContract.Calendars.CALENDAR_DISPLAY_NAME,
            ),
            null,
            null,
            null,
        ) ?: return emptyMap()

        return cursor.use { rows ->
            buildMap {
                while (rows.moveToNext()) {
                    val id = rows.getLong(0)
                    if (id !in selected) continue
                    // OWNER_ACCOUNT 有时是空的(本地日历),退回 ACCOUNT_NAME。
                    // 两个都空就用一个稳定的占位 —— **去重键宁可难看,不能变**
                    val account = rows.getString(1)?.takeIf { it.isNotBlank() }
                        ?: rows.getString(2)?.takeIf { it.isNotBlank() }
                        ?: "local"
                    put(id, Owner(account, rows.getString(3)?.trim().orEmpty()))
                }
            }
        }
    }

    /** 手机上的全部日历,给状态页那个勾选列表用。 */
    fun list(context: Context): List<CalendarInfo> {
        val cursor = context.contentResolver.query(
            CalendarContract.Calendars.CONTENT_URI,
            arrayOf(
                CalendarContract.Calendars._ID,
                CalendarContract.Calendars.CALENDAR_DISPLAY_NAME,
                CalendarContract.Calendars.ACCOUNT_NAME,
            ),
            null,
            null,
            "${CalendarContract.Calendars.CALENDAR_DISPLAY_NAME} ASC",
        ) ?: return emptyList()

        return cursor.use { rows ->
            buildList {
                while (rows.moveToNext()) {
                    add(
                        CalendarInfo(
                            id = rows.getLong(0),
                            name = rows.getString(1)?.trim().orEmpty(),
                            account = rows.getString(2)?.trim().orEmpty(),
                        )
                    )
                }
            }
        }
    }

    private fun iso(millis: Long): String =
        DateTimeFormatter.ISO_OFFSET_DATE_TIME.format(
            Instant.ofEpochMilli(millis).atZone(ZoneId.systemDefault())
        )
}

data class CalendarInfo(val id: Long, val name: String, val account: String)

/**
 * 用户勾了哪几个日历。**默认一个都没有。**
 *
 * 手机上常有生日、节假日、订阅的球赛、公司全员会 —— 全读会把摘要淹掉,
 * 而淹掉的摘要等于没有摘要。和采集白名单是同一条思路(R10):
 * 默认拒绝、用户显式放行、随时能改。
 */
object CalendarChoice {

    private const val FILE = "lifein.device"
    private const val KEY = "calendars"

    fun selected(context: Context): Set<Long> =
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            .getStringSet(KEY, emptySet())
            .orEmpty()
            .mapNotNull { it.toLongOrNull() }
            .toSet()

    fun choose(context: Context, ids: Set<Long>) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            .edit()
            .putStringSet(KEY, ids.map(Long::toString).toSet())
            .apply()
    }
}
