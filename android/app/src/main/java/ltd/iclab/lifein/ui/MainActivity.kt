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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.calendar.CalendarWriter
import ltd.iclab.lifein.collect.CollectorState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ltd.iclab.lifein.BuildConfig
import ltd.iclab.lifein.data.DeviceId
import ltd.iclab.lifein.data.Enrollment
import ltd.iclab.lifein.data.EnrollmentPayload
import ltd.iclab.lifein.net.EnrollClient
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
            listOf("今天", "待确认", "账本", "记忆", "状态").forEachIndexed { index, title ->
                Tab(selected = tab == index, onClick = { tab = index }, text = { Text(title) })
            }
        }
        when (tab) {
            0 -> TodosScreen(repo)
            1 -> PendingScreen(repo)
            2 -> LedgerScreen(repo)
            3 -> MemoryScreen(repo)
            else -> StatusScreen(
                repo = repo,
                localListenerEnabled = CollectorState.listenerEnabled(context),
                queued = queued,
            ) {
                // R10 改判四前提之一:App 里要有这个开关,不是"找你帮忙"
                CollectionControls(repo)
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
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    /**
     * 收下一串配码。两种都认(见 `EnrollmentPayload`):
     *
     * - `issue-device` 那种:密钥已经在手上,直接存
     * - `invite` 那种:手上只有一张换取码,要联网去 `POST /enroll/claim` 换
     *
     * **第二种是这个 App 原来接不上的那一半。** 服务端的 invite 早就在打
     * `{"v":2,"claim":…}` 了,而这里只会解旧的那种 —— 于是朋友接入的第一道门
     * 上,代码在服务端、客户端还走旧洞。
     */
    fun accept(raw: String) {
        error = null
        val payload = try {
            EnrollmentPayload.parse(raw)
        } catch (e: Exception) {
            error = e.message ?: "配码不对"
            return
        }

        when (payload) {
            is EnrollmentPayload.Ready -> {
                LifeInApp.instance.secrets.save(payload.enrollment)
                onEnrolled(payload.enrollment)
            }

            is EnrollmentPayload.Invite -> {
                busy = true
                scope.launch {
                    val result = withContext(Dispatchers.IO) {
                        runCatching {
                            EnrollClient.claim(
                                baseUrl = payload.baseUrl,
                                code = payload.claim,
                                // 自己生成、存下来复用。人编的名字会重复,
                                // 而重复的 device_id 意味着吊销一台会连带
                                // 吊销另一台(06 §6.15)
                                deviceId = DeviceId.get(LifeInApp.instance),
                                appVersion = BuildConfig.VERSION_NAME,
                            )
                        }
                    }
                    busy = false
                    result
                        .onSuccess {
                            LifeInApp.instance.secrets.save(it)
                            onEnrolled(it)
                        }
                        .onFailure { error = it.message ?: "换取密钥失败" }
                }
            }
        }
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
            "让对方在服务器上跑 python -m lifein.admin invite --user <你的 uuid>," +
                "它会生成一个二维码文件。\n\n" +
                "那张图里没有密钥,只有一张十分钟内、只能用一次的换取码 —— " +
                "所以它可以直接发给你。扫它,或者把那串东西粘到下面," +
                "App 会自己去把密钥换回来。",
            style = MaterialTheme.typography.bodyMedium,
        )
        Text(
            "自己给自己配码时也可以用 issue-device 打出来的那种,同样扫或粘。" +
                "但那张图里是明文密钥,不要发在聊天里。",
            style = MaterialTheme.typography.bodySmall,
        )

        Button(
            onClick = {
                error = null
                scanner.launch(
                    ScanOptions()
                        .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                        .setPrompt("对准那张配码二维码")
                        .setBeepEnabled(false)
                        // 竖屏锁死:配码是站着扫的,转屏只会让人手忙脚乱
                        .setOrientationLocked(true)
                )
            },
            enabled = !busy,
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
            enabled = !busy,
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            onClick = { accept(text) },
            // **换取中一律不许再点。** 一张码只能用一次,第二次点下去
            // 拿到的是 401,而那句"配码无效"会让人以为第一次也失败了
            enabled = text.isNotBlank() && !busy,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(if (busy) "正在换取密钥…" else "保存")
        }
    }
}
