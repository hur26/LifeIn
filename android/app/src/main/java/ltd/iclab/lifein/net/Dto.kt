package ltd.iclab.lifein.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

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
