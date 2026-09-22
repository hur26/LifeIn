package ltd.iclab.lifein.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.copilot.CHAT_ADAPTERS
import ltd.iclab.lifein.copilot.CopilotHistory
import ltd.iclab.lifein.copilot.CopilotKeepAlive
import ltd.iclab.lifein.copilot.CopilotPrefs
import ltd.iclab.lifein.copilot.CopilotState
import ltd.iclab.lifein.copilot.CopilotWording
import ltd.iclab.lifein.ui.theme.NoticeBanner
import ltd.iclab.lifein.ui.theme.SectionCard
import ltd.iclab.lifein.ui.theme.Space
import ltd.iclab.lifein.ui.theme.StatTile
import ltd.iclab.lifein.ui.theme.StatusChip
import ltd.iclab.lifein.ui.theme.Tone

/**
 * 副驾那一页(P5)。
 *
 * **这一页在做的事只有一件:把四道门变成看得见的四行。**
 * 在它之前,那四道全在代码里 —— 用户装了 App、开了通知采集,
 * 然后发现副驾没反应,而没有任何地方告诉他还差哪一步。
 *
 * 四道门(07 §2.9),**每一道默认都是关的**:
 *
 * 1. 服务端的 `COPILOT_ENABLED`
 * 2. 系统的无障碍权限
 * 3. 系统的悬浮窗权限
 * 4. App 里的总开关 + 逐个放行聊天 App
 *
 * ## 权限状态传的是函数,不是布尔值
 *
 * 和状态页那条日历权限同一个理由,但这里更要紧:用户是**离开这个 App**
 * 去系统设置里开的,回来时没有任何回调。传布尔值的话界面会一直显示
 * "还没开" —— 而那会让他以为自己开错了地方,于是再去开一次。
 *
 * [permissionTick] 在 Activity 每次 `onResume` 时加一,逼这几行重新问一次系统。
 *
 * ## 为什么"它会看到什么"那一段压在放行开关上面
 *
 * 那不是排版,是 09 §1 那条告知义务的落点:**放行某个 App 的那一下,
 * 正是用户真正做出决定的那一刻** —— 说明摆在决定的后面等于没说。
 */
@Composable
fun CopilotScreen(
    permissionTick: Int,
    checkAccessibility: () -> Boolean,
    checkOverlay: () -> Boolean,
    onOpenAccessibilitySettings: () -> Unit,
    onOpenOverlaySettings: () -> Unit,
) {
    val context = LifeInApp.instance
    val prefs = remember { CopilotPrefs(context) }
    val scope = rememberCoroutineScope()

    // 每次从系统设置回来都重问一次。**不缓存** —— 见上面那段
    val accessibilityOn = remember(permissionTick) { checkAccessibility() }
    val overlayOn = remember(permissionTick) { checkOverlay() }
    val serviceConnected = remember(permissionTick) { CopilotState.serviceConnected(context) }
    val diagnosis = remember(permissionTick) { CopilotState.lastDiagnosis(context) }
    val lastCaptureAt = remember(permissionTick) { CopilotState.lastCaptureAt(context) }

    var enabled by remember { mutableStateOf(prefs.enabled) }
    var autoAnalyze by remember { mutableStateOf(prefs.autoAnalyze) }
    var ocrFallback by remember { mutableStateOf(prefs.ocrFallback) }
    var keepHistory by remember { mutableStateOf(prefs.keepHistory) }
    var allowed by remember { mutableStateOf(prefs.allowedApps) }

    var storedMessages by remember { mutableIntStateOf(0) }
    var historyTick by remember { mutableIntStateOf(0) }
    // Room 读在 IO 上。**主线程读库会抛**,而这一行只是为了显示一个数字
    LaunchedEffect(historyTick) {
        storedMessages = withContext(Dispatchers.IO) { CopilotHistory(context).total() }
    }

    Scaffold(
        topBar = {
            ScreenBar(
                title = "副驾",
                subtitle = "读当前聊天窗,给三条候选。发送键始终是你自己按",
            )
        },
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
            // **系统里勾着、服务却没在跑。** 这是被 ROM 的省电策略冻住的样子
            // (ADR-021 的 2026-09-22 追加)—— 而用户去系统设置里看,
            // 那个开关是开着的,于是他会以为一切正常,然后一直等一个不会来的悬浮窗。
            // 这一条不摆出来的话,没有任何地方会说它
            if (accessibilityOn && !serviceConnected) {
                NoticeBanner(
                    "无障碍开着,但读屏服务没在跑 —— 多半被省电策略冻住了。去系统里给 LifeIn 开自启动、关电池优化。",
                    tone = Tone.Problem,
                    icon = Icons.Default.Warning,
                )
            }

            Row(horizontalArrangement = Arrangement.spacedBy(Space.sm)) {
                StatTile(
                    label = "读屏服务",
                    value = if (serviceConnected) "在跑" else "没跑",
                    note = "手机这一侧的事实,不依赖网络",
                    tone = if (serviceConnected) Tone.Positive else Tone.Neutral,
                    modifier = Modifier.weight(1f),
                )
                StatTile(
                    label = "放行的 App",
                    value = "${allowed.size} 个",
                    note = if (allowed.isEmpty()) "一个都没放行,副驾不会读任何东西" else "只读这几个",
                    tone = if (allowed.isEmpty()) Tone.Neutral else Tone.Positive,
                    modifier = Modifier.weight(1f),
                )
            }

            SectionCard("四道门") {
                Text(
                    "四道全通了副驾才会读一个字。每一道默认都是关的。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                // 第一道在服务器上,这个 App 看不到。**如实说"看不到",
                // 不要画一个永远打勾的假对勾** —— 那会让人在真的没开时
                // 对着一个全绿的清单找别的原因
                GateRow(
                    step = 1,
                    title = "服务端打开副驾",
                    detail = "服务器的 COPILOT_ENABLED 要是 true。这一道 App 查不到,没开的话悬浮窗会当场说出来",
                    state = GateState.Unknown,
                )
                GateRow(
                    step = 2,
                    title = "无障碍权限",
                    detail = if (accessibilityOn) {
                        "已开。收回它就能彻底断开副驾,不需要这个 App 配合"
                    } else {
                        "去系统设置里找到 LifeIn 副驾并打开。升级 App 之后要关一次再开,否则截屏兜底不生效"
                    },
                    state = if (accessibilityOn) GateState.Done else GateState.Todo,
                    action = "去开" to onOpenAccessibilitySettings,
                )
                GateRow(
                    step = 3,
                    title = "悬浮窗权限",
                    detail = if (overlayOn) {
                        "已开。副驾的判断和候选显示在这个窗口里"
                    } else {
                        "没有它副驾读到了也没地方显示,所以这时它一个字都不会读"
                    },
                    state = if (overlayOn) GateState.Done else GateState.Todo,
                    action = "去开" to onOpenOverlaySettings,
                )
                GateRow(
                    step = 4,
                    title = "App 里的开关",
                    detail = if (enabled && allowed.isNotEmpty()) {
                        "已打开,并且放行了 ${allowed.size} 个聊天 App"
                    } else if (enabled) {
                        "总开关开了,但一个聊天 App 都没放行"
                    } else {
                        "下面那个总开关"
                    },
                    state = if (enabled && allowed.isNotEmpty()) GateState.Done else GateState.Todo,
                )
            }

            SectionCard("开关") {
                ToggleRow(
                    title = "副驾",
                    detail = "关掉之后不读屏、不出悬浮窗、不发请求",
                    checked = enabled,
                    onChange = {
                        enabled = it
                        prefs.enabled = it
                        // 常驻通知要跟着走。不停的话通知栏里会留一条
                        // 说着"副驾在运行"而副驾其实已经关了的通知
                        runCatching {
                            if (it) CopilotKeepAlive.start(context) else CopilotKeepAlive.stop(context)
                        }
                    },
                )
                ToggleRow(
                    title = "对方发来新消息就自动分析",
                    detail = "关掉之后悬浮球只待命,点一下才分析。一次分析要打三次模型",
                    checked = autoAnalyze,
                    onChange = { autoAnalyze = it; prefs.autoAnalyze = it },
                )
                ToggleRow(
                    title = "读不到字时截屏认一次",
                    detail = "只认气泡那几块,不整屏识别。图片在这台手机上认,不上传",
                    checked = ocrFallback,
                    onChange = { ocrFallback = it; prefs.ocrFallback = it },
                )
                ToggleRow(
                    title = "在这台手机上记对话",
                    detail = "记了才有长上下文,回复质量高不少。只记在手机上,服务器没有副本",
                    checked = keepHistory,
                    onChange = { keepHistory = it; prefs.keepHistory = it },
                )
            }

            CopilotDisclosure()

            SectionCard("放行哪些聊天 App") {
                Text(
                    "默认一个都不放行。没有适配器的 App 永远读不了,有的也要你在这里逐个打开。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                CHAT_ADAPTERS.forEach { adapter ->
                    ToggleRow(
                        title = adapter.label,
                        detail = adapter.pkg,
                        checked = adapter.pkg in allowed,
                        onChange = { on ->
                            prefs.setAppAllowed(adapter.pkg, on)
                            allowed = prefs.allowedApps
                        },
                    )
                }
            }

            SectionCard(
                title = "上一次读屏",
                trailing = {
                    StatusChip(
                        if (diagnosis == CopilotState.OK) "正常" else "看一眼",
                        if (diagnosis == CopilotState.OK) Tone.Positive else Tone.Neutral,
                    )
                },
            ) {
                Text(CopilotWording.diagnosis(diagnosis), style = MaterialTheme.typography.bodyMedium)
                if (lastCaptureAt > 0) {
                    Text(
                        "时间:${formatTime(lastCaptureAt)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            SectionCard(
                title = "本地对话",
                trailing = {
                    // 清空不问服务端 —— 那边从来没有过副本(ADR-038)
                    TextButton(
                        onClick = {
                            scope.launch {
                                withContext(Dispatchers.IO) { CopilotHistory(context).clear() }
                                historyTick++
                            }
                        }
                    ) { Text("清空") }
                },
            ) {
                Text(
                    "这台手机上存着 $storedMessages 条,每段对话最多 300 条。",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    "服务器上没有它的副本,换手机也带不走。清空之后副驾会忘掉之前说过什么。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

/** 一道门的三种样子。**"查不到"是独立的一种**,不能混进"没开"。 */
private enum class GateState { Done, Todo, Unknown }

@Composable
private fun GateRow(
    step: Int,
    title: String,
    detail: String,
    state: GateState,
    action: Pair<String, () -> Unit>? = null,
) {
    Row(
        Modifier.fillMaxWidth().padding(top = Space.xs),
        horizontalArrangement = Arrangement.spacedBy(Space.md),
        verticalAlignment = Alignment.Top,
    ) {
        val scheme = MaterialTheme.colorScheme
        when (state) {
            GateState.Done -> Icon(
                Icons.Default.CheckCircle,
                null,
                Modifier.size(18.dp),
                tint = scheme.primary,
            )

            GateState.Todo -> Icon(
                Icons.Default.Warning,
                null,
                Modifier.size(18.dp),
                tint = scheme.error,
            )

            GateState.Unknown -> Icon(
                Icons.Default.Lock,
                null,
                Modifier.size(18.dp),
                tint = scheme.onSurfaceVariant,
            )
        }
        Column(Modifier.weight(1f)) {
            Text("$step. $title", style = MaterialTheme.typography.bodyMedium)
            Text(
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = scheme.onSurfaceVariant,
            )
        }
        if (action != null && state != GateState.Done) {
            TextButton(onClick = action.second) { Text(action.first) }
        }
    }
}

@Composable
private fun ToggleRow(
    title: String,
    detail: String,
    checked: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = Space.xs),
        horizontalArrangement = Arrangement.spacedBy(Space.md),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyMedium)
            Text(
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

/**
 * 它到底会看到什么。
 *
 * **这一段不是免责声明,是 09 §1 那条告知义务。** 副驾是这个系统里
 * 唯一一个会读到"你自己发出去的话"的东西,也是唯一一个读得到
 * 免打扰的群、读得到完整长消息的东西。那几件事要写在用户按下
 * 放行开关之前,而不是藏在一份文档里。
 */
@Composable
private fun CopilotDisclosure() {
    SectionCard("放行之前,先知道它看得见什么") {
        // 第二项用正文色而不是弱化色。**这不是排版偏好** ——
        // "它会读到你自己说的话"是这几条里唯一一条用户想不到的,
        // 而和别的几条一样淡的话,它会被当成同一类套话一起略过
        listOf(
            "你打开的那个聊天窗里,屏幕上的整段对话 —— 包括免打扰的群" to false,
            "你自己发出去的话。这是这个系统里第一个会读到它们的地方" to true,
            "分析那一秒,这段对话会完整地经过一次外部大模型。它不落服务器的库" to false,
            "验证码会在上传之前就被丢掉,和通知采集那边同一条规矩" to false,
            "它不会替你发送,也不会替你答应任何事 —— 发送键是你按的" to false,
        ).forEach { (line, emphasised) ->
            Text(
                "· $line",
                style = MaterialTheme.typography.bodySmall,
                color = if (emphasised) {
                    MaterialTheme.colorScheme.onSurface
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
        Text(
            "过不去这一条就别开副驾。关掉它不影响日程、账单、记忆任何一样功能。",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

private val TIME = DateTimeFormatter.ofPattern("MM-dd HH:mm")

private fun formatTime(epochMillis: Long): String =
    TIME.format(Instant.ofEpochMilli(epochMillis).atZone(ZoneId.systemDefault()))
