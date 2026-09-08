package ltd.iclab.lifein.net

import java.io.IOException
import java.time.OffsetDateTime
import java.util.concurrent.TimeUnit
import kotlinx.serialization.json.Json
import ltd.iclab.lifein.data.Enrollment
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * 服务端那两组接口的客户端。
 *
 * **两组用两把密钥,在这个类里也是分开的两条路径**(铁律 12)。
 * (路径前缀不写在注释里:Kotlin 的块注释可以嵌套,`ingest` 那个星号会
 * 开出一层新注释,而报错是文件末尾的"Unclosed comment" —— 找起来很费劲。)
 * 采集那一组用采集密钥逐条签名;查询那一组先用查询密钥换一个短期 token,
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

    // ---------- 查询端:带 token ----------

    fun todos(): TodosResponse =
        authed { json.decodeFromString(TodosResponse.serializer(), bearerGet(PATH_TODOS, it)) }

    fun createTodo(body: NewTodoBody): TodoDto = authed {
        val text = bearerPost(PATH_TODOS, it, json.encodeToString(NewTodoBody.serializer(), body))
        json.decodeFromString(TodoDto.serializer(), text)
    }

    fun setTodoStatus(todoId: String, status: String) = authed {
        bearerPost(
            "$PATH_TODOS/$todoId/status",
            it,
            json.encodeToString(StatusBody.serializer(), StatusBody(status)),
        )
    }

    fun pending(): PendingResponse =
        authed { json.decodeFromString(PendingResponse.serializer(), bearerGet(PATH_PENDING, it)) }

    fun resolvePending(id: Long, body: ResolveBody) = authed {
        bearerPost(
            "$PATH_PENDING/$id/resolve",
            it,
            json.encodeToString(ResolveBody.serializer(), body),
        )
    }

    fun calendarQueue(): CalendarQueue =
        authed { json.decodeFromString(CalendarQueue.serializer(), bearerGet(PATH_CAL_QUEUE, it)) }

    fun reportCalendar(body: CalendarReportBody) = authed {
        bearerPost(
            PATH_CAL_REPORT,
            it,
            json.encodeToString(CalendarReportBody.serializer(), body),
        )
    }

    fun facts(query: String?): FactsResponse = authed {
        json.decodeFromString(FactsResponse.serializer(), bearerGet(PATH_FACTS, it, q(query)))
    }

    fun confirmFact(factId: String) = authed { bearerPost("$PATH_FACTS/$factId/confirm", it, "{}") }

    fun negateFact(factId: String) = authed { bearerPost("$PATH_FACTS/$factId/negate", it, "{}") }

    fun correctFact(factId: String, statement: String): FactDto = authed {
        val text = bearerPost(
            "$PATH_FACTS/$factId/correct",
            it,
            json.encodeToString(CorrectionBody.serializer(), CorrectionBody(statement)),
        )
        json.decodeFromString(FactDto.serializer(), text)
    }

    fun entities(query: String?): EntitiesResponse = authed {
        json.decodeFromString(EntitiesResponse.serializer(), bearerGet(PATH_ENTITIES, it, q(query)))
    }

    fun addWhitelist(body: WhitelistBody) = authed {
        bearerPost(PATH_WHITELIST, it, json.encodeToString(WhitelistBody.serializer(), body))
    }

    fun toggleWhitelist(ruleId: Int, enabled: Boolean) = authed {
        bearerPost(
            "$PATH_WHITELIST/$ruleId/enabled",
            it,
            json.encodeToString(EnabledBody.serializer(), EnabledBody(enabled)),
        )
    }

    fun collectorStatus(): CollectorStatus =
        authed { json.decodeFromString(CollectorStatus.serializer(), bearerGet(PATH_STATUS, it)) }

    // ---------- 内部 ----------

    /**
     * 带着 token 跑一次,401 就换一把再跑一次 —— **只重试一次**。
     *
     * 换完还是 401,说明这台设备的凭据被吊销了(R11 要的单点吊销),
     * 那时候再试第三次只是浪费电。
     *
     * token 只放在内存里:进程重启就重新换一把,代价是一次签名请求,
     * 换掉的是"又多一处静态存放的凭据"。手机丢了以后,拖走那个进程的内存
     * 比拖走一个文件难得多。
     */
    private fun <T> authed(block: (String) -> T): T {
        val token = currentToken()
        return try {
            block(token)
        } catch (e: HttpError) {
            if (!e.isAuth) throw e
            synchronized(this) { cached = null }
            block(currentToken())
        }
    }

    private fun currentToken(): String {
        synchronized(this) {
            cached?.let { (token, expiresAt) ->
                // 提前五分钟换:正好在有效期边缘发出去的请求,到服务端时可能刚过期
                if (expiresAt - System.currentTimeMillis() > RENEW_MARGIN_MS) return token
            }
        }
        val issued = issueToken()
        val expiresAt = runCatching {
            OffsetDateTime.parse(issued.expiresAt).toInstant().toEpochMilli()
        }.getOrElse {
            // 服务端给的时间解不出来时,当它只活一个小时。宁可多换几次,
            // 也不要拿一个永不过期的 token 一直撞 401
            System.currentTimeMillis() + 3_600_000
        }
        synchronized(this) { cached = issued.token to expiresAt }
        return issued.token
    }

    @Volatile
    private var cached: Pair<String, Long>? = null


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

    internal fun bearerGet(
        path: String,
        token: String,
        query: Map<String, String> = emptyMap(),
    ): String {
        // 查询参数交给 HttpUrl 拼:搜索词里有中文和空格,自己拼字符串要么漏转义,
        // 要么转义两遍 —— 两种错都表现为"搜不到",而不是报错
        val url = (enrollment.baseUrl.trimEnd('/') + path).toHttpUrl().newBuilder()
            .apply { query.forEach { (name, value) -> addQueryParameter(name, value) } }
            .build()
        return execute(
            Request.Builder().url(url).header("Authorization", "Bearer $token").get().build()
        )
    }

    private fun q(query: String?): Map<String, String> =
        if (query.isNullOrBlank()) emptyMap() else mapOf("q" to query.trim())

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
                // 401 的响应体是空的,这是服务端有意的(06 §6.11)——
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
        const val PATH_TODOS = "/app/todos"
        const val PATH_PENDING = "/app/pending"
        const val PATH_CAL_QUEUE = "/app/calendar/queue"
        const val PATH_CAL_REPORT = "/app/calendar/report"
        const val PATH_STATUS = "/app/collector/status"
        const val PATH_WHITELIST = "/app/collector/whitelist"
        const val PATH_FACTS = "/app/memory/facts"
        const val PATH_ENTITIES = "/app/memory/entities"

        private const val RENEW_MARGIN_MS = 5 * 60 * 1000L

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
