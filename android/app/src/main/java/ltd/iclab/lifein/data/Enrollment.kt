package ltd.iclab.lifein.data

import android.content.Context
import java.util.UUID
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * 配码 —— 二维码或粘贴框里那一串。
 *
 * **有两种,而它们的安全性差一个量级:**
 *
 * | 出处 | 里面是什么 | 能不能发给别人 |
 * | --- | --- | --- |
 * | `admin issue-device` | **明文的两把密钥** | **不能。** 发出去等于把密钥发在聊天里 |
 * | `admin invite`(`{"v":2,…}`) | 一张十分钟、只能用一次的换取码 | 能 |
 *
 * 第二种是 P4 加的(06 §6.15)。03 的 P4 说朋友要用它配码,而那张图会走微信
 * 发过去 —— 微信的聊天记录会漫游、会备份、会被截图。换取码即使被截图拿到,
 * 也只有两种结局:要么你已经换过了(他换不了),要么你还没换(你会发现自己
 * 换不了)。**两种都比"两个人各有一套"好。**
 *
 * 第一种留着,因为自己给自己配码时它更省一步(不需要服务端在线)。
 * **但 App 必须两种都认** —— 只认旧的那种,等于服务端补好了洞而客户端还走。
 *
 * **配码可以扫码也可以粘贴**(ADR-021 只否掉了深链接:scheme 谁都能注册,
 * 一次性凭据被别的 App 截走是最不该在这一步犯的错)。
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
         * 配码这一步出错时用户手上只有一个输入框和一段看不懂的 JSON。
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

/**
 * 扫进来/粘进来的那一串到底是什么。
 *
 * 做成两个分支而不是"先试 A 再试 B",是为了让**报错说得清**:
 * 一张过期的 invite 和一串手抖粘错的文本要给出完全不同的话,
 * 而"两个 parse 都失败了"只能给出后者。
 */
sealed interface EnrollmentPayload {
    /** `issue-device` 那种:密钥已经在手上,不用联网。 */
    data class Ready(val enrollment: Enrollment) : EnrollmentPayload

    /** `invite` 那种:手上只有一张换取码,密钥要去 `POST /enroll/claim` 换。 */
    data class Invite(val claim: String, val baseUrl: String) : EnrollmentPayload

    companion object {
        private val json = Json { ignoreUnknownKeys = true }

        /**
         * `admin invite` 打出来的 payload 里那个 `v`。
         *
         * 版本号存在的理由是**这两种 payload 长得都像 JSON**:没有它就只能靠
         * "有没有 claim 字段"去猜,而猜错的那一次会把一张换取码当成缺字段的
         * 配码,报出一句完全误导人的话。
         */
        const val INVITE_VERSION = "2"

        fun parse(text: String): EnrollmentPayload {
            val trimmed = text.trim()
            if (trimmed.isEmpty()) error("配码是空的")

            val version = try {
                json.parseToJsonElement(trimmed).jsonObject["v"]?.jsonPrimitive?.content
            } catch (e: Exception) {
                error("这串不像配码:${e.message ?: e::class.simpleName}")
            }

            if (version != INVITE_VERSION) {
                return Ready(Enrollment.parse(trimmed))
            }

            val obj = json.parseToJsonElement(trimmed).jsonObject
            val claim = obj["claim"]?.jsonPrimitive?.content.orEmpty()
            val baseUrl = obj["base_url"]?.jsonPrimitive?.content.orEmpty()
            if (claim.isBlank() || baseUrl.isBlank()) {
                error("这张配码里缺 claim / base_url,让对方重新跑一次 admin invite")
            }
            return Invite(claim = claim, baseUrl = baseUrl)
        }
    }
}

/**
 * 这台设备的 id。**由 App 生成,不由人现编**(06 §6.15)。
 *
 * 人编的名字会重复 —— 两个人都叫 `phone` —— 而重复的 `device_id` 意味着
 * **吊销一台会连带吊销另一台**,而那时被吊销的那个人不知道发生了什么。
 *
 * **生成一次就存下来。** 解绑之后重新配码要拿回同一个 id:
 * 每次配码换一个新 id 的话,服务端的设备列表会越长越长,
 * 而"手机丢了,吊销这台"就再也指不准是哪一台。
 *
 * 不用 `ANDROID_ID` 之类的硬件标识:那类东西在别的 App 里也拿得到,
 * 拿它当 id 等于把一个跨应用可关联的标识写进服务端(R12 的方向)。
 * 随机 UUID 只在这一台和它自己的服务端之间有意义。
 */
object DeviceId {

    private const val FILE = "lifein.device"
    private const val KEY = "device_id"
    private const val PREFIX = "android-"

    fun get(context: Context): String {
        val prefs = context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        prefs.getString(KEY, null)?.takeIf { it.isNotBlank() }?.let { return it }

        // 截短到 12 位:服务端上限 64,而这一串会出现在控制台的设备列表里,
        // 让人一眼分得清两台就够了,不需要完整的 128 位
        val fresh = PREFIX + UUID.randomUUID().toString().replace("-", "").take(12)
        prefs.edit().putString(KEY, fresh).apply()
        return fresh
    }
}
