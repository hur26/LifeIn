package ltd.iclab.lifein.ui.theme

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp

/**
 * 界面上反复出现的那几块。**它们存在的理由是"改一处"。**
 *
 * 一个"没有内容"的空状态在五个页面上各写一遍,就会变成五种措辞和五种留白 ——
 * 而**措辞不一致会让人以为它们是五种不同的情况**。
 *
 * ## 语气分四档,不是"红/不红"
 *
 * `Tone` 把"这条要不要现在管"编码进颜色:
 *
 * | 档 | 含义 | 典型 |
 * | --- | --- | --- |
 * | `Neutral` | 就是一个事实 | "已确认""上次上报 10:03" |
 * | `Positive` | 好的那一侧 | "正在采集""已对账" |
 * | `Attention` | 要你点一下 | "待确认 3 条""快到预算了" |
 * | `Problem` | 已经出事了 | "掉线""没写进日历""超支" |
 *
 * **`Attention` 和 `Problem` 分开是这四档里唯一重要的那件事。** 合成一档的话,
 * "有三条等你确认"和"你的采集器已经死了两天"会长得一模一样,
 * 而人对每天都出现的红色会很快免疫 —— 于是真出事那次也被略过。
 */
enum class Tone { Neutral, Positive, Attention, Problem }

@Composable
private fun toneColors(tone: Tone): Pair<Color, Color> {
    val scheme = MaterialTheme.colorScheme
    return when (tone) {
        Tone.Neutral -> scheme.surfaceContainerHighest to scheme.onSurfaceVariant
        Tone.Positive -> scheme.primaryContainer to scheme.onPrimaryContainer
        Tone.Attention -> scheme.tertiaryContainer to scheme.onTertiaryContainer
        Tone.Problem -> scheme.errorContainer to scheme.onErrorContainer
    }
}

/** 一枚状态。**只放短语,不放句子** —— 一个换行的角标说明它装的是别的东西。 */
@Composable
fun StatusChip(text: String, tone: Tone = Tone.Neutral, icon: ImageVector? = null) {
    val (background, foreground) = toneColors(tone)
    Surface(color = background, shape = RoundedCornerShape(50), contentColor = foreground) {
        Row(
            Modifier.padding(horizontal = Space.sm, vertical = 3.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            if (icon != null) Icon(icon, null, Modifier.size(13.dp))
            Text(text, style = MaterialTheme.typography.labelSmall)
        }
    }
}

/**
 * 一块内容。`title` 给空串就是一张没有标题的卡片。
 *
 * 用 `Card` 而不是 `ElevatedCard`:这个 App 的列表里一屏能有七八张卡,
 * **每张都投影的话整页看起来像一堆浮起来的纸片**,而它们其实是一列同级的东西。
 */
@Composable
fun SectionCard(
    title: String = "",
    modifier: Modifier = Modifier,
    trailing: @Composable (() -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
    ) {
        Column(Modifier.padding(Space.lg), verticalArrangement = Arrangement.spacedBy(Space.sm)) {
            if (title.isNotBlank() || trailing != null) {
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(title, style = MaterialTheme.typography.titleSmall)
                    trailing?.invoke()
                }
            }
            content()
        }
    }
}

/**
 * 一个数字加一句话。**下面那句话不是可选的。**
 *
 * 一个没有解释的数字会被当成 KPI,而这个 App 上每个数字要回答的是
 * "要不要现在做点什么"。
 */
@Composable
fun StatTile(
    label: String,
    value: String,
    note: String,
    tone: Tone = Tone.Neutral,
    modifier: Modifier = Modifier,
) {
    val (background, foreground) = toneColors(tone)
    Surface(
        modifier = modifier,
        color = if (tone == Tone.Neutral) MaterialTheme.colorScheme.surface else background,
        contentColor = if (tone == Tone.Neutral) MaterialTheme.colorScheme.onSurface else foreground,
        shape = MaterialTheme.shapes.medium,
    ) {
        Column(
            Modifier.padding(Space.md),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            // 标题和注脚都比数字淡一档:一张卡片上最先被看见的必须是那个数字
            val dim = LocalContentColor.current.copy(alpha = 0.72f)
            Text(label, style = MaterialTheme.typography.labelSmall, color = dim)
            Text(value, style = MaterialTheme.typography.titleLarge)
            Text(note, style = MaterialTheme.typography.bodySmall, color = dim)
        }
    }
}

/**
 * 出事了那条横幅。**只在真出事时出现。**
 *
 * 常驻的提示会被读成装饰,而装饰是不会被读的 —— 于是真出事那次也被略过。
 */
@Composable
fun NoticeBanner(
    text: String,
    tone: Tone = Tone.Problem,
    icon: ImageVector? = null,
    action: Pair<String, () -> Unit>? = null,
) {
    val (background, foreground) = toneColors(tone)
    Surface(
        color = background,
        contentColor = foreground,
        shape = MaterialTheme.shapes.medium,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            Modifier.padding(start = Space.lg, end = Space.sm, top = Space.md, bottom = Space.md),
            horizontalArrangement = Arrangement.spacedBy(Space.md),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (icon != null) Icon(icon, null, Modifier.size(18.dp))
            Text(text, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
            if (action != null) {
                TextButton(onClick = action.second) { Text(action.first) }
            }
        }
    }
}

/**
 * 什么都没有的时候。
 *
 * **一句解释,而不是一个"暂无数据"。** "暂无数据"回答不了用户真正在问的那句:
 * 是还没开始跑,还是我做错了什么?
 */
@Composable
fun EmptyState(icon: ImageVector, title: String, hint: String) {
    Column(
        Modifier.fillMaxWidth().padding(vertical = Space.xxl, horizontal = Space.xl),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(Space.sm),
    ) {
        Box(
            Modifier
                .size(52.dp)
                .background(MaterialTheme.colorScheme.surfaceContainerHigh, CircleShape),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                icon,
                null,
                Modifier.size(24.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Text(title, style = MaterialTheme.typography.titleSmall)
        Text(
            hint,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}

/** 转圈。**居中且占满** —— 顶在角落里的转圈会让人以为页面已经加载完了。 */
@Composable
fun LoadingState(modifier: Modifier = Modifier.fillMaxSize()) {
    Box(modifier, contentAlignment = Alignment.Center) {
        CircularProgressIndicator(strokeWidth = 2.5.dp, modifier = Modifier.size(28.dp))
    }
}

/**
 * 一条横向的进度条(预算用)。
 *
 * 自己画而不是用 `LinearProgressIndicator`:超支时要显示的是**超出的那一截**,
 * 而那个组件在 1.0 处就截断了 —— 于是"刚好花完"和"超了一倍"长得一模一样。
 */
@Composable
fun MeterBar(fraction: Float, tone: Tone) {
    val (track, _) = toneColors(Tone.Neutral)
    val fill = when (tone) {
        Tone.Problem -> MaterialTheme.colorScheme.error
        Tone.Attention -> MaterialTheme.colorScheme.tertiary
        else -> MaterialTheme.colorScheme.primary
    }
    Box(
        Modifier
            .fillMaxWidth()
            .height(6.dp)
            .background(track, RoundedCornerShape(50)),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0f, 1f))
                .height(6.dp)
                .background(fill, RoundedCornerShape(50)),
        )
    }
}
