package ltd.iclab.lifein.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * 配码 —— 服务端 `python -m lifein.admin issue-device` 打出来的那一串。
 *
 * 两把密钥是**分开签发**的两条凭据(铁律 12):采集那把只能写,查询那把才读得到
 * 东西。App 两样都要,因为它两件事都做;但它们在服务端是两行,
 * 手机丢了可以只吊销其中一条(06 §6.1)。
 *
 * **配码走粘贴,不做扫码也不做深链接**(ADR-021):
 * 应用内扫码要引两套重依赖和一个相机权限,而深链接的 scheme 谁都能注册 ——
 * 一次性凭据被别的 App 截走,是最不该在这一步犯的错。
 */
@Serializable
data class Enrollment(
    @SerialName("base_url") val baseUrl: String,
    @SerialName("user_id") val userId: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("collector_secret") val collectorSecret: String = "",
    @SerialName("query_secret") val querySecret: String = "",
) {
    val isComplete: Boolean
        get() = collectorSecret.isNotBlank() && querySecret.isNotBlank()

    companion object {
        private val json = Json { ignoreUnknownKeys = true }

        /**
         * 解析粘进来的那一串。**报错要说人话** ——
         * 配码这一步出错时用户手上只有一个输入框和一段看不懂的 base64。
         */
        fun parse(text: String): Enrollment {
            val trimmed = text.trim()
            if (trimmed.isEmpty()) error("配码是空的")

            val parsed = try {
                json.decodeFromString<Enrollment>(trimmed)
            } catch (e: Exception) {
                error("这串不像配码:${e.message ?: e::class.simpleName}")
            }

            if (parsed.baseUrl.isBlank() || parsed.userId.isBlank() || parsed.deviceId.isBlank()) {
                error("配码里缺 base_url / user_id / device_id")
            }
            if (!parsed.isComplete) {
                // 只签了一把的情况真实存在:issue-device --purpose collect
                error("配码里少一把密钥。重新跑 issue-device,不要加 --purpose")
            }
            return parsed
        }
    }
}
