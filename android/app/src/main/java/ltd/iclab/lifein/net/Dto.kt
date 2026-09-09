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
     * 服务端加一类待确认不必等 App 发版,而没发版的 App 也不会拿错误的
     * 形状去确认。
     *
     * `transactions` 是 P2 加进来的。加它之前这里只有 `todos`,而服务端
     * 那一侧同样只会写 `todos` —— 两边一起改才有意义:只改一边的结果是
     * **按钮点得动但拿一个 422**,或者**服务端接得住而没人点得动**。
     */
    val isKnown: Boolean get() = targetTable in KNOWN_TABLES

    val isTransaction: Boolean get() = targetTable == "transactions"

    val title: String
        get() = (payload["title"] as? JsonPrimitive)?.content ?: "(没有标题)"

    val startsAt: String?
        get() = (payload["starts_at"] as? JsonPrimitive)?.contentOrNull

    // ---------- 账目那一类的字段 ----------

    private fun str(key: String): String? =
        (payload[key] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() }

    val amount: String? get() = str("amount")

    val direction: String? get() = str("direction")

    /** 商户。取不到就退回渠道名 —— 空着比"未知商户"更让人不知道这是什么。 */
    val merchant: String get() = str("merchant_raw") ?: str("channel") ?: "(没有商户)"

    /**
     * 资金变动的类型(`expense` / `repayment` / …)。
     *
     * **叫 `txnKind` 不叫 `kind`**:`kind` 已经被 `PendingDto` 的构造参数占了,
     * 那个是待确认的类别(`transaction` / `calendar_event`),两者不是一回事。
     * 撞名字的话编译器直接报冲突 —— 而更糟的情况是它没报,那时两个"kind"
     * 会在某一处被互相当成对方用。
     */
    val txnKind: String? get() = str("kind")

    val category: String? get() = str("category")

    /**
     * 这一条在列表里的一行标题。
     *
     * **账目不能用 `title`** —— 它的 payload 里根本没有那个字段,
     * 于是会显示成"(没有标题)",而一条看不出金额的账没人点得下去。
     */
    val headline: String
        get() = if (isTransaction) {
            val sign = if (direction == "credit") "+" else "-"
            "$sign${amount ?: "?"} · $merchant"
        } else {
            title
        }

    companion object {
        val KNOWN_TABLES = setOf("todos", "transactions")

        /**
         * 资金变动的类型,和服务端 `TxnKind` 一一对应。
         *
         * **顺序不是随手排的**:`repayment` 排在 `expense` 后面,因为
         * "信用卡还款被记成支出"是这个队列里最常见的那类错 ——
         * 消费那一刻已经记过一次,再记一次每个统计数字都会偏大。
         */
        val KINDS = listOf(
            "expense" to "支出",
            "repayment" to "还款",
            "income" to "收入",
            "transfer" to "转账",
            "refund" to "退款",
        )

        /** 和服务端 `CATEGORIES` 同一份封闭枚举。多一个少一个都会被工具挡回来。 */
        val CATEGORIES = listOf(
            "餐饮", "交通", "购物", "居住", "通信", "娱乐", "医疗", "教育", "人情", "其他",
        )
    }
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

// ---------- 记忆与实体浏览(06 §6.10) ----------

@Serializable
data class FactDto(
    val id: String,
    val statement: String,
    val confidence: Double = 0.0,
    @SerialName("confirmed_by_user") val confirmedByUser: Boolean = false,
    /** `raw_events.id`。**永远跟着事实一起来** —— 没有来源的记忆不该显示。 */
    val provenance: List<Long> = emptyList(),
    @SerialName("created_by_agent") val createdByAgent: String? = null,
    @SerialName("valid_until") val validUntil: String? = null,
) {
    /** 用户亲手改出来的那条,和系统推断的要能一眼分开。 */
    val authoredByUser: Boolean get() = createdByAgent == "user"
}

@Serializable
data class SourceDto(
    val source: String,
    val title: String? = null,
    @SerialName("occurred_at") val occurredAt: String? = null,
)

@Serializable
data class FactsResponse(
    val facts: List<FactDto> = emptyList(),
    /** 键是 `raw_events.id` 的字符串形式 —— JSON 的对象键只能是字符串。 */
    val sources: Map<String, SourceDto> = emptyMap(),
)

@Serializable
data class CorrectionBody(val statement: String)

@Serializable
data class EntityDto(
    val id: String,
    val kind: String,
    @SerialName("canonical_name") val canonicalName: String,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
)

@Serializable
data class EntitiesResponse(val entities: List<EntityDto> = emptyList())

@Serializable
data class WhitelistBody(
    @SerialName("match_type") val matchType: String,
    val pattern: String,
    val purpose: String,
    val phase: String = "P1",
)

@Serializable
data class EnabledBody(val enabled: Boolean)

// ---------- 账本、报表与预算(06 §6.11 / §6.12) ----------
//
// **金额一律是 String,不是 Double。** 服务端那边发的就是字符串,
// 而这边如果解成 Double,38.50 会变成 38.499999999999996 —— 账本上出现
// 那个数字比出现一笔错账更让人不信任。要算的时候用 BigDecimal(它)。

@Serializable
data class TransactionDto(
    val id: Long,
    @SerialName("occurred_at") val occurredAt: String,
    val amount: String,
    val currency: String = "CNY",
    val direction: String,
    val kind: String,
    @SerialName("merchant_raw") val merchantRaw: String? = null,
    val category: String? = null,
    @SerialName("account_hint") val accountHint: String? = null,
    @SerialName("order_no") val orderNo: String? = null,
    val channel: String,
    val stage: String,
    @SerialName("counts_as_spending") val countsAsSpending: Boolean = true,
) {
    /** 显示用。**只在这里转一次** —— 别处再解一遍就多一处可能解错的地方。 */
    val money: java.math.BigDecimal get() = java.math.BigDecimal(amount)
}

@Serializable
data class TransactionsResponse(val transactions: List<TransactionDto> = emptyList())

@Serializable
data class CategoryLineDto(
    val category: String,
    val total: String,
    val count: Int,
    @SerialName("last_total") val lastTotal: String? = null,
)

@Serializable
data class MerchantLineDto(val merchant: String, val total: String, val count: Int)

@Serializable
data class MonthlyReportDto(
    val period: String,
    val total: String,
    val count: Int,
    @SerialName("last_total") val lastTotal: String? = null,
    val categories: List<CategoryLineDto> = emptyList(),
    val merchants: List<MerchantLineDto> = emptyList(),
    val uncategorized: String = "0",
    /** 对账覆盖率。**null 表示这个月还没对过账**,不是 0。 */
    @SerialName("reconciled_ratio") val reconciledRatio: Double? = null,
    /** 上一次月报 job 写下的评语。空着说明还没跑过 —— 数字照样是准的。 */
    val notes: List<String> = emptyList(),
)

@Serializable
data class BudgetDto(
    /** null 就是总预算。 */
    val category: String? = null,
    val amount: String,
    @SerialName("alert_threshold") val alertThreshold: String,
    val spent: String,
    val remaining: String,
    val over: Boolean,
    val near: Boolean,
)

@Serializable
data class BudgetsResponse(val budgets: List<BudgetDto> = emptyList())

@Serializable
data class BudgetBody(
    val category: String? = null,
    val amount: String,
    @SerialName("alert_threshold") val alertThreshold: String = "0.9",
)

@Serializable
data class TxnPatchBody(
    val category: String? = null,
    @SerialName("merchant_raw") val merchantRaw: String? = null,
)

@Serializable
data class ManualTxnBody(
    @SerialName("occurred_at") val occurredAt: String,
    val amount: String,
    val direction: String = "debit",
    val kind: String = "expense",
    val category: String? = null,
    @SerialName("merchant_raw") val merchantRaw: String? = null,
    val note: String? = null,
)

// ---------- 关掉采集、删掉数据(P4 第 3 片,R10 改判的四前提之一) ----------

@Serializable
data class CollectionStateDto(
    /** 还在采不在采。**三层里任何一层关着就算关**(凭据、白名单、手机端)。 */
    val enabled: Boolean,
    @SerialName("active_devices") val activeDevices: Int = 0,
    @SerialName("enabled_rules") val enabledRules: Int = 0,
)

@Serializable
data class StopCollectionResult(val enabled: Boolean, val note: String = "")

@Serializable
data class DeletedDto(
    @SerialName("raw_events") val rawEvents: Int = 0,
    val transactions: Int = 0,
    val pending: Int = 0,
    val facts: Int = 0,
    val total: Int = 0,
)
