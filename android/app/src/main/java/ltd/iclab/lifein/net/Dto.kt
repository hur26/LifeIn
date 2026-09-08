package ltd.iclab.lifein.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * 线上那几种形状 —— [06 §6](../../../../../../../docs/06-data-model.md#6-接口契约) 的可执行版本。
 *
 * 全部 `@Serializable`,字段名用 `@SerialName` 对齐服务端的下划线写法:
 * **契约是那份文档,不是 Kotlin 的命名习惯**。
 */
@Serializable
data class IngestBatch(
    @SerialName("device_id") val deviceId: String,
    val events: List<IngestEvent>,
)

@Serializable
data class IngestEvent(
    val channel: String,
    @SerialName("source_app") val sourceApp: String? = null,
    val sender: String? = null,
    @SerialName("posted_at") val postedAt: String,
    val title: String? = null,
    val text: String? = null,
    @SerialName("external_id") val externalId: String,
)

@Serializable
data class IngestResult(
    val accepted: Int = 0,
    val duplicates: Int = 0,
    val dropped: Map<String, Int> = emptyMap(),
) {
    /**
     * 全被丢掉了 —— 这不是错误(HTTP 是 200),但状态页要显示出来。
     * 最常见的原因是白名单还没在服务端放行,而那种情况下"一切正常但什么都没有"
     * 是最难自己想明白的(06 §6.4)。
     */
    val allDropped: Boolean
        get() = accepted == 0 && duplicates == 0 && dropped.values.sum() > 0
}

@Serializable
data class HeartbeatBody(
    @SerialName("device_id") val deviceId: String,
    @SerialName("app_version") val appVersion: String? = null,
    @SerialName("android_version") val androidVersion: String? = null,
    @SerialName("listener_enabled") val listenerEnabled: Boolean = true,
)

@Serializable
data class HeartbeatResult(
    /** 服务端时间。设备时钟偏了会表现成"所有请求 401 且不说原因",这是唯一的线索。 */
    @SerialName("server_time") val serverTime: String? = null,
)

@Serializable
data class TokenRequest(@SerialName("device_id") val deviceId: String)

@Serializable
data class TokenResponse(
    val token: String,
    @SerialName("expires_at") val expiresAt: String,
)

// ---------- 查询端(06 §6.6–§6.10) ----------

@Serializable
data class TodoDto(
    val id: String,
    val kind: String,
    val title: String,
    val notes: String? = null,
    @SerialName("starts_at") val startsAt: String? = null,
    @SerialName("ends_at") val endsAt: String? = null,
    val status: String = "open",
    val source: String = "agent",
    @SerialName("created_by_agent") val createdByAgent: String? = null,
    @SerialName("device_ref") val deviceRef: String? = null,
    /** 空 = 还没写进系统日历。**这个字段必须在界面上看得见**(ADR-020)。 */
    @SerialName("synced_at") val syncedAt: String? = null,
) {
    val isSchedule: Boolean get() = kind == "schedule"

    /** 要写日历、但设备还没确认写进去。看得见的延迟可以接受,静默丢失不行。 */
    val awaitingCalendar: Boolean get() = isSchedule && syncedAt.isNullOrBlank()
}

@Serializable
data class TodosResponse(val until: String? = null, val todos: List<TodoDto> = emptyList())

@Serializable
data class NewTodoBody(
    val title: String,
    val notes: String? = null,
    @SerialName("starts_at") val startsAt: String? = null,
)

@Serializable
data class StatusBody(val status: String)

@Serializable
data class PendingDto(
    val id: Long,
    val agent: String,
    val kind: String,
    @SerialName("target_table") val targetTable: String,
    val payload: Map<String, JsonElement> = emptyMap(),
    val reason: String,
    val confidence: Double? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
) {
    /**
     * 认不认识这一类。**不认识的只展示,不给确认按钮**(06 §6.7)——
     * P2 的账目进来时,这个版本的 App 会把它列出来但不让点,
     * 而不是拿错误的形状去确认。
     */
    val isKnown: Boolean get() = targetTable == "todos"

    val title: String
        get() = (payload["title"] as? JsonPrimitive)?.content ?: "(没有标题)"

    val startsAt: String?
        get() = (payload["starts_at"] as? JsonPrimitive)?.contentOrNull
}

@Serializable
data class PendingResponse(val pending: List<PendingDto> = emptyList())

@Serializable
data class ResolveBody(
    val action: String,
    /** 只放人看得懂的那几项。出处不由客户端说了算(铁律 5)。 */
    val payload: Map<String, String>? = null,
)

@Serializable
data class CalendarQueue(
    @SerialName("to_create") val toCreate: List<CalendarCreate> = emptyList(),
    @SerialName("to_delete") val toDelete: List<CalendarDelete> = emptyList(),
)

@Serializable
data class CalendarCreate(
    @SerialName("todo_id") val todoId: String,
    val title: String,
    val notes: String? = null,
    @SerialName("starts_at") val startsAt: String? = null,
    @SerialName("ends_at") val endsAt: String? = null,
)

@Serializable
data class CalendarDelete(
    @SerialName("todo_id") val todoId: String,
    @SerialName("device_ref") val deviceRef: String,
)

@Serializable
data class CalendarReportBody(
    @SerialName("todo_id") val todoId: String,
    val action: String,
    @SerialName("device_ref") val deviceRef: String? = null,
)

@Serializable
data class CollectorStatus(
    val devices: List<DeviceStatus> = emptyList(),
    val whitelist: List<ltd.iclab.lifein.collect.WhitelistRule> = emptyList(),
)

@Serializable
data class DeviceStatus(
    @SerialName("device_id") val deviceId: String,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    @SerialName("listener_enabled") val listenerEnabled: Boolean = true,
    val stale: Boolean = false,
)
