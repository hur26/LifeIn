package ltd.iclab.lifein.collect

import android.app.Notification
import android.app.Person
import android.content.Context
import android.content.pm.ApplicationInfo
import android.os.Build
import android.os.Bundle
import android.os.Parcelable
import android.util.Log

/**
 * **一次性诊断,答完就删。** 它要回答的问题只有一个:
 *
 * > 一条短信通知的 `extras` 里,到底有没有发件号码?
 *
 * 为什么这个问题决定后面怎么改:白名单的 `sms_sender` 承诺的是"发件号码前缀
 * 匹配",而采集端走的是通知监听(ADR-010,没有 `READ_SMS`)。2026-09-11 收到
 * 第一条真实银行短信时,通知标题是**「招商银行」而不是 95555** —— 于是那 14 条
 * 号段预设一条都没命中。
 *
 * 当时有两种可能,而**猜哪一种都会写出错的代码**:
 *
 * - 号码根本不在通知里 → `sms_sender` 这个 match_type 在本架构下不可实现,
 *   要重新定义或者去掉,而不是把显示名塞进一个叫"发件号码"的字段
 * - 号码在 `EXTRA_PEOPLE` / `EXTRA_MESSAGES` 这些地方,只是取错了位置 →
 *   那它是可实现的,改取值就行
 *
 * ## 它只打形状,不打内容
 *
 * 每一行日志里没有任何一个字是通知的原文:key 的名字、值的长度、值**像不像**
 * 一个号码(一个布尔)、URI 的 scheme。正文与大文本两个 key 直接跳过。
 *
 * 这条规矩和[铁律 11](../../../../../../../AGENTS.md) 里验证码那条是同一个
 * 道理:**刚拦住的东西不该被写到另一个地方去**,而日志正是"另一个地方"。
 *
 * ## 它只在 debug 包里跑
 *
 * 用 `FLAG_DEBUGGABLE` 判断,不引 `BuildConfig`(那要在 gradle 里多开一个
 * `buildFeatures`,为一个临时探针不值得)。
 */
object SenderProbe {

    const val TAG = "LifeInSenderProbe"

    /** 整串就是一个号码的样子。**只用来算一个布尔,不记下匹配到的东西。** */
    private val LOOKS_LIKE_NUMBER = Regex("""^\+?[0-9][0-9\-\s]{3,}$""")

    /** 正文不参与:它里面本来就有金额和卡号,报"含数字"只是噪音。 */
    private val SKIP = setOf(Notification.EXTRA_TEXT, Notification.EXTRA_BIG_TEXT)

    fun enabled(context: Context): Boolean =
        (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0

    /**
     * 把一条通知的 `extras` 长什么样打出来。
     *
     * 调用点刻意放在**白名单之前**:要回答的问题是"号码在不在通知里",
     * 而那和这条通知放不放行无关。没放行的那些照样不进队列、不上报 ——
     * 这里写的也只是形状,不是内容。
     */
    fun report(context: Context, notification: Notification) {
        if (!enabled(context)) return
        runCatching { describe(notification) }
            .onFailure { Log.w(TAG, "探针自己炸了:${it::class.simpleName}") }
    }

    private fun describe(notification: Notification) {
        val extras = notification.extras ?: run {
            Log.i(TAG, "extras 是空的")
            return
        }
        val keys = extras.keySet().sorted()
        Log.i(TAG, "--- 一条短信通知 --- extras 共 ${keys.size} 个 key")
        Log.i(TAG, "keys = $keys")

        for (key in keys) {
            if (key in SKIP) continue
            when (val value = @Suppress("DEPRECATION") extras.get(key)) {
                is CharSequence -> Log.i(TAG, "  $key: 文本 长度=${value.length} 像号码=${looksLikeNumber(value)}")
                is Array<*> -> Log.i(TAG, "  $key: 数组 长度=${value.size} 元素类型=${value.firstOrNull()?.javaClass?.simpleName}")
                is ArrayList<*> -> Log.i(TAG, "  $key: 列表 长度=${value.size} 元素类型=${value.firstOrNull()?.javaClass?.simpleName}")
                null -> Log.i(TAG, "  $key: null")
                else -> Log.i(TAG, "  $key: ${value.javaClass.simpleName}")
            }
        }

        describePeople(extras)
        describeMessages(extras)
    }

    /** `EXTRA_PEOPLE` 是老的 URI 数组,`EXTRA_PEOPLE_LIST` 是 28 起的 Person。 */
    private fun describePeople(extras: Bundle) {
        @Suppress("DEPRECATION")
        val uris = extras.getStringArray(Notification.EXTRA_PEOPLE)
        if (uris != null) {
            Log.i(TAG, "  EXTRA_PEOPLE: ${uris.size} 条,scheme=${uris.map { schemeOf(it) }}")
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            @Suppress("DEPRECATION")
            val people = extras.getParcelableArrayList<Person>(Notification.EXTRA_PEOPLE_LIST)
            if (people != null) {
                Log.i(TAG, "  EXTRA_PEOPLE_LIST: ${people.size} 条")
                people.forEachIndexed { index, person ->
                    Log.i(
                        TAG,
                        "    [$index] uri的scheme=${schemeOf(person.uri)} " +
                            "有key=${person.key != null} " +
                            "name像号码=${looksLikeNumber(person.name)}",
                    )
                }
            }
        }
    }

    /** MessagingStyle 那条路:每条消息自己带发件人。 */
    private fun describeMessages(extras: Bundle) {
        @Suppress("DEPRECATION")
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES) ?: return
        Log.i(TAG, "  EXTRA_MESSAGES: ${messages.size} 条")
        messages.forEachIndexed { index, raw ->
            val bundle = raw as? Bundle ?: return@forEachIndexed
            Log.i(TAG, "    [$index] keys=${bundle.keySet().sorted()}")
            val sender = @Suppress("DEPRECATION") bundle.getCharSequence("sender")
            if (sender != null) {
                Log.i(TAG, "    [$index] sender: 长度=${sender.length} 像号码=${looksLikeNumber(sender)}")
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                val person = @Suppress("DEPRECATION")
                bundle.getParcelable<Parcelable>("sender_person") as? Person
                if (person != null) {
                    Log.i(
                        TAG,
                        "    [$index] sender_person: uri的scheme=${schemeOf(person.uri)} " +
                            "name像号码=${looksLikeNumber(person.name)}",
                    )
                }
            }
        }
    }

    /** `tel:138…` → `tel`。**只留 scheme,号码本身不记。** */
    private fun schemeOf(uri: String?): String =
        uri?.substringBefore(':', missingDelimiterValue = "无scheme") ?: "空"

    private fun looksLikeNumber(value: CharSequence?): Boolean =
        value != null && LOOKS_LIKE_NUMBER.matches(value.toString().trim())
}
