package ltd.iclab.lifein.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.calendar.CalendarChoice
import ltd.iclab.lifein.calendar.CalendarInfo
import ltd.iclab.lifein.calendar.CalendarReader
import ltd.iclab.lifein.net.CollectionStateDto
import ltd.iclab.lifein.net.CollectorStatus
import ltd.iclab.lifein.net.PendingDto
import ltd.iclab.lifein.net.TodoDto
import ltd.iclab.lifein.ui.theme.EmptyState
import ltd.iclab.lifein.ui.theme.LoadingState
import ltd.iclab.lifein.ui.theme.NoticeBanner
import ltd.iclab.lifein.ui.theme.SectionCard
import ltd.iclab.lifein.ui.theme.Space
import ltd.iclab.lifein.ui.theme.StatTile
import ltd.iclab.lifein.ui.theme.StatusChip
import ltd.iclab.lifein.ui.theme.Tone
import ltd.iclab.lifein.work.Schedules

/**
 * 三个页面:今天、待确认、我的。
 *
 * 界面要回答三个问题:**今天要干什么、有什么等我点头、这套东西还活着吗**。
 * 每一页的顶栏上都有一个刷新键 —— 这个 App 不接收推送(ADR-014),
 * 数据是打开时拉的,**所以"我刚在电脑上改了,这儿怎么没变"必须有一个答案**。
 */

/** 每一页共用的顶栏。标题左对齐、动作在右,五页一个样子。 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ScreenBar(
    title: String,
    subtitle: String? = null,
    onRefresh: (() -> Unit)? = null,
    actions: @Composable () -> Unit = {},
) {
    TopAppBar(
        title = {
            Column {
                Text(title, style = MaterialTheme.typography.titleLarge)
                if (subtitle != null) {
                    Text(
                        subtitle,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
        actions = {
            actions()
            if (onRefresh != null) {
                IconButton(onClick = onRefresh) { Icon(Icons.Default.Refresh, "刷新") }
            }
        },
        colors = TopAppBarDefaults.topAppBarColors(
            containerColor = MaterialTheme.colorScheme.background,
        ),
    )
}

/** 连不上服务端那一条。**措辞统一在这里** —— 五个页面各写一句会有五种说法。 */
@Composable
private fun OfflineNotice(message: String, onRetry: () -> Unit) {
    NoticeBanner(
        "连不上服务端:$message",
        tone = Tone.Problem,
        icon = Icons.Default.Warning,
        action = "重试" to onRetry,
    )
}

// ---------------------------------------------------------------- 今天

@Composable
fun TodosScreen(repo: Repository) {
    var todos by remember { mutableStateOf<List<TodoDto>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var adding by remember { mutableStateOf(false) }
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

    Scaffold(
        topBar = {
            ScreenBar(
                title = "今天",
                subtitle = todos?.let { "${it.size} 件事等着" },
                onRefresh = { scope.launch { refresh() } },
            )
        },
        floatingActionButton = {
            FloatingActionButton(onClick = { adding = true }) {
                Icon(Icons.Default.Add, "加一条待办")
            }
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { insets ->
        Column(Modifier.fillMaxSize().padding(insets)) {
            error?.let {
                Row(Modifier.padding(horizontal = Space.lg, vertical = Space.sm)) {
                    OfflineNotice(it) { scope.launch { refresh() } }
                }
            }

            when (val list = todos) {
                null -> LoadingState()
                else -> if (list.isEmpty()) {
                    EmptyState(
                        Icons.Default.DateRange,
                        "今天没有安排",
                        "邮件和日历里的日程会自己进来。想手动加一条,点右下角那个加号。",
                    )
                } else {
                    LazyColumn(
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(
                            start = Space.lg, end = Space.lg, top = Space.sm, bottom = 88.dp
                        ),
                        verticalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
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
    }

    if (adding) {
        AddTodoDialog(
            onSubmit = { title ->
                scope.launch {
                    runCatching { repo.addTodo(title) }.onFailure { error = it.message }
                    adding = false
                    refresh()
                }
            },
            onDismiss = { adding = false },
        )
    }
}

/**
 * 一条待办。
 *
 * **「完成」做成一个圆形的图标按钮放在左边,不是一个文字按钮放在下面。**
 * 它是这一行上出现频率最高的动作,而放在左边意味着一屏七八条时,
 * 手指走的是一条直线。
 */
@Composable
private fun TodoRow(todo: TodoDto, onDone: () -> Unit, onCancel: () -> Unit) {
    SectionCard {
        Row(
            horizontalArrangement = Arrangement.spacedBy(Space.md),
            verticalAlignment = Alignment.Top,
        ) {
            IconButton(onClick = onDone, modifier = Modifier.size(28.dp)) {
                Icon(
                    Icons.Default.CheckCircle,
                    "完成",
                    tint = MaterialTheme.colorScheme.primary,
                )
            }
            Column(
                Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(Space.xs),
            ) {
                Text(todo.title, style = MaterialTheme.typography.bodyLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
                    todo.startsAt?.let { StatusChip(shortTime(it)) }
                    if (todo.awaitingCalendar) {
                        // ADR-020 的那条"看得见的延迟":服务端记了要写日历,设备还没写进去。
                        // 这一行不显示的话,"以为写进日历了其实没有"就变成了静默丢失
                        StatusChip("还没写进日历", Tone.Problem)
                    }
                }
            }
            TextButton(onClick = onCancel) { Text("撤销") }
        }
    }
}

@Composable
private fun AddTodoDialog(onSubmit: (String) -> Unit, onDismiss: () -> Unit) {
    var draft by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("加一条待办") },
        text = {
            OutlinedTextField(
                value = draft,
                onValueChange = { draft = it },
                label = { Text("要做什么") },
                modifier = Modifier.fillMaxWidth(),
            )
        },
        confirmButton = {
            Button(enabled = draft.isNotBlank(), onClick = { onSubmit(draft.trim()) }) {
                Text("加上")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("算了") } },
    )
}

// ---------------------------------------------------------------- 待确认

@Composable
fun PendingScreen(repo: Repository, onCount: (Int) -> Unit = {}) {
    var items by remember { mutableStateOf<List<PendingDto>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        runCatching { repo.pending() }
            .onSuccess {
                items = it
                error = null
                onCount(it.size)
            }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { refresh() }

    Scaffold(
        topBar = {
            ScreenBar(
                title = "待确认",
                subtitle = "拿不准的都在这儿,不确认就不会写进你的待办和账本",
                onRefresh = { scope.launch { refresh() } },
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { insets ->
        Column(Modifier.fillMaxSize().padding(insets)) {
            error?.let {
                Row(Modifier.padding(horizontal = Space.lg, vertical = Space.sm)) {
                    OfflineNotice(it) { scope.launch { refresh() } }
                }
            }

            when (val list = items) {
                null -> LoadingState()
                else -> if (list.isEmpty()) {
                    EmptyState(
                        Icons.Default.CheckCircle,
                        "没有要确认的",
                        "拿不准的东西才会到这儿来(铁律 7)。空着说明它这段时间都很有把握。",
                    )
                } else {
                    LazyColumn(
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(
                            start = Space.lg, end = Space.lg, top = Space.sm, bottom = Space.xl
                        ),
                        verticalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
                        items(list, key = { it.id }) { item ->
                            PendingRow(
                                item = item,
                                onConfirm = { edits ->
                                    scope.launch { repo.confirm(item.id, edits); refresh() }
                                },
                                onReject = { scope.launch { repo.reject(item.id); refresh() } },
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PendingRow(
    item: PendingDto,
    onConfirm: (Map<String, String>?) -> Unit,
    onReject: () -> Unit,
) {
    // 账目上改过的那几项。**只在这一行的生命周期里存在** ——
    // 点了确认就随刷新一起没了,而没点确认的修改本来就不该留下
    var kind by remember(item.id) { mutableStateOf(item.txnKind) }
    var category by remember(item.id) { mutableStateOf(item.category) }

    SectionCard {
        Text(item.headline, style = MaterialTheme.typography.bodyLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
            StatusChip(item.agent, Tone.Neutral)
            item.startsAt?.let { StatusChip(shortTime(it)) }
        }
        Text(
            item.reason,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (item.isTransaction) {
            HorizontalDivider(Modifier.padding(vertical = Space.xs))
            TransactionEditor(
                kind = kind,
                category = category,
                onKind = { kind = it },
                onCategory = { category = it },
            )
        }

        if (item.isKnown) {
            Row(
                Modifier.fillMaxWidth().padding(top = Space.xs),
                horizontalArrangement = Arrangement.spacedBy(Space.sm),
            ) {
                Button(
                    onClick = {
                        onConfirm(if (item.isTransaction) edits(item, kind, category) else null)
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("确认") }
                OutlinedButton(onClick = onReject, modifier = Modifier.weight(1f)) { Text("不对") }
            }
        } else {
            // 不认识的 target_table 只展示不给按钮(06 §6.7):
            // 服务端加一类待确认不必等 App 发版,而老版本也不会拿错误的形状去确认
            NoticeBanner(
                "这类(${item.targetTable})要新版本的 App 才能处理",
                tone = Tone.Attention,
            )
        }
    }
}

/**
 * 改过的那几项,没改就返回 null(原样确认)。
 *
 * **只送改过的**:全量送回去和原样确认在服务端是两种状态(`edited` 和
 * `confirmed`),而"用户到底动没动过"正是评测集要区分的那件事。
 */
private fun edits(item: PendingDto, kind: String?, category: String?): Map<String, String>? {
    val changed = mutableMapOf<String, String>()
    if (kind != null && kind != item.txnKind) changed["kind"] = kind
    if (category != null && category != item.category) changed["category"] = category
    return changed.ifEmpty { null }
}

/**
 * 账目能改的就两项:**是哪种资金变动、算哪一类支出**。
 *
 * 金额、方向、时间不给改(06 §6.7)—— 它们是规则从原文里抠出来的,
 * 而这条进队列的原因是"这是不是一笔支出说不准",不是"钱数说不准"。
 * 数抠错了正确的动作是点「不对」,然后手动补一笔。
 */
@Composable
private fun TransactionEditor(
    kind: String?,
    category: String?,
    onKind: (String) -> Unit,
    onCategory: (String) -> Unit,
) {
    Text(
        "这是",
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    Row(
        Modifier.horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(Space.xs),
    ) {
        PendingDto.KINDS.forEach { (value, label) ->
            FilterChip(
                selected = kind == value,
                onClick = { onKind(value) },
                label = { Text(label) },
            )
        }
    }

    // 只有支出才有分类。收入、转账、还款给分类没有意义,而报表只统计支出 ——
    // 给它们配一个分类会让人以为那笔钱进了那个类目的统计
    if (kind == "expense") {
        Text(
            "算在",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(Space.xs),
        ) {
            PendingDto.CATEGORIES.forEach { name ->
                FilterChip(
                    selected = category == name,
                    onClick = { onCategory(name) },
                    label = { Text(name) },
                )
            }
        }
    }
}

// ---------------------------------------------------------------- 我的

/**
 * 状态页 —— **这个 App 里唯一一处能回答"它还活着吗"的地方**,
 * 也是关掉采集、删数据、解绑设备的地方。
 *
 * 顶上三块数字回答"活着吗",下面按"你的数据 → 采集范围 → 这台设备"三段排。
 * **顺序不是随便的**:一个人打开这一页,最可能的原因是"我想少采一点"
 * 或者"我想停下来",而那两件事在最上面。
 */
@Composable
fun StatusScreen(
    repo: Repository,
    localListenerEnabled: Boolean,
    queued: Int,
    checkCalendarPermission: () -> Boolean,
    onOpenListenerSettings: () -> Unit,
    onRequestCalendar: () -> Unit,
    onOpenUrl: (String) -> Unit,
    onUnenroll: () -> Unit,
) {
    var status by remember { mutableStateOf<CollectorStatus?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var newSource by remember { mutableStateOf("") }
    var note by remember { mutableStateOf<String?>(null) }
    // 点完"授权"之后加一,好让下面那句重新问一次系统
    var permissionTick by remember { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()
    val context = LifeInApp.instance
    val hasCalendarPermission = remember(permissionTick) { checkCalendarPermission() }

    suspend fun refresh() {
        runCatching { repo.collectorStatus() }
            .onSuccess { status = it; error = null }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { refresh() }

    Scaffold(
        topBar = { ScreenBar("我的", onRefresh = { scope.launch { refresh() } }) },
        containerColor = MaterialTheme.colorScheme.background,
    ) { insets ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(insets)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = Space.lg),
            verticalArrangement = Arrangement.spacedBy(Space.md),
        ) {
            if (!localListenerEnabled) {
                NoticeBanner(
                    "没有通知使用权,采集器一条都收不到。",
                    tone = Tone.Problem,
                    icon = Icons.Default.Warning,
                    action = "去开" to onOpenListenerSettings,
                )
            }
            error?.let { OfflineNotice(it) { scope.launch { refresh() } } }
            note?.let { NoticeBanner(it, tone = Tone.Neutral) }

            Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
                StatTile(
                    label = "通知监听",
                    value = if (localListenerEnabled) "已开启" else "没权限",
                    note = "手机这一侧的事实,不依赖网络",
                    tone = if (localListenerEnabled) Tone.Positive else Tone.Problem,
                    modifier = Modifier.weight(1f),
                )
                StatTile(
                    label = "待上报",
                    value = "$queued 条",
                    note = if (queued > 0) "攒着,联网了会送出去" else "都送出去了",
                    tone = Tone.Neutral,
                    modifier = Modifier.weight(1f),
                )
            }

            CollectionControls(repo) { note = it }

            SectionCard("这台手机的心跳") {
                val devices = status?.devices.orEmpty()
                if (devices.isEmpty()) {
                    Text(
                        "服务端还没收到过心跳。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                devices.forEach { device ->
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(device.deviceId, style = MaterialTheme.typography.bodyMedium)
                            Text(
                                "最后心跳 ${device.lastSeenAt ?: "从没有过"}",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        StatusChip(
                            if (device.stale) "掉线" else "正常",
                            if (device.stale) Tone.Problem else Tone.Positive,
                        )
                    }
                }
                CollectorState.lastUpload(context)?.let {
                    Text(
                        "上次上报:$it",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            CalendarSection(hasCalendarPermission) {
                onRequestCalendar()
                permissionTick++
            }

            SectionCard("放行的来源") {
                Text(
                    "默认拒绝:不在这份名单上的一律丢掉。停用不会删掉那一行 —— " +
                        "留着才回答得了「曾经放行过谁」。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (status?.whitelist?.isEmpty() == true) {
                    NoticeBanner("一条都没有 —— 采集器送上去的东西全会被丢掉", Tone.Problem)
                }
                status?.whitelist?.forEach { rule ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(rule.pattern, style = MaterialTheme.typography.bodyMedium)
                            Text(
                                rule.purpose,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        // 只能开关,**没有删除**:留着那一行才回答得了"曾经放行过谁"(06 §6.9)
                        Switch(
                            checked = rule.enabled,
                            onCheckedChange = { on ->
                                scope.launch { repo.toggleSource(rule.id, on); refresh() }
                            },
                        )
                    }
                }
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(Space.sm),
                ) {
                    OutlinedTextField(
                        value = newSource,
                        onValueChange = { newSource = it },
                        label = { Text("加一个包名") },
                        placeholder = { Text("com.tencent.mm") },
                        singleLine = true,
                        modifier = Modifier.weight(1f),
                    )
                    Button(
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
                    "只放消息类。银行与支付类要在服务器上单独开。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            SectionCard("在电脑上打开") {
                Text(
                    "会生成一条十五分钟有效的链接,在浏览器里能导出数据、加设备、" +
                        "改放行的来源。不需要密码 —— 那条链接本身就是钥匙,所以别转发它。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            runCatching { repo.consoleLink() }
                                .onSuccess(onOpenUrl)
                                .onFailure { note = "打不开:${it.message}" }
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("在浏览器打开") }
            }

            SectionCard("这台设备") {
                OutlinedButton(onClick = onUnenroll, modifier = Modifier.fillMaxWidth()) {
                    Text("解绑这台设备")
                }
                Text(
                    "解绑只清掉手机上这份。服务端那两条凭据还有效 —— " +
                        "手机丢了要在控制台上吊销这一台,或者跑 revoke-device。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Column(Modifier.padding(bottom = Space.xl)) {
                CollectorState.lastError(context)?.let {
                    Text(
                        "最近一次失败:$it",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        }
    }
}

/**
 * 读哪几个日历(06 §6.14)。**默认一个都不读。**
 *
 * 手机上常有生日、节假日、订阅的球赛、公司全员会 —— 全读会把摘要淹掉,
 * 而淹掉的摘要等于没有摘要。和放行来源是同一条思路(R10):
 * 默认拒绝、用户显式放行、随时能改。
 *
 * **勾完立刻读一遍**,不等那两小时的周期任务:勾了却什么都不发生,
 * 会让人以为这个开关没生效。
 */
@Composable
private fun CalendarSection(hasPermission: Boolean, onRequestPermission: () -> Unit) {
    val context = LifeInApp.instance
    var calendars by remember { mutableStateOf<List<CalendarInfo>?>(null) }
    var chosen by remember { mutableStateOf(CalendarChoice.selected(context)) }

    LaunchedEffect(hasPermission) {
        calendars = runCatching { CalendarReader.list(context) }.getOrDefault(emptyList())
    }

    SectionCard("读哪几个日历") {
        Text(
            "默认一个都不读。全读会把摘要淹掉,而淹掉的摘要等于没有摘要。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (!hasPermission) {
            NoticeBanner(
                "没有日历权限,日程写不进系统日历,也读不到你的日历。",
                tone = Tone.Attention,
                action = "授权" to onRequestPermission,
            )
            return@SectionCard
        }

        when {
            calendars == null -> Text(
                "正在看手机上有哪些日历…",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            calendars!!.isEmpty() -> Text(
                "这台手机上一个日历都没有。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            else -> {
                if (chosen.isEmpty()) {
                    NoticeBanner(
                        "一个都没勾 —— 日程不会进摘要,也不会被提取成待办",
                        tone = Tone.Attention,
                    )
                }
                calendars!!.forEach { calendar ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(
                            checked = calendar.id in chosen,
                            onCheckedChange = { on ->
                                chosen = if (on) chosen + calendar.id else chosen - calendar.id
                                CalendarChoice.choose(context, chosen)
                                // 勾完立刻读一遍 —— 见这个函数的说明
                                Schedules.collectCalendarNow(context)
                            },
                        )
                        Column(Modifier.weight(1f)) {
                            Text(
                                calendar.name.ifBlank { "(没有名字)" },
                                style = MaterialTheme.typography.bodyMedium,
                            )
                            if (calendar.account.isNotBlank()) {
                                Text(
                                    calendar.account,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                }
                Text(
                    "只读,不改。App 自己写进日历的那些日程不会被读回来 —— " +
                        "否则一条日程会变成两条、四条。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
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
fun CollectionControls(repo: Repository, onNote: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var state by remember { mutableStateOf<CollectionStateDto?>(null) }
    var confirmingDelete by remember { mutableStateOf(false) }

    suspend fun refresh() {
        runCatching { repo.collectionState() }.onSuccess { state = it }
    }

    LaunchedEffect(Unit) { refresh() }

    SectionCard(
        title = "你的数据",
        trailing = {
            StatusChip(
                if (state?.enabled == true) "正在采集" else "没有在采集",
                if (state?.enabled == true) Tone.Positive else Tone.Attention,
            )
        },
    ) {
        Text(
            "关掉采集不会删数据,删数据也不会自动关掉采集 —— 两件事分开。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
            if (state?.enabled == true) {
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            runCatching { repo.stopCollection() }
                                .onSuccess { onNote(it.note) }
                                .onFailure { onNote("没关成:${it.message}") }
                            refresh()
                        }
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("关掉采集") }
            }
            OutlinedButton(
                onClick = { confirmingDelete = true },
                modifier = Modifier.weight(1f),
            ) {
                Icon(Icons.Default.Delete, null, Modifier.size(16.dp))
                Text(" 删掉已采的")
            }
        }
    }

    if (confirmingDelete) {
        AlertDialog(
            onDismissRequest = { confirmingDelete = false },
            icon = { Icon(Icons.Default.Warning, null) },
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
                            .onSuccess { onNote("删掉了 ${it.total} 条") }
                            .onFailure { onNote("没删成:${it.message}") }
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

/** ISO 时间只留到分钟。**列表上没人读秒和时区** —— 而它们会把一行挤到换行。 */
internal fun shortTime(iso: String): String =
    iso.take(16).replace('T', ' ')
