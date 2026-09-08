package ltd.iclab.lifein.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import ltd.iclab.lifein.LifeInApp
import ltd.iclab.lifein.data.Enrollment

/**
 * App 的唯一界面入口。
 *
 * 没配码之前只有一个粘贴框:**这个 App 在配好之前什么都不该做** ——
 * 没有凭据的采集器只会攒一堆送不出去的东西。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    val app = LifeInApp.instance
                    var enrolled by remember { mutableStateOf(app.secrets.load()) }

                    if (enrolled == null) {
                        EnrollScreen(onEnrolled = { enrolled = it })
                    } else {
                        EnrolledScreen(
                            enrollment = enrolled!!,
                            onCleared = {
                                app.secrets.clear()
                                enrolled = null
                            },
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun EnrollScreen(onEnrolled: (Enrollment) -> Unit) {
    var text by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("配置采集端", style = MaterialTheme.typography.headlineSmall)
        Text(
            "在服务器上跑 python -m lifein.admin issue-device --user <你的 uuid> " +
                "--device-id <这台手机>,把它打出来的那一串粘到下面。\n\n" +
                "那串东西里有两把密钥,只显示一次。",
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = text,
            onValueChange = {
                text = it
                error = null
            },
            label = { Text("配码") },
            minLines = 4,
            modifier = Modifier.fillMaxWidth(),
        )
        error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button(
            onClick = {
                runCatching { Enrollment.parse(text) }
                    .onSuccess {
                        LifeInApp.instance.secrets.save(it)
                        onEnrolled(it)
                    }
                    .onFailure { error = it.message ?: "配码不对" }
            },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("保存")
        }
    }
}

@Composable
private fun EnrolledScreen(enrollment: Enrollment, onCleared: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("已配置", style = MaterialTheme.typography.headlineSmall)
        Text("服务端:${enrollment.baseUrl}")
        Text("这台设备:${enrollment.deviceId}")
        // 密钥不显示。它已经进了 Keystore,而"再看一眼"没有任何正当用途
        TextButton(onClick = onCleared) { Text("解绑这台设备") }
        Text(
            "解绑只清掉手机上这份。**服务端那两条凭据还有效**," +
                "手机丢了要另外跑 revoke-device。",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}
