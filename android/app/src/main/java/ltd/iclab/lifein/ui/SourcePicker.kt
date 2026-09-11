package ltd.iclab.lifein.ui

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import ltd.iclab.lifein.net.PresetDto
import ltd.iclab.lifein.ui.theme.Space

/**
 * 放行来源的两个入口:**选应用**,和**从建议目录里选**([ADR-033](../../../../../../../docs/04-tech-decisions.md))。
 *
 * 它存在的理由是用户那句话:
 *
 * > 普通用户怎么知道包名呢,他们只知道软件名呀
 *
 * 原来那一格的输入框写着"加一个包名"、placeholder 是 `com.tencent.mm` ——
 * 等于要求人先知道 `com.eg.android.AlipayGphone` 才能放行支付宝。而
 * [R10 的四前提](../../../../../../../docs/05-risks.md)里有一条是**朋友要能
 * 自己管理采集**,一个要先查包名的输入框把"自己能做"变回了"找你帮忙"。
 *
 * 两个入口分工不同,缺一个都不够:
 *
 * - **应用选择器**回答"支付宝的包名是什么" —— `PackageManager` 在本地就有
 *   这份对应关系,不需要任何接口
 * - **建议目录**回答"招商银行的短信号段是多少" —— 号段不是应用,选择器里
 *   选不出来,而它从服务端拿(号段会变,而 App 改一次要重新发版)
 */

/** 一个能从桌面启动的应用。`label` 是用户看得懂的那个名字。 */
data class InstalledApp(val label: String, val packageName: String)

/**
 * 列出能从桌面启动的应用。
 *
 * **只列有启动图标的**:全部列出来会有几百个系统组件,而那比包名输入框
 * 更难用。代价是有些厂商的短信应用没有启动图标 —— 那时退回手输包名,
 * 所以那个输入框留着(ADR-033)。
 *
 * Android 11 起包可见性默认隔离,靠 `AndroidManifest.xml` 里那段
 * `<queries>` 才看得到别的应用。**用 `<queries>` 而不是
 * `QUERY_ALL_PACKAGES`** —— 后者是敏感权限,而这里只要"有启动图标"这一类。
 */
fun installedApps(context: Context): List<InstalledApp> {
    val pm = context.packageManager
    val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
    return runCatching {
        pm.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY)
            .asSequence()
            .map { InstalledApp(it.loadLabel(pm).toString(), it.activityInfo.packageName) }
            .filter { it.packageName != context.packageName }
            .distinctBy { it.packageName }
            .sortedBy { it.label }
            .toList()
    }.getOrDefault(emptyList())
}

/**
 * 从已安装的应用里选一个,返回包名。
 *
 * 列表在 IO 线程上取:几百个应用的 `loadLabel` 会读一遍资源,放在主线程上
 * 表现是点开对话框卡一下。
 */
@Composable
fun AppPickerDialog(onPick: (InstalledApp) -> Unit, onDismiss: () -> Unit) {
    val context = LocalContext.current
    var apps by remember { mutableStateOf<List<InstalledApp>?>(null) }
    var keyword by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        apps = withContext(Dispatchers.IO) { installedApps(context) }
    }

    val shown = remember(apps, keyword) {
        val all = apps ?: emptyList()
        if (keyword.isBlank()) all
        else all.filter {
            it.label.contains(keyword, ignoreCase = true) ||
                it.packageName.contains(keyword, ignoreCase = true)
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("取消") } },
        title = { Text("选一个应用") },
        text = {
            Column {
                Text(
                    "选中之后这个应用的通知会被采集。只放消息类 —— " +
                        "银行与支付类在「常用来源」那一格里。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(Space.sm))
                OutlinedTextField(
                    value = keyword,
                    onValueChange = { keyword = it },
                    label = { Text("搜应用名") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(Space.sm))
                when {
                    apps == null -> Text("正在读已安装的应用…")
                    shown.isEmpty() -> Text("没有匹配的应用。装了但没有启动图标的应用在这里列不出来,用下面的包名输入框。")
                    else -> LazyColumn(Modifier.height(360.dp)) {
                        items(shown, key = { it.packageName }) { app ->
                            AppRow(app) { onPick(app) }
                        }
                    }
                }
            }
        },
    )
}

@Composable
private fun AppRow(app: InstalledApp, onClick: () -> Unit) {
    val context = LocalContext.current
    // 只有滚到的那几行会取图标 —— LazyColumn 不会把没显示的也组合出来
    val icon = remember(app.packageName) { loadIcon(context, app.packageName) }

    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Space.md),
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(vertical = Space.sm),
    ) {
        if (icon != null) {
            Image(bitmap = icon, contentDescription = null, modifier = Modifier.size(36.dp))
        } else {
            Spacer(Modifier.size(36.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(app.label, style = MaterialTheme.typography.bodyLarge)
            // 包名仍然显示出来:选错了要看得出来选的是哪一个,
            // 而且排查时它是唯一能对上服务端那一行的东西
            Text(
                app.packageName,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * 从建议目录里选一条。银行号段这类**不是应用**的东西只能走这里。
 *
 * 显示的是"招商银行",发出去的是 `95555` + `sms_sender` + `transaction`。
 */
@Composable
fun PresetPickerDialog(
    presets: List<PresetDto>,
    onPick: (PresetDto) -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = onDismiss) { Text("取消") } },
        title = { Text("常用来源") },
        text = {
            Column {
                Text(
                    "银行短信按号段放行,支付类按应用放行。选中只是加进白名单 —— " +
                        "采不采得到还要看手机上装没装、有没有给通知使用权。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(Space.sm))
                if (presets.isEmpty()) {
                    Text("拿不到建议目录 —— 检查一下服务端连得上吗。")
                } else {
                    LazyColumn(Modifier.height(360.dp)) {
                        items(presets, key = { it.matchType + it.pattern }) { preset ->
                            Column(
                                Modifier
                                    .fillMaxWidth()
                                    .clickable { onPick(preset) }
                                    .padding(vertical = Space.sm)
                            ) {
                                Text(preset.label, style = MaterialTheme.typography.bodyLarge)
                                Text(
                                    if (preset.matchType == "sms_sender") {
                                        "短信号段 ${preset.pattern}"
                                    } else {
                                        preset.pattern
                                    },
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                    }
                }
            }
        },
    )
}

private fun loadIcon(context: Context, packageName: String): ImageBitmap? =
    runCatching { context.packageManager.getApplicationIcon(packageName).toImageBitmap() }.getOrNull()

/** 图标是 `Drawable`,Compose 要 `ImageBitmap`。自己转,不为这件事引一个依赖。 */
private fun Drawable.toImageBitmap(): ImageBitmap {
    (this as? BitmapDrawable)?.bitmap?.let { return it.asImageBitmap() }
    val width = intrinsicWidth.takeIf { it > 0 } ?: 96
    val height = intrinsicHeight.takeIf { it > 0 } ?: 96
    val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
    val canvas = Canvas(bitmap)
    setBounds(0, 0, canvas.width, canvas.height)
    draw(canvas)
    return bitmap.asImageBitmap()
}
