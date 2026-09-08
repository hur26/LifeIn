package ltd.iclab.lifein.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import ltd.iclab.lifein.net.FactDto
import ltd.iclab.lifein.net.SourceDto

/**
 * 记忆浏览:查看、否定、纠正([03 的 P1 查看侧](../../../../../../../docs/03-roadmap.md))。
 *
 * **每条事实旁边都显示出处。** P1 的退出条件写着"记忆里开始出现你不认可
 * 又说不清来源的条目 → provenance 链路有漏",而这个界面就是那句话的日常验证口 ——
 * 翻库查 `provenance` 谁也不会天天做,在手机上顺手看一眼才会。
 *
 * 三个动作对应记忆层的三条规矩:
 *
 * | 按钮 | 服务端做什么 |
 * | --- | --- |
 * | 对 | 确认。**这是突破 external 0.6 上限的唯一路径** |
 * | 不对 | 否定。只标记不删 —— 删了明天会被重新推断出来 |
 * | 改 | 否定旧的 + 用**同一份出处**写一条新的 |
 */
@Composable
fun MemoryScreen(repo: Repository) {
    var facts by remember { mutableStateOf<List<FactDto>?>(null) }
    var sources by remember { mutableStateOf<Map<String, SourceDto>>(emptyMap()) }
    var query by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        runCatching { repo.facts(query.takeIf { it.isNotBlank() }) }
            .onSuccess {
                facts = it.facts
                sources = it.sources
                error = null
            }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { refresh() }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                label = { Text("搜记忆") },
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { scope.launch { refresh() } }) { Text("搜") }
        }
        error?.let { Text("连不上服务端:$it", color = MaterialTheme.colorScheme.error) }

        when (val list = facts) {
            null -> Text("加载中…")
            else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(list, key = { it.id }) { fact ->
                    FactRow(
                        fact = fact,
                        sources = sources,
                        onConfirm = { scope.launch { repo.confirmFact(fact.id); refresh() } },
                        onNegate = { scope.launch { repo.negateFact(fact.id); refresh() } },
                        onCorrect = { text ->
                            scope.launch {
                                runCatching { repo.correctFact(fact.id, text) }
                                    .onFailure { error = "改不了:${it.message}" }
                                refresh()
                            }
                        },
                    )
                }
                if (list.isEmpty()) {
                    items(listOf("empty")) { Text("还没有记忆。抽取任务每天跑一次") }
                }
            }
        }
    }
}

@Composable
private fun FactRow(
    fact: FactDto,
    sources: Map<String, SourceDto>,
    onConfirm: () -> Unit,
    onNegate: () -> Unit,
    onCorrect: (String) -> Unit,
) {
    var editing by remember { mutableStateOf(false) }
    var draft by remember { mutableStateOf(fact.statement) }

    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(fact.statement, style = MaterialTheme.typography.titleMedium)

            val mark = when {
                fact.authoredByUser -> "你改的"
                fact.confirmedByUser -> "你确认过"
                else -> "系统推断 · 置信度 ${"%.1f".format(fact.confidence)}"
            }
            Text(mark, style = MaterialTheme.typography.bodySmall)

            // 出处。**没有来源的记忆不该出现在这里** —— 真出现了就是 provenance 有漏,
            // 那是 P1 的退出条件之一,所以这一行要显眼到不用找
            if (fact.provenance.isEmpty()) {
                Text("说不清来源", color = MaterialTheme.colorScheme.error)
            } else {
                fact.provenance.forEach { eventId ->
                    val source = sources[eventId.toString()]
                    Text(
                        "来自:" + (source?.let { "${it.source} · ${it.title ?: "(无标题)"}" }
                            ?: "事件 $eventId"),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }

            if (editing) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    label = { Text("改成") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Row {
                    TextButton(
                        enabled = draft.isNotBlank() && draft != fact.statement,
                        onClick = {
                            editing = false
                            onCorrect(draft)
                        },
                    ) { Text("保存") }
                    TextButton(onClick = { editing = false; draft = fact.statement }) {
                        Text("算了")
                    }
                }
            } else {
                Row {
                    if (!fact.confirmedByUser) {
                        TextButton(onClick = onConfirm) { Text("对") }
                    }
                    TextButton(onClick = onNegate) { Text("不对") }
                    TextButton(onClick = { editing = true }) { Text("改") }
                }
            }
        }
    }
}
