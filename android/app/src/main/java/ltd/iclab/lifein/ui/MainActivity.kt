package ltd.iclab.lifein.ui

import android.Manifest
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.calendar.CalendarWriter
import ltd.iclab.lifein.collect.CollectorState
import ltd.iclab.lifein.data.Enrollment
import ltd.iclab.lifein.data.LifeInDatabase

/**
 * App 的唯一界面入口。
 *
 * 没配码之前只有一个粘贴框:**这个 App 在配好之前什么都不该做** ——
 * 没有凭据的采集器只会攒一堆送不出去的东西。
 *
 * 配好之后四个页签,对应四个问题:今天要干什么、有什么等我点头、
 * 它记住了什么(以及记错了没有)、采集器还活着吗。
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
        val repo = Repository(applicationContext)

        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
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
}

@Composable
private fun Home(
    repo: Repository,
    onOpenListenerSettings: () -> Unit,
    onRequestCalendar: () -> Unit,
    onUnenroll: () -> Unit,
) {
    var tab by remember { mutableIntStateOf(0) }
    var queued by remember { mutableIntStateOf(0) }
    val context = LifeInApp.instance

    LaunchedEffect(tab) {
        queued = LifeInDatabase.get(context).queuedEvents().pending()
    }

    Column(Modifier.fillMaxSize()) {
        TabRow(selectedTabIndex = tab) {
            listOf("今天", "待确认", "记忆", "状态").forEachIndexed { index, title ->
                Tab(selected = tab == index, onClick = { tab = index }, text = { Text(title) })
            }
        }
        when (tab) {
            0 -> TodosScreen(repo)
            1 -> PendingScreen(repo)
            2 -> MemoryScreen(repo)
            else -> StatusScreen(
                repo = repo,
                localListenerEnabled = CollectorState.listenerEnabled(context),
                queued = queued,
            ) {
                TextButton(onClick = onOpenListenerSettings) { Text("打开通知使用权设置") }
                if (!CalendarWriter(context).hasPermission()) {
                    // 没有这个权限时,日程会一直停在"未写入日历"。
                    // 那个状态在列表和小组件上都看得见,但原因只有这里说得清
                    Text("没有日历权限,日程写不进系统日历")
                    TextButton(onClick = onRequestCalendar) { Text("授予日历权限") }
                }
                CollectorState.lastUpload(context)?.let { Text("上次上报:$it") }
                CollectorState.lastHeartbeat(context)?.let { Text("上次心跳:$it") }
                CollectorState.lastError(context)?.let {
                    Text("最近一次失败:$it", color = MaterialTheme.colorScheme.error)
                }
                TextButton(onClick = onUnenroll) { Text("解绑这台设备") }
                Text(
                    "解绑只清掉手机上这份。服务端那两条凭据还有效," +
                        "手机丢了要另外跑 revoke-device。",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
private fun EnrollScreen(onEnrolled: (Enrollment) -> Unit) {
    var text by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    fun accept(raw: String) {
        runCatching { Enrollment.parse(raw) }
            .onSuccess {
                LifeInApp.instance.secrets.save(it)
                onEnrolled(it)
            }
            .onFailure { error = it.message ?: "配码不对" }
    }

    // 扫码结果直接进解析:扫出来的和粘进来的是同一串东西,
    // 所以走同一条校验路径 —— 两条入口一套判断,不会出现"扫码能过、粘贴不过"
    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        val raw = result.contents
        if (raw == null) {
            // 用户按了返回,或者没给相机权限。不当错误 —— 粘贴那条路还在
            error = null
        } else {
            accept(raw)
        }
    }

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("配置采集端", style = MaterialTheme.typography.headlineSmall)
        Text(
            "在服务器上跑 python -m lifein.admin issue-device --user <你的 uuid> " +
                "--device-id <这台手机>,它会打出一串配码并生成一个二维码文件。\n\n" +
                "扫那个二维码,或者把那串东西粘到下面。只显示一次。",
            style = MaterialTheme.typography.bodyMedium,
        )

        Button(
            onClick = {
                error = null
                scanner.launch(
                    ScanOptions()
                        .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                        .setPrompt("对准 issue-device 生成的那个二维码")
                        .setBeepEnabled(false)
                        // 竖屏锁死:配码是站着扫的,转屏只会让人手忙脚乱
                        .setOrientationLocked(true)
                )
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("扫码配置")
        }
        Text(
            "相机权限点了才会要;不给也能用 —— 粘贴那条路一直在。",
            style = MaterialTheme.typography.bodySmall,
        )

        HorizontalDivider()

        OutlinedTextField(
            value = text,
            onValueChange = {
                text = it
                error = null
            },
            label = { Text("或者把配码粘在这里") },
            minLines = 4,
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            onClick = { accept(text) },
            enabled = text.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("保存")
        }
    }
}
