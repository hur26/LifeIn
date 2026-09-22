package ltd.iclab.lifein.copilot

import android.content.Context
import android.util.Log
import ltd.iclab.lifein.data.CopilotMessage
import ltd.iclab.lifein.data.LifeInDatabase

/**
 * 副驾的本地对话历史([ADR-038](docs/04-tech-decisions.md))。
 *
 * **"副驾读记忆,不写记忆"**:长上下文存在这台手机的 Room 里,
 * 服务端一行都没有。分析那一次把它拼进请求,服务端读完就丢。
 *
 * ## 为什么不用完即丢
 *
 * 用完即丢是最省事的做法,而且隐私上最干净。但它换来的是**回复质量下降** ——
 * 屏幕上只有十几条,而"上周你也是这么说的"这种话要在更早的地方才找得到依据。
 * 副驾读不到那些,就只能对着最后一句话干说。
 *
 * 所以这里的取舍是:**留,但留在你自己手机上**。上限每会话 300 条,
 * App 里一键清空,服务器上没有副本(09 §2)。
 *
 * ## 全部阻塞,要在工作线程上调
 *
 * 用的是非 suspend 的 DAO:调用方是那个单线程 executor,本来就在后台,
 * 再套一层协程只是让栈更深(和 `LifeInApi` 那边同一个判断)。
 */
class CopilotHistory(context: Context) {

    private val dao = LifeInDatabase.get(context).copilotMessages()

    /**
     * 会话的键:`包名|标题`。
     *
     * **标题空的时候返回 null,也就是不记。** 分不清这是谁的对话时,
     * 把两个人的历史搅在一起比没有历史糟得多 —— 后者只是少一点背景,
     * 前者会让副驾拿着张三的话去回李四。
     *
     * 用标题而不是某个 id,是因为读屏拿不到任何 id。代价是改群名会断开历史 ——
     * 那时表现是"它忘了之前说过什么",可接受。
     */
    fun conversationKey(pkg: String, title: String?): String? {
        val name = title?.trim().orEmpty()
        if (name.isEmpty()) return null
        return "$pkg|$name"
    }

    /**
     * 把这一屏并进历史,并回答"送给服务端的 history 该取几条"。
     *
     * 判断全在 [ChatLogMerge] 里(纯函数,有用例)。这里只负责读写。
     *
     * @return 排在这一屏之前的那段历史,最多 [window] 条,按时间正序
     */
    fun mergeAndRead(conversation: String, screen: List<Msg>, window: Int): List<Msg> {
        val stored = runCatching {
            dao.recent(conversation, ChatLogMerge.MAX_PER_CONVERSATION).map { Msg(it.side, it.text) }
        }.getOrElse {
            // 读不到历史只是少一点背景,不该让这次分析挂掉
            Log.w(TAG, "读副驾历史失败:${it::class.simpleName}")
            return emptyList()
        }

        val plan = ChatLogMerge.plan(stored, screen)
        Log.d(TAG, "历史合并:${plan.reason},追加 ${plan.append.size} 条,历史可用 ${plan.historyBefore}")

        if (plan.append.isNotEmpty()) {
            val now = System.currentTimeMillis()
            runCatching {
                dao.appendAndTrim(
                    plan.append.map {
                        CopilotMessage(
                            conversation = conversation,
                            side = it.side,
                            text = it.text,
                            at = now,
                        )
                    },
                    keep = ChatLogMerge.MAX_PER_CONVERSATION,
                )
            }.onFailure { Log.w(TAG, "写副驾历史失败:${it::class.simpleName}") }
        }

        return stored.take(plan.historyBefore).takeLast(window)
    }

    /** 09 §5 承诺的那个"一键清空副驾历史"。**不问服务端** —— 那边没有副本。 */
    fun clear() {
        runCatching { dao.clear() }.onFailure { Log.w(TAG, "清空失败:${it::class.simpleName}") }
    }

    /** 本地一共存了多少条。设置页要显示出来 —— 看不见的存储等于没有承诺过上限。 */
    fun total(): Int = runCatching { dao.total() }.getOrDefault(0)

    private companion object {
        const val TAG = "LifeIn/copilot"
    }
}
