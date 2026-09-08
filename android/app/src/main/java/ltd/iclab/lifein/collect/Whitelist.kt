package ltd.iclab.lifein.collect

import android.content.Context
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * 采集白名单的手机端那一份。
 *
 * **默认拒绝**([R10](../../../../../../../docs/05-risks.md)):不在名单里的通知
 * 根本不上报,不做"先全转发、服务端再过滤"。服务端入库前还会按自己那份再过一次
 * (06 §6.4 第 1 步)—— 两道独立。
 *
 * 名单从服务端拉(`GET /app/collector/status`),存在本地。
 * **拉不到时用内置的那份,绝不是全放行** —— 那等于把默认拒绝反过来。
 * 内置的只有微信,和 07 §4 的 P1 行一致。
 */
@Serializable
data class WhitelistRule(
    val id: Int = 0,
    @SerialName("match_type") val matchType: String,
    val pattern: String,
    val purpose: String,
    val enabled: Boolean = true,
) {
    fun matches(packageName: String?, sender: String?): Boolean = when (matchType) {
        // 包名全等:前缀匹配会让 com.tencent.mm 顺带放行 com.tencent.mm.fake
        MATCH_PACKAGE -> !packageName.isNullOrBlank() && packageName == pattern
        // 短信发件人前缀:银行短信的号码是号段,写死全等每换一个通道就漏一批
        MATCH_SMS_SENDER -> !sender.isNullOrBlank() && sender.startsWith(pattern)
        else -> false
    }

    companion object {
        const val MATCH_PACKAGE = "package_name"
        const val MATCH_SMS_SENDER = "sms_sender"
        const val PURPOSE_MESSAGE = "message"
    }
}

class Whitelist(private val rules: List<WhitelistRule>) {

    /**
     * 放不放行。**purpose 闸门在这里也有一道**:P1 只放消息,
     * 银行与支付类是 P2 的事。服务端那边同样会挡,两边一致不是重复 ——
     * 手机端挡住的那些根本不会离开这台设备。
     */
    fun allows(packageName: String?, sender: String?): Boolean =
        rules.any {
            it.enabled &&
                it.purpose == WhitelistRule.PURPOSE_MESSAGE &&
                it.matches(packageName, sender)
        }

    companion object {
        private const val FILE = "lifein.whitelist"
        private const val KEY = "rules"
        private val json = Json { ignoreUnknownKeys = true }

        /** P1 的内置名单:只有微信(07 §4)。拉不到服务端那份时用它。 */
        val BUILT_IN = listOf(
            WhitelistRule(
                matchType = WhitelistRule.MATCH_PACKAGE,
                pattern = "com.tencent.mm",
                purpose = WhitelistRule.PURPOSE_MESSAGE,
            )
        )

        fun load(context: Context): Whitelist {
            val stored = prefs(context).getString(KEY, null) ?: return Whitelist(BUILT_IN)
            return try {
                val rules = json.decodeFromString<List<WhitelistRule>>(stored)
                // 服务端给了一份空名单,那是"全部停用",不是"没拉到" —— 照它办
                Whitelist(rules)
            } catch (e: Exception) {
                // 本地那份坏了,退回内置的。**不退回全放行**
                Whitelist(BUILT_IN)
            }
        }

        fun save(context: Context, rules: List<WhitelistRule>) {
            prefs(context).edit()
                .putString(KEY, json.encodeToString(rules))
                .apply()
        }

        private fun prefs(context: Context) =
            context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
    }
}
