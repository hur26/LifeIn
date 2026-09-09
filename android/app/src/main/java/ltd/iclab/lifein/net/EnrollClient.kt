package ltd.iclab.lifein.net

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import ltd.iclab.lifein.data.Enrollment
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * 一次性换取码换密钥(`POST /enroll/claim`,06 §6.15)。
 *
 * **它不能走 [LifeInApi]**,理由不是嫌麻烦:那个类的每个方法都要拿
 * `Enrollment` 里的密钥去签名,而这一步的全部意义就是"还没有密钥"。
 * 06 §6.15 那句"它是这个系统里唯一一个不需要凭据的写入口"落在代码上,
 * 就是这里必须是一个独立的、不带任何签名逻辑的客户端。
 *
 * 也因为不带认证,它是唯一需要防爆破的接口 —— 而防爆破做在服务端
 * (码是 128 位随机数),这边不重试:
 *
 * **换取失败一律不重试。** 一张码只能用一次,而"超时了但服务端其实换成了"
 * 是完全可能的 —— 自动重试的结果是第二次拿 401,然后告诉用户"码无效",
 * 而实际上密钥已经签出去了、这张码已经废了、他再也换不了。
 * 让用户自己决定要不要重来,他至少知道自己点了两次。
 */
object EnrollClient {

    private val json = Json { ignoreUnknownKeys = true }
    private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()

    /**
     * 超时给得比平常短。配码是站着做的事,而**一个转了六十秒的圈**
     * 会让人以为该点第二次 —— 而第二次点下去那张码已经废了。
     */
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    @Serializable
    private data class ClaimIn(
        val code: String,
        @SerialName("device_id") val deviceId: String,
        @SerialName("app_version") val appVersion: String? = null,
    )

    /**
     * 换。成功返回可以直接存进 Keystore 的 [Enrollment]。
     *
     * 失败抛 [IOException],消息是给用户看的 —— 配码这一步出错时
     * 他手上只有一个按钮和一句话。
     */
    fun claim(
        baseUrl: String,
        code: String,
        deviceId: String,
        appVersion: String? = null,
    ): Enrollment {
        val body = json.encodeToString(
            ClaimIn.serializer(), ClaimIn(code = code, deviceId = deviceId, appVersion = appVersion)
        )
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + PATH)
            .post(body.toRequestBody(JSON_TYPE))
            .build()

        client.newCall(request).execute().use { response ->
            if (response.code == 401) {
                // 服务端三种原因不区分(码不对、用过了、过期了),这边也不猜。
                // 但**要把三种可能都说出来** —— 用户能自己对上是哪一种,
                // 而一句"配码无效"会让他反复扫同一张已经用掉的码
                throw IOException("这张配码用不了:可能已经用过、已经过期,或者根本不对。让对方重新发一张")
            }
            if (!response.isSuccessful) {
                throw IOException("服务端拒绝了这次配码(HTTP ${response.code})")
            }

            val text = response.body?.string().orEmpty()
            val enrollment = try {
                json.decodeFromString(Enrollment.serializer(), text)
            } catch (e: Exception) {
                throw IOException("服务端回的东西看不懂:${e.message ?: e::class.simpleName}")
            }
            if (!enrollment.isComplete) {
                // invite --purpose collect 只签一把。App 两件事都做,少一把跑不起来
                throw IOException("这张配码只签了一把密钥。让对方跑 admin invite 时不要加 --purpose")
            }
            return enrollment
        }
    }

    private const val PATH = "/enroll/claim"
}
