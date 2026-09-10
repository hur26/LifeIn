package ltd.iclab.lifein.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ltd.iclab.lifein.BuildConfig
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.data.DeviceId
import ltd.iclab.lifein.data.EnrollmentPayload
import ltd.iclab.lifein.net.EnrollClient
import ltd.iclab.lifein.ui.theme.NoticeBanner
import ltd.iclab.lifein.ui.theme.SectionCard
import ltd.iclab.lifein.ui.theme.Space
import ltd.iclab.lifein.ui.theme.Tone

/**
 * 配码页 —— **这个 App 的第一屏,而且对大多数人是唯一一次看见的一屏**。
 *
 * 它要在三十秒内让一个非技术背景的人完成接入,所以排版上做了一件事:
 * **把"扫码"放在最上面、最大、独占一行**,把粘贴那条路折到下面。
 * 两条路都留着(相机权限不给也要能进来),但它们不是并列的两个选项 ——
 * 十个人里有九个会扫码。
 *
 * 那段"为什么这张图可以直接发给你"的解释放在按钮**下面**而不是上面:
 * 挡在动作前面的解释,人会跳过;跟在动作后面的解释,人会在等待时读。
 */
@Composable
fun EnrollScreen(onEnrolled: OnEnrolled) {
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
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = Space.xl, vertical = Space.xxl),
        verticalArrangement = Arrangement.spacedBy(Space.lg),
    ) {
        Surface(
            color = MaterialTheme.colorScheme.primaryContainer,
            shape = CircleShape,
            modifier = Modifier.size(56.dp),
        ) {
            Box(contentAlignment = Alignment.Center) {
                Icon(Icons.Default.Lock, null, tint = MaterialTheme.colorScheme.onPrimaryContainer)
            }
        }

        Text("配置这台设备", style = MaterialTheme.typography.headlineSmall)
        Text(
            "扫一张配码二维码,这个 App 就配好了。它由对方在控制台上点「添加设备」生成,"
            + "或者在服务器上跑 admin invite。",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
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

        SectionCard("图里没有密钥,所以它可以直接发给你") {
            Text(
                "二维码里只有一张十分钟内、只能用一次的换取码。App 会拿它去服务端"
                    + "换回真正的密钥,而那两把密钥从头到尾没有离开过这台手机。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                "自己给自己配码时也可以用 issue-device 打出来的那种,同样扫或粘。"
                    + "但那张图里是明文密钥,不要发在聊天里。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        error?.let { NoticeBanner(it, tone = Tone.Problem) }

        SectionCard("扫不了?把那一串粘进来") {
            OutlinedTextField(
                value = text,
                onValueChange = {
                    text = it
                    error = null
                },
                label = { Text("配码") },
                minLines = 3,
                maxLines = 6,
                enabled = !busy,
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedButton(
                onClick = { accept(text) },
                // **换取中一律不许再点。** 一张码只能用一次,第二次点下去
                // 拿到的是 401,而那句"配码无效"会让人以为第一次也失败了
                enabled = text.isNotBlank() && !busy,
                modifier = Modifier.fillMaxWidth(),
            ) {
                if (busy) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(Space.sm),
                    ) {
                        CircularProgressIndicator(Modifier.size(15.dp), strokeWidth = 2.dp)
                        Text("正在换取密钥…")
                    }
                } else {
                    Text("保存")
                }
            }
            Text(
                "相机权限点了扫码才会要;不给也能用 —— 粘贴这条路一直在。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
