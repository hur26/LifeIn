package ltd.iclab.lifein.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Index
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Transaction
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * 手机上的本地库。**它只存两类东西:还没送出去的,和送出去之后要对账的。**
 *
 * 不缓存"服务端已经有的东西"作为数据源 —— App 是待办的界面不是数据源
 * ([ADR-020](../../../../../../../docs/04-tech-decisions.md))。
 * 展示用的缓存另说,那是为了打开就有东西看,丢了不影响正确性。
 */
@Entity(
    tableName = "queued_events",
    // 同一条通知会被系统反复 post(内容更新、分组变化)。设备端先去一次重,
    // 服务端还有 raw_events 的唯一键兜着 —— 但让重复的东西根本不上路更省事
    indices = [Index(value = ["externalId"], unique = true)],
)
data class QueuedEvent(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val channel: String,
    val sourceApp: String?,
    val sender: String?,
    val postedAt: String,
    val title: String,
    val text: String,
    val externalId: String,
    val attempts: Int = 0,
)

@Dao
interface QueuedEventDao {

    /** 重复的静默忽略 —— 同一条通知被 post 两次不该变成两条上报。 */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(event: QueuedEvent): Long

    /**
     * 取一批要上报的。**按进来的顺序**:离线补报时先送老的,
     * 那样服务端看到的时间线不会跳。
     */
    @Query("SELECT * FROM queued_events ORDER BY id LIMIT :limit")
    suspend fun batch(limit: Int): List<QueuedEvent>

    /** 送成功了才删。送之前删等于网络一抖就丢数据。 */
    @Query("DELETE FROM queued_events WHERE id IN (:ids)")
    suspend fun drop(ids: List<Long>)

    @Query("UPDATE queued_events SET attempts = attempts + 1 WHERE id IN (:ids)")
    suspend fun countAttempt(ids: List<Long>)

    @Query("SELECT count(*) FROM queued_events")
    suspend fun pending(): Int

    /**
     * 攒了太久还送不出去的。**丢弃它们是有意的**:
     * 一条三个月前的群消息补报进来,只会让摘要和提取拿到一批过期的东西,
     * 而队列无限涨会让手机上的库越来越大。丢之前要能被看见(状态页给的就是条数)。
     */
    @Query("DELETE FROM queued_events WHERE attempts >= :maxAttempts")
    suspend fun dropExhausted(maxAttempts: Int)
}

/**
 * 展示用的缓存 —— **小组件和"打开就有东西看"靠它**。
 *
 * 它不是数据源:待办的真相在服务端(ADR-020),这张表丢了只是短暂地
 * 显示旧数据,下一次拉取就对上了。所以它只存看得见的那几列,
 * 不存 provenance、不存 notes 全文。
 *
 * [R11] 那句"App 本地缓存同样是攻击面"落在这条上:**只缓存展示所需的最小集**。
 */
@Entity(tableName = "cached_todos")
data class CachedTodo(
    @PrimaryKey val id: String,
    val title: String,
    val startsAt: String?,
    val kind: String,
    /** 要写日历但还没写进去。小组件上要标出来 —— 看得见的延迟可以接受。 */
    val awaitingCalendar: Boolean,
    val sortKey: Int,
)

@Dao
interface CachedTodoDao {

    @Query("SELECT * FROM cached_todos ORDER BY sortKey LIMIT :limit")
    suspend fun top(limit: Int): List<CachedTodo>

    @Query("DELETE FROM cached_todos")
    suspend fun clear()

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(items: List<CachedTodo>)

    /**
     * 整表替换。**在一个事务里**:中间断电的话,小组件上会显示半份列表,
     * 而那比显示旧的一整份更让人困惑。
     */
    @Transaction
    suspend fun replace(items: List<CachedTodo>) {
        clear()
        put(items)
    }
}

/**
 * 哪条待办对应系统日历里的哪条事件。**幂等责任在设备端**(06 §6.8)。
 *
 * 服务端只记最后一次回报,同一条 todo 写两次日历会留下两条事件而服务端
 * 只知道后一条 —— 那条孤儿事件谁也删不掉。所以**先查这张表再写日历**:
 * 有记录就直接把旧的 id 重报一次(多半是上次回报没送到),不再写第二条。
 */
@Entity(tableName = "calendar_links")
data class CalendarLink(
    @PrimaryKey val todoId: String,
    val eventId: String,
    val writtenAt: Long,
)

@Dao
interface CalendarLinkDao {

    @Query("SELECT * FROM calendar_links WHERE todoId = :todoId")
    suspend fun find(todoId: String): CalendarLink?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun put(link: CalendarLink)

    @Query("DELETE FROM calendar_links WHERE todoId = :todoId")
    suspend fun forget(todoId: String)
}

@Database(
    entities = [QueuedEvent::class, CachedTodo::class, CalendarLink::class],
    version = 3,
    exportSchema = true,
)
abstract class LifeInDatabase : RoomDatabase() {

    abstract fun queuedEvents(): QueuedEventDao

    abstract fun cachedTodos(): CachedTodoDao

    abstract fun calendarLinks(): CalendarLinkDao

    companion object {
        @Volatile
        private var instance: LifeInDatabase? = null

        /**
         * 加展示缓存那张表。**写真的迁移,不用破坏性重建** ——
         * 破坏性重建会把 `queued_events` 里还没送出去的通知一起删掉,
         * 而那些东西在手机上没有第二份。
         */
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS cached_todos (" +
                        "id TEXT NOT NULL PRIMARY KEY, " +
                        "title TEXT NOT NULL, " +
                        "startsAt TEXT, " +
                        "kind TEXT NOT NULL, " +
                        "awaitingCalendar INTEGER NOT NULL, " +
                        "sortKey INTEGER NOT NULL)"
                )
            }
        }

        /** 加日历幂等表。同样是真迁移:那张表丢了会让日历里多出一批孤儿事件。 */
        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS calendar_links (" +
                        "todoId TEXT NOT NULL PRIMARY KEY, " +
                        "eventId TEXT NOT NULL, " +
                        "writtenAt INTEGER NOT NULL)"
                )
            }
        }

        fun get(context: Context): LifeInDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    LifeInDatabase::class.java,
                    "lifein.db",
                ).addMigrations(MIGRATION_1_2, MIGRATION_2_3).build().also { instance = it }
            }
    }
}
