package ltd.iclab.lifein.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.ShoppingCart
import androidx.compose.material.icons.filled.Star
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.calendar.CalendarWriter
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.data.Enrollment
import ltd.iclab.lifein.data.LifeInDatabase
import ltd.iclab.lifein.ui.theme.LifeInTheme

/**
 * App 的唯一界面入口。
 *
 * 没配码之前只有一张引导页:**这个 App 在配好之前什么都不该做** ——
 * 没有凭据的采集器只会攒一堆送不出去的东西。
 *
 * 配好之后五个页签,对应五个问题:今天要干什么、有什么等我点头、
 * 钱花到哪儿了、它记住了什么(以及记错了没有)、这套东西还活着吗。
 *
 * 最后一页叫「状态」而不是「我的」:**docs 里九处都叫它状态页**
 * (08 §部署验收、09 §5 那张表),而一个在文档里叫 A、在界面上叫 B 的东西,
 * 会让照着文档做的人以为自己找错了地方。
 *
 * ## 底部导航,不是顶部页签
 *
 * 原来是 `TabRow`。换掉它的理由有两条,第二条是决定性的:
 *
 * 1. 五个中文页签横排在顶部,窄屏上字会被压到换行或截断
 * 2. **顶部够不着。** 这五个页签是这个 App 的主干路,而一只手拿手机时
 *    拇指到不了屏幕顶部 —— 一个每天要点十次的东西不该需要换个姿势
 *
 * 换来的一件事:**「待确认」上能挂一个角标**。那是这个 App 里唯一一处
 * "不点开就不知道有事"的地方,而顶部页签上挂角标会挤掉本来就不够的宽度。
 */
class MainActivity : ComponentActivity() {

    /**
     * 日历是危险权限,只能在界面上要。**这个 App 要的权限一共就两处**:
     * 通知使用权(去系统设置里开)和这一个 —— 少到可以一句话解释清楚,
     * 而一个读你全部通知的 App 最需要的正是"能解释清楚"。
     */
    private val calendarPermission =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 内容铺到状态栏和手势条底下,由 Scaffold 把 inset 还回来。
        // 不开这个的话底部导航栏会和系统手势条打架 —— 表现是"点不中最下面那一排"
        enableEdgeToEdge()
        val repo = Repository(applicationContext)

        setContent {
            LifeInTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    val app = LifeInApp.instance
                    var enrolled by remember { mutableStateOf(app.secrets.load()) }

                    if (enrolled == null) {
                        EnrollScreen(onEnrolled = { enrolled = it })
                    } else {
                        Home(
                            repo = repo,
                            onOpenListenerSettings = { openListenerSettings() },
                            onRequestCalendar = {
                                calendarPermission.launch(
                                    arrayOf(
                                        Manifest.permission.READ_CALENDAR,
                                        Manifest.permission.WRITE_CALENDAR,
                                    )
                                )
                            },
                            onOpenUrl = { openInBrowser(it) },
                            onUnenroll = {
                                app.secrets.clear()
                                enrolled = null
                            },
                        )
                    }
                }
            }
        }
    }

    /** 通知使用权在系统设置里,只能引导过去 —— 没有任何 API 能替用户打开它。 */
    private fun openListenerSettings() {
        startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
    }

    /**
     * 把控制台链接交给浏览器。
     *
     * **不用 WebView。** 那条链接十五分钟内有效,而 WebView 里的 cookie 存在
     * 这个 App 自己的容器里 —— 于是"在浏览器打开"打开的并不是浏览器,
     * 用户存不了书签、用不了密码管理器、也看不见地址栏上那把锁。
     */
    private fun openInBrowser(url: String) {
        runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
    }
}

/** 底部那一排。`badge` 只有「待确认」给,见 `MainActivity` 的说明。 */
private enum class Section(val label: String, val icon: ImageVector) {
    Today("今天", Icons.Default.DateRange),
    Pending("待确认", Icons.Default.Notifications),
    Ledger("账本", Icons.Default.ShoppingCart),
    Memory("记忆", Icons.Default.Star),
    Status("状态", Icons.Default.Settings),
}

@Composable
private fun Home(
    repo: Repository,
    onOpenListenerSettings: () -> Unit,
    onRequestCalendar: () -> Unit,
    onOpenUrl: (String) -> Unit,
    onUnenroll: () -> Unit,
) {
    var tab by remember { mutableIntStateOf(0) }
    var queued by remember { mutableIntStateOf(0) }
    var pendingCount by remember { mutableIntStateOf(0) }
    val context = LifeInApp.instance

    // 切一次页签刷一次。**不轮询** —— 这个 App 的数据一天变几次,
    // 而一个每秒醒一次的界面在锁屏之后还在耗电(ADR-014 那条"不接收推送"的另一面)
    LaunchedEffect(tab) {
        queued = LifeInDatabase.get(context).queuedEvents().pending()
        // 待确认那一页自己会拉一次并回调 `onCount`,这里再拉就是两次同样的请求
        if (Section.entries[tab] != Section.Pending) {
            pendingCount = runCatching { repo.pending().size }.getOrDefault(pendingCount)
        }
    }

    Scaffold(
        bottomBar = {
            NavigationBar {
                Section.entries.forEachIndexed { index, section ->
                    NavigationBarItem(
                        selected = tab == index,
                        onClick = { tab = index },
                        icon = {
                            if (section == Section.Pending && pendingCount > 0) {
                                BadgedBox(badge = { Badge { Text("$pendingCount") } }) {
                                    Icon(section.icon, null)
                                }
                            } else {
                                Icon(section.icon, null)
                            }
                        },
                        label = { Text(section.label) },
                    )
                }
            }
        },
    ) { insets ->
        Box(Modifier.fillMaxSize().padding(bottom = insets.calculateBottomPadding())) {
            when (Section.entries[tab]) {
                Section.Today -> TodosScreen(repo)
                Section.Pending -> PendingScreen(repo, onCount = { pendingCount = it })
                Section.Ledger -> LedgerScreen(repo)
                Section.Memory -> MemoryScreen(repo)
                Section.Status -> StatusScreen(
                    repo = repo,
                    localListenerEnabled = CollectorState.listenerEnabled(context),
                    queued = queued,
                    onOpenListenerSettings = onOpenListenerSettings,
                    onRequestCalendar = onRequestCalendar,
                    // **传一个函数而不是一个布尔值。** 授权对话框关掉之后没有任何
                    // 东西会让这一行重新求值,而一个"授权了但界面没变"的开关
                    // 会让人以为授权失败了 —— 于是他再点一次,系统直接忽略
                    checkCalendarPermission = { CalendarWriter(context).hasPermission() },
                    onOpenUrl = onOpenUrl,
                    onUnenroll = onUnenroll,
                )
            }
        }
    }
}

/** 让 `EnrollScreen` 拿得到刚存下来的那份凭据。 */
internal typealias OnEnrolled = (Enrollment) -> Unit
