package ltd.iclab.lifein.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import ltd.iclab.lifein.net.FactDto
import ltd.iclab.lifein.net.SourceDto
import ltd.iclab.lifein.ui.theme.EmptyState
import ltd.iclab.lifein.ui.theme.LoadingState
import ltd.iclab.lifein.ui.theme.NoticeBanner
import ltd.iclab.lifein.ui.theme.SectionCard
import ltd.iclab.lifein.ui.theme.Space
import ltd.iclab.lifein.ui.theme.StatusChip
import ltd.iclab.lifein.ui.theme.Tone

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
 *
 * ## 排版上做的一件事:出处不折叠
 *
 * 出处占的地方不小,把它折进一个"查看来源"里能让列表短一半。
 * **没有那么做**,因为一条看得见来源的记忆和一条要点两下才看得见来源的记忆,
 * 在"你信不信它"这件事上差得很远 —— 而这一页的全部意义就是那个信任。
 */
@OptIn(ExperimentalMaterial3Api::class)
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

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("记忆", style = MaterialTheme.typography.titleLarge)
                        Text(
                            "它记住的每一条都带着出处",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { insets ->
        Column(Modifier.fillMaxSize().padding(insets)) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                label = { Text("搜记忆") },
                leadingIcon = { Icon(Icons.Default.Search, null) },
                trailingIcon = {
                    TextButton(onClick = { scope.launch { refresh() } }) { Text("搜") }
                },
                singleLine = true,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = Space.lg, vertical = Space.sm),
            )
            error?.let {
                Row(Modifier.padding(horizontal = Space.lg, vertical = Space.xs)) {
                    NoticeBanner("连不上服务端:$it", Tone.Problem)
                }
            }

            when (val list = facts) {
                null -> LoadingState()
                else -> if (list.isEmpty()) {
                    EmptyState(
                        Icons.Default.Star,
                        if (query.isBlank()) "还没有记忆" else "没搜到",
                        if (query.isBlank()) {
                            "抽取任务每天跑一次。它只从已经采到的东西里抽,不会去猜。"
                        } else {
                            "换个说法试试 —— 它按语义搜,不是按字面。"
                        },
                    )
                } else {
                    LazyColumn(
                        contentPadding = PaddingValues(
                            start = Space.lg, end = Space.lg, bottom = Space.xl
                        ),
                        verticalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
                        items(list, key = { it.id }) { fact ->
                            FactRow(
                                fact = fact,
                                sources = sources,
                                onConfirm = {
                                    scope.launch { repo.confirmFact(fact.id); refresh() }
                                },
                                onNegate = {
                                    scope.launch { repo.negateFact(fact.id); refresh() }
                                },
                                onCorrect = { text ->
                                    scope.launch {
                                        runCatching { repo.correctFact(fact.id, text) }
                                            .onFailure { error = "改不了:${it.message}" }
                                        refresh()
                                    }
                                },
                            )
                        }
                    }
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

    SectionCard(
        trailing = {
            when {
                fact.authoredByUser -> StatusChip("你改的", Tone.Positive)
                fact.confirmedByUser -> StatusChip("你确认过", Tone.Positive)
                else -> StatusChip("置信度 ${"%.1f".format(fact.confidence)}", Tone.Neutral)
            }
        },
    ) {
        Text(fact.statement, style = MaterialTheme.typography.bodyLarge)

        // 出处。**没有来源的记忆不该出现在这里** —— 真出现了就是 provenance 有漏,
        // 那是 P1 的退出条件之一,所以这一行要显眼到不用找
        if (fact.provenance.isEmpty()) {
            NoticeBanner(
                "说不清来源。这条不该存在 —— provenance 链路上有漏。",
                tone = Tone.Problem,
            )
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                fact.provenance.forEach { eventId ->
                    val source = sources[eventId.toString()]
                    Text(
                        "来自:" + (source?.let { "${it.source} · ${it.title ?: "(无标题)"}" }
                            ?: "事件 $eventId"),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        if (editing) {
            OutlinedTextField(
                value = draft,
                onValueChange = { draft = it },
                label = { Text("改成") },
                modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
                Button(
                    enabled = draft.isNotBlank() && draft != fact.statement,
                    onClick = {
                        editing = false
                        onCorrect(draft)
                    },
                ) { Text("保存") }
                TextButton(onClick = { editing = false; draft = fact.statement }) { Text("算了") }
            }
        } else {
            Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
                if (!fact.confirmedByUser) {
                    Button(onClick = onConfirm) { Text("对") }
                }
                OutlinedButton(onClick = onNegate) { Text("不对") }
                TextButton(onClick = { editing = true }) { Text("改") }
            }
        }
    }
}
