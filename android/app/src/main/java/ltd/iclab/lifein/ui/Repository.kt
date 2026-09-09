package ltd.iclab.lifein.ui

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.collect.Whitelist
import ltd.iclab.lifein.data.CachedTodo
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.collect.WhitelistRule
import ltd.iclab.lifein.net.BudgetBody
import ltd.iclab.lifein.net.BudgetDto
import ltd.iclab.lifein.net.CollectionStateDto
import ltd.iclab.lifein.net.CollectorStatus
import ltd.iclab.lifein.net.DeletedDto
import ltd.iclab.lifein.net.FactsResponse
import ltd.iclab.lifein.net.LifeInApi
import ltd.iclab.lifein.net.ManualTxnBody
import ltd.iclab.lifein.net.MonthlyReportDto
import ltd.iclab.lifein.net.StopCollectionResult
import ltd.iclab.lifein.net.NewTodoBody
import ltd.iclab.lifein.net.PendingDto
import ltd.iclab.lifein.net.ResolveBody
import ltd.iclab.lifein.net.TodoDto
import ltd.iclab.lifein.net.TransactionDto
import ltd.iclab.lifein.net.TxnPatchBody
import ltd.iclab.lifein.net.WhitelistBody
import ltd.iclab.lifein.work.Schedules
import ltd.iclab.lifein.work.WidgetRefresh

/**
 * 界面和网络之间的那一层。
 *
 * 它只做三件事:**把请求挪到 IO 线程、把拿到的东西缓存下来、把白名单同步给采集器**。
 * 没有业务判断 —— 判断在服务端,这一点和采集侧是同一条原则(架构 §8.1)。
 *
 * 白名单那件事值得单独说:采集器按**本地那份**过滤,而本地那份就是在这里
 * 被刷新的。App 一直不打开的话,采集器用的还是上次那份(或内置那份),
 * **不会退化成全放行**(R10)。
 */
class Repository(private val context: Context) {

    private fun api(): LifeInApi {
        val enrollment = LifeInApp.instance.secrets.load()
            ?: error("还没配码。先在配置页粘贴 issue-device 打出来的那一串")
        return LifeInApi(enrollment)
    }

    suspend fun todos(): List<TodoDto> = withContext(Dispatchers.IO) {
        val items = api().todos().todos
        cache(items)
        items
    }

    /** 打不通时给缓存 —— 地铁里打开 App 至少还能看见今天有什么。 */
    suspend fun cachedTodos(limit: Int = 20): List<CachedTodo> = withContext(Dispatchers.IO) {
        LifeInDatabase.get(context).cachedTodos().top(limit)
    }

    suspend fun addTodo(title: String): TodoDto = withContext(Dispatchers.IO) {
        // 用户自己加的:服务端记 source=user,不带 provenance —— 他就是出处
        api().createTodo(NewTodoBody(title = title))
    }

    suspend fun complete(todoId: String) = withContext(Dispatchers.IO) {
        api().setTodoStatus(todoId, "done")
        // 刚点完的那条不该还留在桌面上。小组件下一个周期才刷,
        // 而"点了没反应"会让人再点一次
        WidgetRefresh.now(context)
    }

    suspend fun cancel(todoId: String) = withContext(Dispatchers.IO) {
        // 撤销之后,已经写进日历的那条要等下一次同步才会被删(06 §6.8)
        api().setTodoStatus(todoId, "cancelled")
        WidgetRefresh.now(context)
    }

    suspend fun pending(): List<PendingDto> = withContext(Dispatchers.IO) {
        api().pending().pending
    }

    suspend fun confirm(id: Long, edits: Map<String, String>? = null) = withContext(Dispatchers.IO) {
        // 送什么由调用方决定,但**送什么算数由服务端决定**:它按 target_table
        // 取一份白名单,别的字段一律丢掉。provenance、created_by_agent、
        // source_event_id 永远沿用队列里那份 —— 出处不由客户端说了算(铁律 5)
        val payload = edits?.filterValues { it.isNotBlank() }?.ifEmpty { null }
        api().resolvePending(id, ResolveBody(action = "confirm", payload = payload))
        // 刚确认的那条日程该尽快进日历。等半小时的话,用户会以为没生效
        Schedules.syncCalendarNow(context)
        WidgetRefresh.now(context)
    }

    suspend fun reject(id: Long) = withContext(Dispatchers.IO) {
        api().resolvePending(id, ResolveBody(action = "reject"))
    }

    // ---------- 记忆(06 §6.10) ----------

    suspend fun facts(query: String? = null): FactsResponse = withContext(Dispatchers.IO) {
        api().facts(query)
    }

    suspend fun confirmFact(factId: String) = withContext(Dispatchers.IO) {
        api().confirmFact(factId)
    }

    suspend fun negateFact(factId: String) = withContext(Dispatchers.IO) {
        // 否定只标记不删:删了明天会被重新推断出来(R7)
        api().negateFact(factId)
    }

    suspend fun correctFact(factId: String, statement: String) = withContext(Dispatchers.IO) {
        // 只送改过的说法。出处由服务端从旧那条继承(铁律 5)
        api().correctFact(factId, statement)
    }

    suspend fun entities(query: String? = null) = withContext(Dispatchers.IO) {
        api().entities(query).entities
    }

    // ---------- 采集器 ----------

    suspend fun allowSource(packageName: String) = withContext(Dispatchers.IO) {
        api().addWhitelist(
            WhitelistBody(
                matchType = WhitelistRule.MATCH_PACKAGE,
                pattern = packageName.trim(),
                // P1 只放消息。加银行与支付类是 P2 的动作,不该从手机上顺手打开
                purpose = WhitelistRule.PURPOSE_MESSAGE,
            )
        )
    }

    suspend fun toggleSource(ruleId: Int, enabled: Boolean) = withContext(Dispatchers.IO) {
        api().toggleWhitelist(ruleId, enabled)
    }

    /**
     * 拉一次采集器状态,**顺手把白名单同步到本地**。
     *
     * 放在状态页而不是某个后台任务里:白名单变了要立刻生效的场景只有一个 ——
     * 你刚在服务端放行了一个来源,然后打开 App 看它有没有生效。
     */
    suspend fun collectorStatus(): CollectorStatus = withContext(Dispatchers.IO) {
        val status = api().collectorStatus()
        if (status.whitelist.isNotEmpty()) {
            Whitelist.save(context, status.whitelist)
        }
        status
    }


    // ---------- 账本(P2 第 13、14 片) ----------

    suspend fun transactions(
        category: String? = null,
        query: String? = null,
    ): List<TransactionDto> = withContext(Dispatchers.IO) {
        api().transactions(category = category, query = query).transactions
    }

    suspend fun monthlyReport(period: String? = null): MonthlyReportDto =
        withContext(Dispatchers.IO) { api().monthlyReport(period) }

    suspend fun budgets(): List<BudgetDto> = withContext(Dispatchers.IO) {
        api().budgets().budgets
    }

    suspend fun setBudget(category: String?, amount: String, threshold: String = "0.9") =
        withContext(Dispatchers.IO) {
            api().setBudget(BudgetBody(category = category, amount = amount, alertThreshold = threshold))
        }

    suspend fun deleteBudget(category: String?) = withContext(Dispatchers.IO) {
        api().deleteBudget(category)
    }

    /**
     * 改分类。**服务端会把它写回商户规则表**,以后这个商户就一直归到这一类,
     * 而且模型改不回去(ADR-008)—— 所以这个动作比它看起来重要。
     */
    suspend fun recategorize(txnId: Long, category: String): TransactionDto =
        withContext(Dispatchers.IO) {
            api().patchTransaction(txnId, TxnPatchBody(category = category))
        }

    suspend fun deleteTransaction(txnId: Long) = withContext(Dispatchers.IO) {
        api().deleteTransaction(txnId)
    }

    /** 手动补一笔。现金和纸质票据那条长尾,实时通知那一路永远采不到。 */
    suspend fun addTransaction(body: ManualTxnBody): TransactionDto =
        withContext(Dispatchers.IO) { api().addTransaction(body) }


    // ---------- 关掉采集、删掉数据(P4 第 3 片) ----------

    suspend fun collectionState(): CollectionStateDto =
        withContext(Dispatchers.IO) { api().collectionState() }

    /**
     * 关掉采集。**服务端吊销采集密钥并停掉全部白名单**,不等这个 App 配合 ——
     * App 有 bug、被降级、被别人装了旧版本,上报都可能继续。
     *
     * 关完把本地那份白名单也刷成空的,免得采集器在下一次同步之前还照旧过滤。
     */
    suspend fun stopCollection(): StopCollectionResult = withContext(Dispatchers.IO) {
        val result = api().stopCollection()
        Whitelist.save(context, emptyList())
        result
    }

    /** 删掉采集来的数据。**是真删,不是标记。** */
    suspend fun deleteCollected(since: String? = null): DeletedDto =
        withContext(Dispatchers.IO) { api().deleteCollected(since) }

    private suspend fun cache(items: List<TodoDto>) {
        LifeInDatabase.get(context).cachedTodos().replace(
            items.mapIndexed { index, todo ->
                CachedTodo(
                    id = todo.id,
                    title = todo.title,
                    startsAt = todo.startsAt,
                    kind = todo.kind,
                    awaitingCalendar = todo.awaitingCalendar,
                    sortKey = index,
                )
            }
        )
    }
}
