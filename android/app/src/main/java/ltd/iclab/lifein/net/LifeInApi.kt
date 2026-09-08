package ltd.iclab.lifein.net

import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import ltd.iclab.lifein.data.Enrollment
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * 服务端那两组接口的客户端。
 *
 * **两组用两把密钥,在这个类里也是分开的两条路径**(铁律 12):
 * `/ingest/*` 用采集密钥逐条签名;`/app/*` 先用查询密钥换一个短期 token,
 * 之后带 token。写成两个方法而不是一个"带上凭据"的通用方法,
 * 是为了让用错密钥变成一件写不出来的事。
 *
 * 全部是阻塞调用:调用方是 WorkManager 的 Worker,本来就在后台线程上,
 * 再套一层协程调度只是让栈更深。
 */
class LifeInApi(
    private val enrollment: Enrollment,
    private val client: OkHttpClient = shared,
) {

    class HttpError(val code: Int, message: String) : IOException(message) {
        /** 401 是"这台设备不该再进来了" —— 重试多少次都一样,别浪费电。 */
        val isAuth: Boolean get() = code == 401
    }

    // ---------- 采集端:每次请求签名 ----------

    fun ingest(deviceId: String, events: List<IngestEvent>): IngestResult {
        val body = json.encodeToString(IngestBatch.serializer(), IngestBatch(deviceId, events))
        val text = signedPost(PATH_INGEST, body, enrollment.collectorSecret)
        return json.decodeFromString(IngestResult.serializer(), text)
    }

    fun heartbeat(body: HeartbeatBody): HeartbeatResult {
        val payload = json.encodeToString(HeartbeatBody.serializer(), body)
        val text = signedPost(PATH_HEARTBEAT, payload, enrollment.collectorSecret)
        return json.decodeFromString(HeartbeatResult.serializer(), text)
    }

    // ---------- 查询端:换 token ----------

    fun issueToken(): TokenResponse {
        val payload = json.encodeToString(
            TokenRequest.serializer(), TokenRequest(enrollment.deviceId)
        )
        val text = signedPost(PATH_TOKEN, payload, enrollment.querySecret)
        return json.decodeFromString(TokenResponse.serializer(), text)
    }

    // ---------- 内部 ----------

    private fun signedPost(path: String, body: String, secret: String): String {
        // 序列化一次、签一次、发同一份:签的必须是**实际发出去的那串字节**
        val bytes = body.toByteArray()
        val timestamp = (System.currentTimeMillis() / 1000).toString()
        val signature = Signing.sign(
            secret,
            Signing.signingString("POST", path, timestamp, bytes),
        )

        val request = Request.Builder()
            .url(enrollment.baseUrl.trimEnd('/') + path)
            .post(bytes.toRequestBody(JSON))
            .apply {
                Signing.headers(enrollment.userId, enrollment.deviceId, timestamp, signature)
                    .forEach { (name, value) -> header(name, value) }
            }
            .build()

        return execute(request)
    }

    internal fun bearerGet(path: String, token: String): String =
        execute(
            Request.Builder()
                .url(enrollment.baseUrl.trimEnd('/') + path)
                .header("Authorization", "Bearer $token")
                .get()
                .build()
        )

    internal fun bearerPost(path: String, token: String, body: String): String =
        execute(
            Request.Builder()
                .url(enrollment.baseUrl.trimEnd('/') + path)
                .header("Authorization", "Bearer $token")
                .post(body.toByteArray().toRequestBody(JSON))
                .build()
        )

    private fun execute(request: Request): String {
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                // 401 的响应体是空的,这是服务端有意的(06 §6.10)——
                // 所以这里的消息只能靠状态码说话
                throw HttpError(response.code, "HTTP ${response.code} ${request.url.encodedPath}")
            }
            return text
        }
    }

    companion object {
        const val PATH_INGEST = "/ingest/events"
        const val PATH_HEARTBEAT = "/ingest/heartbeat"
        const val PATH_TOKEN = "/app/token"

        private val JSON = "application/json; charset=utf-8".toMediaType()
        internal val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

        /**
         * 超时给得短:这个 App 的所有请求都跑在 WorkManager 里,
         * 卡住半分钟不如失败一次让它重排 —— 重排是免费的,卡住会拖住整个队列。
         */
        val shared: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }
}
