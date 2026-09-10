package ltd.iclab.lifein.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.view.WindowCompat

/**
 * App 的一套外观。**和 Web 控制台是同一个色板**(`lifein/api/console_style.py`)。
 *
 * 两处用同一个绿不是为了好看,是为了**同一个东西在两个屏幕上看起来是同一个东西**:
 * 一个人在手机上关掉采集、又在电脑上打开控制台确认,那两页要让他相信自己
 * 看的是同一套系统。
 *
 * ## 不用动态取色
 *
 * Material You 的 `dynamicColorScheme` 会让主色跟着壁纸走。否决它的理由只有一条:
 * **这个 App 里唯一需要被一眼看见的是「待确认」那个角标**,而它用的正是强调色 ——
 * 跟着壁纸走意味着某个人的手机上它和背景一个颜色,而他不会知道自己漏了什么。
 *
 * 代价是这个 App 在 Android 12+ 上不融入系统主题。接受它。
 *
 * ## 为什么是这个绿
 *
 * 这套界面上最重要的颜色其实是**红**:掉线、超支、"还没写进日历"。
 * 蓝色或紫色的主色会和红抢注意力 —— 它们在色环上离得近,并排出现时
 * "这一行不对劲"要看两眼才看得出来。一个偏冷的绿离红最远。
 *
 * ## 深色跟随系统,不做开关
 *
 * 做开关就要存偏好,而存偏好要么进库要么进 SharedPreferences ——
 * 两样都比这件事本身重。系统自己有那个开关。
 */

// ---------------------------------------------------------------- 色板

private val Brand = Color(0xFF2C6B5C)
private val BrandDark = Color(0xFF63BDA7)

private val LightColors = lightColorScheme(
    primary = Brand,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD8EAE4),
    onPrimaryContainer = Color(0xFF10382E),
    secondary = Color(0xFF4F6259),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFDDE9E3),
    onSecondaryContainer = Color(0xFF17281F),
    tertiary = Color(0xFF3F6377),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFD3E7F7),
    onTertiaryContainer = Color(0xFF001F2C),
    // 错误色偏暗红而不是鲜红:这个 App 上的"错"多半是"该看一眼",
    // 不是"出事了",而一个鲜红的惊叹号会让人以为数据丢了
    error = Color(0xFFA52C26),
    onError = Color.White,
    errorContainer = Color(0xFFFBE1DE),
    onErrorContainer = Color(0xFF410E0B),
    background = Color(0xFFF7F8F7),
    onBackground = Color(0xFF191D1B),
    surface = Color(0xFFFFFFFF),
    onSurface = Color(0xFF191D1B),
    surfaceVariant = Color(0xFFEEF1EF),
    onSurfaceVariant = Color(0xFF56635D),
    surfaceContainerLowest = Color(0xFFFFFFFF),
    surfaceContainerLow = Color(0xFFF7F8F7),
    surfaceContainer = Color(0xFFF1F3F1),
    surfaceContainerHigh = Color(0xFFEBEEEC),
    surfaceContainerHighest = Color(0xFFE5E9E6),
    outline = Color(0xFFCCD3CF),
    outlineVariant = Color(0xFFE3E7E5),
)

private val DarkColors = darkColorScheme(
    primary = BrandDark,
    onPrimary = Color(0xFF00382D),
    primaryContainer = Color(0xFF1B4A40),
    onPrimaryContainer = Color(0xFFB8EADB),
    secondary = Color(0xFFB6CCC2),
    onSecondary = Color(0xFF21352D),
    secondaryContainer = Color(0xFF374B43),
    onSecondaryContainer = Color(0xFFD2E8DE),
    tertiary = Color(0xFFA5CBE2),
    onTertiary = Color(0xFF073546),
    tertiaryContainer = Color(0xFF254B5E),
    onTertiaryContainer = Color(0xFFC5E7FF),
    error = Color(0xFFEF8B83),
    onError = Color(0xFF5F1512),
    errorContainer = Color(0xFF7A2620),
    onErrorContainer = Color(0xFFFFDAD5),
    background = Color(0xFF101413),
    onBackground = Color(0xFFE1E4E1),
    surface = Color(0xFF171B1A),
    onSurface = Color(0xFFE1E4E1),
    surfaceVariant = Color(0xFF212725),
    onSurfaceVariant = Color(0xFFA7B2AC),
    surfaceContainerLowest = Color(0xFF0C100F),
    surfaceContainerLow = Color(0xFF171B1A),
    surfaceContainer = Color(0xFF1B201E),
    surfaceContainerHigh = Color(0xFF252A28),
    surfaceContainerHighest = Color(0xFF303533),
    outline = Color(0xFF3B4340),
    outlineVariant = Color(0xFF2A312E),
)

// ---------------------------------------------------------------- 字与形

/**
 * 行高比 Material 的默认值大一点。
 *
 * 默认那套是按拉丁字母的字形算的,而汉字是满格的方块 —— 同样的行高下,
 * 中文看起来更挤。这个 App 上大部分文字是**要读进去的句子**
 * (「还没写进系统日历」「拿不准的都在这儿」),不是标签。
 */
private val AppTypography = Typography().let { base ->
    fun TextStyle.loosen(extra: Float) = copy(lineHeight = fontSize * extra)
    base.copy(
        headlineSmall = base.headlineSmall.copy(fontWeight = FontWeight.SemiBold),
        titleLarge = base.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        bodyLarge = base.bodyLarge.loosen(1.55f),
        bodyMedium = base.bodyMedium.loosen(1.6f),
        bodySmall = base.bodySmall.loosen(1.65f),
        labelLarge = base.labelLarge.copy(fontWeight = FontWeight.Medium),
    )
}

/**
 * 圆角比默认的大一档。卡片是这个界面的主要单位(一条待办、一笔账、一条记忆),
 * 而**圆角越大,一张卡片越像"一件东西"而不是"一段被框起来的文字"**。
 */
private val AppShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(10.dp),
    medium = RoundedCornerShape(14.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(26.dp),
)

/** 一把尺子,4dp 起步。见 `console_style.py` 里那段 —— 调过的那个组件会变成唯一对不齐的那个。 */
object Space {
    val xs = 4.dp
    val sm = 8.dp
    val md = 12.dp
    val lg = 16.dp
    val xl = 24.dp
    val xxl = 32.dp
}

@Composable
fun LifeInTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val colors = if (darkTheme) DarkColors else LightColors

    // 状态栏图标跟着主题走。不写这一句的话浅色主题下状态栏是白字白底 ——
    // 那不是"看不清",是"看不见"
    val view = LocalView.current
    if (!view.isInEditMode) {
        val context = LocalContext.current
        SideEffect {
            (context as? Activity)?.window?.let { window ->
                WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars =
                    !darkTheme
            }
        }
    }

    MaterialTheme(
        colorScheme = colors,
        typography = AppTypography,
        shapes = AppShapes,
        content = content,
    )
}
