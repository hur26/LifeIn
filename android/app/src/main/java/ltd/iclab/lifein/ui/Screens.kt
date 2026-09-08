package ltd.iclab.lifein.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import ltd.iclab.lifein.net.CollectorStatus
import ltd.iclab.lifein.net.PendingDto
import ltd.iclab.lifein.net.TodoDto
import androidx.compose.material3.AlertDialog
import ltd.iclab.lifein.net.CollectionStateDto

/**
 * 三个页面:今天、待确认、状态。
 *
 * 刻意都很朴素 —— 这个 App 的价值在采集和同步,不在界面。
 * 界面只要回答三个问题:**今天要干什么、有什么等我点头、采集器还活着吗**。
 */

@Composable
fun TodosScreen(repo: Repository) {
    var todos by remember { mutableStateOf<List<TodoDto>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var draft by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        runCatching { repo.todos() }
            .onSuccess {
                todos = it
                error = null
            }
            .onFailure { failure ->
                error = failure.message
                // 打不通就显示缓存 —— 地铁里打开也该看得见今天有什么
                if (todos == null) {
                    todos = repo.cachedTodos().map {
                        TodoDto(
                            id = it.id,
                            kind = it.kind,
                            title = it.title,
                            startsAt = it.startsAt,
                            syncedAt = if (it.awaitingCalendar) null else "cached",
                        )
                    }
                }
            }
    }

    LaunchedEffect(Unit) { refresh() }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = draft,
                onValueChange = { draft = it },
                label = { Text("加一条待办") },
                modifier = Modifier.weight(1f),
            )
            TextButton(
                enabled = draft.isNotBlank(),
                onClick = {
                    scope.launch {
                        runCatching { repo.addTodo(draft) }
                            .onSuccess {
                                draft = ""
                                refresh()
                            }
                            .onFailure { error = it.message }
                    }
                },
            ) { Text("加") }
        }

        error?.let {
            Text("连不上服务端:$it", color = MaterialTheme.colorScheme.error)
        }

        when (val list = todos) {
            null -> Loading()
            else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(list, key = { it.id }) { todo ->
                    TodoRow(
                        todo = todo,
                        onDone = { scope.launch { repo.complete(todo.id); refresh() } },
                        onCancel = { scope.launch { repo.cancel(todo.id); refresh() } },
                    )
                }
            }
        }
    }
}

@Composable
private fun TodoRow(todo: TodoDto, onDone: () -> Unit, onCancel: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(todo.title, style = MaterialTheme.typography.titleMedium)
            todo.startsAt?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            if (todo.awaitingCalendar) {
                // ADR-020 的那条"看得见的延迟":服务端记了要写日历,设备还没写进去。
                // 这一行不显示的话,"以为写进日历了其实没有"就变成了静默丢失
                Text(
                    "还没写进系统日历",
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Row {
                TextButton(onClick = onDone) { Text("完成") }
                TextButton(onClick = onCancel) { Text("撤销") }
            }
        }
    }
}

@Composable
fun PendingScreen(repo: Repository) {
    var items by remember { mutableStateOf<List<PendingDto>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        runCatching { repo.pending() }
            .onSuccess { items = it; error = null }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { refresh() }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Text("待确认", style = MaterialTheme.typography.headlineSmall)
        Text(
            "拿不准的都在这儿。**不确认就不会写进你的待办和日历**(铁律 7)。",
            style = MaterialTheme.typography.bodySmall,
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }

        when (val list = items) {
            null -> Loading()
            else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(list, key = { it.id }) { item ->
                    PendingRow(
                        item = item,
                        onConfirm = { scope.launch { repo.confirm(item.id); refresh() } },
                        onReject = { scope.launch { repo.reject(item.id); refresh() } },
                    )
                }
            }
        }
    }
}

@Composable
private fun PendingRow(item: PendingDto, onConfirm: () -> Unit, onReject: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(item.title, style = MaterialTheme.typography.titleMedium)
            item.startsAt?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            Text(
                "来自 ${item.agent} · ${item.reason}",
                style = MaterialTheme.typography.bodySmall,
            )
            if (item.isKnown) {
                Row {
                    TextButton(onClick = onConfirm) { Text("确认") }
                    TextButton(onClick = onReject) { Text("不对") }
                }
            } else {
                // 不认识的 target_table 只展示不给按钮(06 §6.7):
                // 服务端加一类待确认不必等 App 发版,而老版本也不会拿错误的形状去确认
                Text(
                    "这类(${item.targetTable})要新版本的 App 才能处理",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
fun StatusScreen(
    repo: Repository,
    localListenerEnabled: Boolean,
    queued: Int,
    /**
     * 页面末尾的那几个动作(开权限、解绑)。用插槽塞进**同一个可滚动的列**里 ——
     * 放在外面的话它们会被这一页的内容顶出屏幕,而"打开通知使用权"恰恰是
     * 新装的手机上最需要点的那一个。
     */
    footer: @Composable () -> Unit = {},
) {
    var status by remember { mutableStateOf<CollectorStatus?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var newSource by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        runCatching { repo.collectorStatus() }
            .onSuccess { status = it; error = null }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { refresh() }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("采集器", style = MaterialTheme.typography.headlineSmall)

        // 手机这一侧的事实,不依赖网络 —— 权限没开时这一行就是答案
        Text(if (localListenerEnabled) "通知监听:已开启" else "通知监听:没有权限,去系统设置里开")
        Text("待上报:$queued 条")

        HorizontalDivider()
        error?.let { Text("拉不到服务端状态:$it", color = MaterialTheme.colorScheme.error) }

        status?.devices?.forEach { device ->
            Text("${device.deviceId} · 最后心跳 ${device.lastSeenAt ?: "从没有过"}")
            if (device.stale) {
                Text("服务端认为这台已经掉线", color = MaterialTheme.colorScheme.error)
            }
        }

        HorizontalDivider()
        Text("放行的来源(默认拒绝)", style = MaterialTheme.typography.titleSmall)
        if (status?.whitelist?.isEmpty() == true) {
            Text(
                "一条都没有 —— 采集器送上去的东西全会被丢掉",
                color = MaterialTheme.colorScheme.error,
            )
        }
        status?.whitelist?.forEach { rule ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("${rule.pattern} · ${rule.purpose}", modifier = Modifier.weight(1f))
                // 只能开关,**没有删除**:留着那一行才回答得了"曾经放行过谁"(06 §6.9)
                Switch(
                    checked = rule.enabled,
                    onCheckedChange = { on ->
                        scope.launch { repo.toggleSource(rule.id, on); refresh() }
                    },
                )
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = newSource,
                onValueChange = { newSource = it },
                label = { Text("加一个包名") },
                placeholder = { Text("com.tencent.mm") },
                modifier = Modifier.weight(1f),
            )
            TextButton(
                enabled = newSource.isNotBlank(),
                onClick = {
                    scope.launch {
                        runCatching { repo.allowSource(newSource) }
                            .onSuccess { newSource = "" }
                            .onFailure { error = it.message }
                        refresh()
                    }
                },
            ) { Text("放行") }
        }
        Text(
            "只放消息类。银行与支付类是 P2 的事,不从这里打开。",
            style = MaterialTheme.typography.bodySmall,
        )

        HorizontalDivider()
        footer()
    }
}


@Composable
private fun Loading() {
    Column(
        Modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) { CircularProgressIndicator() }
}

/**
 * 关掉采集、删掉已采数据(P4 第 3 片)。
 *
 * [R10 那节的改判](../../../../../../../docs/05-risks.md)写着四个前提,
 * **少一件就不该开放**,而这是第 2 件:
 *
 * > 朋友要能自己关掉采集、并删掉已采的数据。**App 里要有这个开关,
 * > 不是"找你帮忙"。**
 *
 * "找你帮忙"和"自己能做"的差别不在功能,在**是不是要开口** ——
 * 一个人要发一条微信才能删掉自己的数据时,他多半不会发那条微信。
 *
 * 两个动作分开,不合并成一个:合成一个的话,"我想先停下来想想"就变成了
 * "要么继续采要么全删"。
 */
@Composable
fun CollectionControls(repo: Repository) {
    val scope = rememberCoroutineScope()
    var state by remember { mutableStateOf<CollectionStateDto?>(null) }
    var note by remember { mutableStateOf<String?>(null) }
    var confirmingDelete by remember { mutableStateOf(false) }

    suspend fun refresh() {
        runCatching { repo.collectionState() }.onSuccess { state = it }
    }

    LaunchedEffect(Unit) { refresh() }

    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text("你的数据", style = MaterialTheme.typography.titleMedium)
        Text(
            if (state?.enabled == true) "正在采集" else "没有在采集",
            style = MaterialTheme.typography.bodyMedium,
        )
        note?.let { Text(it, style = MaterialTheme.typography.bodySmall) }

        if (state?.enabled == true) {
            TextButton(onClick = {
                scope.launch {
                    runCatching { repo.stopCollection() }
                        .onSuccess { note = it.note }
                        .onFailure { note = "没关成:${it.message}" }
                    refresh()
                }
            }) { Text("关掉采集") }
        }

        TextButton(onClick = { confirmingDelete = true }) { Text("删掉已采的数据") }
        Text(
            "关掉采集不会删数据,删数据也不会自动关掉采集 —— 两件事分开。",
            style = MaterialTheme.typography.bodySmall,
        )
    }

    if (confirmingDelete) {
        AlertDialog(
            onDismissRequest = { confirmingDelete = false },
            title = { Text("删掉已采的数据?") },
            text = {
                // **是真删,不是标记。** 说清楚,因为它不可撤销
                Text(
                    "通知原文、由它们记下的账、待确认的条目,以及出处只剩这些的记忆," +
                        "会一起删掉。删了就找不回来了。"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    scope.launch {
                        runCatching { repo.deleteCollected() }
                            .onSuccess { note = "删掉了 ${it.total} 条" }
                            .onFailure { note = "没删成:${it.message}" }
                        confirmingDelete = false
                        refresh()
                    }
                }) { Text("删") }
            },
            dismissButton = {
                TextButton(onClick = { confirmingDelete = false }) { Text("算了") }
            },
        )
    }
}
