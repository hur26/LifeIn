package ltd.iclab.lifein.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.ShoppingCart
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Tab
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import ltd.iclab.lifein.collect.ReceiptScanner
import ltd.iclab.lifein.net.BudgetDto
import ltd.iclab.lifein.net.ManualTxnBody
import ltd.iclab.lifein.net.MonthlyReportDto
import ltd.iclab.lifein.net.TransactionDto
import ltd.iclab.lifein.ui.theme.EmptyState
import ltd.iclab.lifein.ui.theme.MeterBar
import ltd.iclab.lifein.ui.theme.NoticeBanner
import ltd.iclab.lifein.ui.theme.SectionCard
import ltd.iclab.lifein.ui.theme.Space
import ltd.iclab.lifein.ui.theme.StatusChip
import ltd.iclab.lifein.ui.theme.Tone
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter

/**
 * 账本、报表与预算(P2 第 13 片,06 §6.11)。
 *
 * **金额从头到尾是字符串。** 服务端发的是字符串,这边显示的也是字符串 ——
 * 中间一次都不转成 Double。`38.50` 经过一次 Double 会变成 `38.499999999999996`,
 * 而账本上出现那个数字比出现一笔错账更让人不信任。
 *
 * ## 排版上做的一件事:金额右对齐
 *
 * 一屏十几笔账,人扫的是**那一列数字**,不是商户名。左对齐的金额在小数点上
 * 对不齐,而对不齐的一列数字要一个个读 —— 右对齐之后"这个月哪笔最大"
 * 是一眼的事。
 *
 * ## 改分类那个动作比它看起来重要
 *
 * 点一下改分类,服务端会把它写回商户规则表并标成 `created_by='user'` ——
 * 以后这个商户就一直归到这一类,**而且模型改不回去**
 * ([ADR-008](../../../../../../../docs/04-tech-decisions.md#adr-008--账单归类用规则llm-混合))。
 * 这是整个归类链路里最准的一条输入,所以它做成列表里点一下就能改,
 * 而不是藏在详情页第二层。
 *
 * ## 手动补一笔在这里,不在别处
 *
 * 现金和纸质票据那条长尾,实时通知那一路永远采不到。它和账本在同一个界面,
 * 是因为**想补一笔的时刻就是发现账本上少了一笔的时刻**。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LedgerScreen(repo: Repository) {
    var tab by remember { mutableIntStateOf(0) }
    val titles = listOf("账目", "报表", "预算")

    Scaffold(
        topBar = {
            Column {
                TopAppBar(
                    title = { Text("账本", style = MaterialTheme.typography.titleLarge) },
                    colors = TopAppBarDefaults.topAppBarColors(
                        containerColor = MaterialTheme.colorScheme.background,
                    ),
                )
                PrimaryTabRow(
                    selectedTabIndex = tab,
                    containerColor = MaterialTheme.colorScheme.background,
                ) {
                    titles.forEachIndexed { index, title ->
                        Tab(
                            selected = tab == index,
                            onClick = { tab = index },
                            text = { Text(title) },
                        )
                    }
                }
            }
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { insets ->
        Column(Modifier.fillMaxSize().padding(insets)) {
            when (tab) {
                0 -> TransactionsTab(repo)
                1 -> ReportTab(repo)
                else -> BudgetsTab(repo)
            }
        }
    }
}

// ---------------------------------------------------------------- 账目

@Composable
private fun TransactionsTab(repo: Repository) {
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<TransactionDto>?>(null) }
    var query by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var editing by remember { mutableStateOf<TransactionDto?>(null) }
    var adding by remember { mutableStateOf(false) }

    suspend fun reload() {
        runCatching { repo.transactions(query = query.ifBlank { null }) }
            .onSuccess { items = it; error = null }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { reload() }

    Scaffold(
        floatingActionButton = {
            FloatingActionButton(onClick = { adding = true }) {
                Icon(Icons.Default.Add, "手动补一笔")
            }
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { _ ->
        Column(Modifier.fillMaxSize()) {
            OutlinedTextField(
                value = query,
                onValueChange = {
                    query = it
                    scope.launch { reload() }
                },
                label = { Text("找商户") },
                leadingIcon = { Icon(Icons.Default.Search, null) },
                singleLine = true,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = Space.lg, vertical = Space.sm),
            )
            error?.let {
                Row(Modifier.padding(horizontal = Space.lg, vertical = Space.xs)) {
                    NoticeBanner("读不到账本:$it", Tone.Problem)
                }
            }

            when (val list = items) {
                null -> ltd.iclab.lifein.ui.theme.LoadingState()
                else -> if (list.isEmpty()) {
                    EmptyState(
                        Icons.Default.ShoppingCart,
                        if (query.isBlank()) "账本还是空的" else "没找到这个商户",
                        if (query.isBlank()) {
                            "放行了支付类通知之后,消费会自己记进来。现金和纸质小票点右下角补。"
                        } else {
                            "换个词试试,或者清空搜索看全部。"
                        },
                    )
                } else {
                    LazyColumn(
                        contentPadding = PaddingValues(
                            start = Space.lg, end = Space.lg, bottom = 88.dp
                        ),
                        verticalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
                        items(list, key = { it.id }) { txn ->
                            TransactionRow(
                                txn = txn,
                                onEdit = { editing = txn },
                                onDelete = {
                                    scope.launch {
                                        runCatching { repo.deleteTransaction(txn.id) }
                                        reload()
                                    }
                                },
                            )
                        }
                    }
                }
            }
        }
    }

    editing?.let { txn ->
        CategoryDialog(
            current = txn.category,
            onPick = { picked ->
                scope.launch {
                    runCatching { repo.recategorize(txn.id, picked) }
                    editing = null
                    reload()
                }
            },
            onDismiss = { editing = null },
        )
    }

    if (adding) {
        ManualEntryDialog(
            onSubmit = { body ->
                scope.launch {
                    runCatching { repo.addTransaction(body) }.onFailure { error = it.message }
                    adding = false
                    reload()
                }
            },
            onDismiss = { adding = false },
        )
    }
}

@Composable
private fun TransactionRow(txn: TransactionDto, onEdit: () -> Unit, onDelete: () -> Unit) {
    SectionCard {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(Space.md),
            verticalAlignment = Alignment.Top,
        ) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(Space.xs)) {
                Text(
                    txn.merchantRaw ?: "(没有商户名)",
                    style = MaterialTheme.typography.bodyLarge,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
                    StatusChip(shortDate(txn.occurredAt))
                    StatusChip(
                        txn.category ?: "未归类",
                        if (txn.category == null) Tone.Attention else Tone.Neutral,
                    )
                    if (txn.kind != "expense") StatusChip(kindLabel(txn.kind))
                    // 对账过的标出来:它意味着这一笔和银行账单核对过
                    if (txn.stage == "reconciled") StatusChip("已对账", Tone.Positive)
                }
            }
            // 金额原样显示,一次都不转 Double
            Text(
                if (txn.direction == "credit") "+${txn.amount}" else txn.amount,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.SemiBold,
                color = if (txn.direction == "credit") {
                    MaterialTheme.colorScheme.primary
                } else {
                    MaterialTheme.colorScheme.onSurface
                },
            )
        }
        Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
            TextButton(onClick = onEdit) { Text("改分类") }
            TextButton(onClick = onDelete) { Text("删掉") }
        }
    }
}

// ---------------------------------------------------------------- 报表

@Composable
private fun ReportTab(repo: Repository) {
    var report by remember { mutableStateOf<MonthlyReportDto?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        runCatching { repo.monthlyReport() }
            .onSuccess { report = it; error = null }
            .onFailure { error = it.message }
    }

    val data = report
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = Space.lg),
        verticalArrangement = Arrangement.spacedBy(Space.md),
    ) {
        error?.let { NoticeBanner("读不到报表:$it", Tone.Problem) }
        if (data == null) {
            EmptyState(
                Icons.Default.ShoppingCart,
                "还没有报表",
                "月报是每月初那次定时任务算出来的,不是现调模型 —— 所以同一个月每次看都一样。",
            )
            return@Column
        }

        SectionCard(data.period) {
            Row(verticalAlignment = Alignment.Bottom) {
                Text("¥", style = MaterialTheme.typography.titleMedium)
                Text(data.total, style = MaterialTheme.typography.headlineMedium)
            }
            Text(
                "${data.count} 笔支出" + (data.lastTotal?.let { " · 上个月 ¥$it" } ?: ""),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            // 评语来自月报 job 上次跑的结果,不是现调模型(06 §6.11)
            data.notes.forEach {
                Text("· $it", style = MaterialTheme.typography.bodyMedium)
            }
        }

        SectionCard("花在哪儿") {
            if (data.categories.isEmpty()) {
                Text(
                    "这个月还没有归好类的支出。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            val top = data.categories.maxOfOrNull { amountOf(it.total) } ?: 1.0
            data.categories.forEach { line ->
                Column(verticalArrangement = Arrangement.spacedBy(Space.xs)) {
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Text(
                            "${line.category}(${line.count} 笔)",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text("¥${line.total}", style = MaterialTheme.typography.bodyMedium)
                    }
                    // 条形按"这个月最大的那一类"归一。按总额归一的话,
                    // 一个花得少的月份里每根条都短得看不出差别
                    MeterBar((amountOf(line.total) / top).toFloat(), Tone.Neutral)
                }
            }
        }

        if (data.uncategorized != "0" && data.uncategorized != "0.00") {
            // 单独说:混进"其他"的话,报表会声称自己看懂了这些钱
            NoticeBanner(
                "还有 ¥${data.uncategorized} 没归类。它们不在上面那几条里 —— " +
                    "混进「其他」的话,这份报表会声称自己看懂了这些钱。",
                tone = Tone.Attention,
            )
        }

        SectionCard("这份报表可不可信") {
            Text(
                coverageLine(data.reconciledRatio),
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                "覆盖率低意味着有些消费根本没进账本,而报表本身看不出这一点。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        Column(Modifier.padding(bottom = Space.xl)) {}
    }
}

// ---------------------------------------------------------------- 预算

@Composable
private fun BudgetsTab(repo: Repository) {
    val scope = rememberCoroutineScope()
    var items by remember { mutableStateOf<List<BudgetDto>?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var editing by remember { mutableStateOf(false) }

    suspend fun reload() {
        runCatching { repo.budgets() }
            .onSuccess { items = it; error = null }
            .onFailure { error = it.message }
    }

    LaunchedEffect(Unit) { reload() }

    Scaffold(
        floatingActionButton = {
            FloatingActionButton(onClick = { editing = true }) {
                Icon(Icons.Default.Add, "设一条预算")
            }
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { _ ->
        Column(Modifier.fillMaxSize().padding(horizontal = Space.lg)) {
            error?.let { NoticeBanner("读不到预算:$it", Tone.Problem) }

            when (val list = items) {
                null -> ltd.iclab.lifein.ui.theme.LoadingState()
                else -> if (list.isEmpty()) {
                    // 没设预算就不会有超支预警 —— 说清楚,别让人以为功能坏了
                    EmptyState(
                        Icons.Default.ShoppingCart,
                        "还没设预算",
                        "没有预算就不会有超支提醒。点右下角设一条,不选类目就是总预算。",
                    )
                } else {
                    LazyColumn(
                        contentPadding = PaddingValues(bottom = 88.dp),
                        verticalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
                        items(list, key = { it.category ?: "__total__" }) { budget ->
                            BudgetRow(budget) {
                                scope.launch {
                                    runCatching { repo.deleteBudget(budget.category) }
                                    reload()
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if (editing) {
        BudgetDialog(
            onSubmit = { category, amount ->
                scope.launch {
                    runCatching { repo.setBudget(category, amount) }
                        .onFailure { error = it.message }
                    editing = false
                    reload()
                }
            },
            onDismiss = { editing = false },
        )
    }
}

@Composable
private fun BudgetRow(budget: BudgetDto, onDelete: () -> Unit) {
    // 超了和快到了是两件事,颜色和措辞都分开
    val tone = when {
        budget.over -> Tone.Problem
        budget.near -> Tone.Attention
        else -> Tone.Positive
    }
    SectionCard(
        title = budget.category ?: "总预算",
        trailing = {
            StatusChip(
                when {
                    budget.over -> "已超支"
                    budget.near -> "快到了"
                    else -> "还宽裕"
                },
                tone,
            )
        },
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text("¥${budget.spent}", style = MaterialTheme.typography.titleMedium)
            Text(
                "／ ¥${budget.amount}",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        MeterBar(ratio(budget), tone)
        Text(
            when {
                budget.over -> "已超支 ¥${budget.remaining.removePrefix("-")}"
                budget.near -> "还剩 ¥${budget.remaining}"
                else -> "还剩 ¥${budget.remaining}"
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        TextButton(onClick = onDelete) { Text("删掉这条") }
    }
}

// ---------------------------------------------------------------- 对话框

@Composable
private fun CategoryDialog(current: String?, onPick: (String) -> Unit, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("算了") } },
        title = { Text("归到哪一类") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(Space.sm)) {
                Text(
                    "改一次以后这个商户就一直归到这一类,而且模型改不回去。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                CategoryChips(selected = current, onPick = onPick)
            }
        },
    )
}

/** 分类选择器。**三个对话框共用** —— 各写一份的话,加一个类目要改三处。 */
@Composable
private fun CategoryChips(selected: String?, onPick: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(Space.xs)) {
        CATEGORIES.chunked(4).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(Space.xs)) {
                row.forEach { name ->
                    FilterChip(
                        selected = name == selected,
                        onClick = { onPick(name) },
                        label = { Text(name) },
                    )
                }
            }
        }
    }
}

/**
 * 手动补一笔。**拍小票只是把这个表单先填上几格**,不是另一条路 ——
 * 识别出来的东西一律要人看一眼再点"记上",而不是直接入账。
 *
 * 小票上"实付"和"原价"、桌号和金额长得一样近,而
 * [03 那条"误记率 = 0"](../../../../../../../docs/03-roadmap.md)
 * 不区分错误来自模型还是来自 OCR。
 */
@Composable
private fun ManualEntryDialog(onSubmit: (ManualTxnBody) -> Unit, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var amount by remember { mutableStateOf("") }
    var merchant by remember { mutableStateOf("") }
    var category by remember { mutableStateOf<String?>(null) }
    var scanNote by remember { mutableStateOf<String?>(null) }

    val pickPhoto = rememberLauncherForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            // 认不出来不是异常:小票拍糊、光线不好都是日常,
            // 弹错误对话框只会让人下次不再用这个功能
            val parsed = runCatching { ReceiptScanner(context).scan(uri) }.getOrNull()
            if (parsed?.useful == true) {
                amount = parsed.amount!!.toPlainString()
                parsed.merchant?.let { merchant = it }
                scanNote = "认出来了,核对一下再记上"
            } else {
                scanNote = "没认出金额,自己填一下"
            }
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("手动补一笔") },
        text = {
            Column(
                Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(Space.sm),
            ) {
                OutlinedButton(
                    onClick = { pickPhoto.launch("image/*") },
                    modifier = Modifier.fillMaxWidth(),
                ) { Text("拍 / 选一张小票") }
                scanNote?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                OutlinedTextField(
                    value = amount,
                    onValueChange = { amount = it },
                    label = { Text("金额") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = merchant,
                    onValueChange = { merchant = it },
                    label = { Text("在哪花的") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                CategoryChips(selected = category, onPick = { category = it })
            }
        },
        confirmButton = {
            Button(
                enabled = amount.isNotBlank(),
                onClick = {
                    onSubmit(
                        ManualTxnBody(
                            // 带时区:无时区的时间会让一笔深夜的消费落到前一天,
                            // 而月度报表按天切
                            occurredAt = OffsetDateTime.now()
                                .format(DateTimeFormatter.ISO_OFFSET_DATE_TIME),
                            amount = amount.trim(),
                            merchantRaw = merchant.ifBlank { null },
                            category = category,
                        )
                    )
                },
            ) { Text("记上") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("算了") } },
    )
}

@Composable
private fun BudgetDialog(onSubmit: (String?, String) -> Unit, onDismiss: () -> Unit) {
    var amount by remember { mutableStateOf("") }
    var category by remember { mutableStateOf<String?>(null) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("每月能花多少") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(Space.sm)) {
                OutlinedTextField(
                    value = amount,
                    onValueChange = { amount = it },
                    label = { Text("金额") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    "不选类目就是总预算。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                CategoryChips(
                    selected = category,
                    onPick = { category = if (category == it) null else it },
                )
            }
        },
        confirmButton = {
            Button(
                enabled = amount.isNotBlank(),
                onClick = { onSubmit(category, amount.trim()) },
            ) { Text("设好") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("算了") } },
    )
}

/**
 * 分类的封闭枚举。**和服务端那份必须一致** ——
 * 这边多一个的话,选了会被 422 挡回来,而用户看到的是"记不上,不知道为什么"。
 *
 * 不从服务端拉:它一年也变不了一次,而为它多一个接口就多一处启动时会失败的地方。
 * 加类目时两边一起改,和验证码正则那件事同一个取舍(07 §4)。
 */
private val CATEGORIES = listOf(
    "餐饮", "交通", "购物", "居住", "通信", "娱乐", "医疗", "教育", "人情", "其他",
)

private fun ratio(budget: BudgetDto): Float =
    runCatching {
        val spent = java.math.BigDecimal(budget.spent)
        val total = java.math.BigDecimal(budget.amount)
        if (total.signum() <= 0) 0f else spent.divide(total, 4, java.math.RoundingMode.HALF_UP)
            .toFloat().coerceIn(0f, 1f)
    }.getOrDefault(0f)

/** 只给条形图的长度用。**显示的金额永远走字符串那条路** —— 见文件开头。 */
private fun amountOf(text: String): Double = text.toDoubleOrNull() ?: 0.0

private fun shortDate(iso: String): String =
    runCatching { OffsetDateTime.parse(iso).format(DateTimeFormatter.ofPattern("MM-dd")) }
        .getOrDefault(iso.take(10))

private fun kindLabel(kind: String): String = when (kind) {
    "expense" -> "支出"
    "income" -> "收入"
    "refund" -> "退款"
    "repayment" -> "还款"
    "transfer" -> "转账"
    else -> kind
}

/**
 * 对账覆盖率那一行。**它是"这份报表可不可信"的唯一提示** ——
 * 覆盖率低意味着有些消费根本没进账本,而报表本身看不出这一点。
 */
private fun coverageLine(ratio: Double?): String =
    if (ratio == null) "这个月还没对过账" else "其中 ${(ratio * 100).toInt()}% 已和账单核对过"
