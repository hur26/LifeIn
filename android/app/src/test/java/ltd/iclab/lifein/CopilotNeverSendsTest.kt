package ltd.iclab.lifein

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **发送键永远由人按。**
 *
 * 这不是一条权衡出来的结论,是这个功能的定义(01 §8)。
 * [03 的 P5](../../../../../../../docs/03-roadmap.md) 把它写成了验收标准,
 * 而且原话是"代码里不存在指向发送按钮的 `ACTION_CLICK`,**而且这一条要有测试盯着**"。
 * 这个文件就是那个测试。
 *
 * ## 它是文本扫描,不是证明
 *
 * 这一点要说在前面:下面几条断言查的是**源码文本**,不是运行时行为。
 * 一个存心要绕过它的人当然绕得过去(反射、字符串拼常量名)。
 *
 * 它挡的不是那种人,是**另一种更可能发生的事**:半年后某个人为了修
 * "填入之后输入框没聚焦"的毛病,顺手加一个 `performAction(ACTION_CLICK)`,
 * 而那个节点恰好是发送键的父容器。那种改动在 code review 里看起来完全无害 ——
 * 一行,而且改的是一个真实存在的毛病。
 *
 * 所以这里的做法是**把所有无障碍动作圈进一个文件**:
 * 副驾包里只有 `CopilotFill.kt` 允许出现 `performAction`,
 * 而那个文件小到能被逐行读完。
 */
class CopilotNeverSendsTest {

    /**
     * 允许出现 `performAction` 的文件。**只有一个,而且是刻意只有一个。**
     *
     * 加名字到这张表里要问一句:这个新文件凭什么需要对别人的界面做动作?
     * 副驾的全部写动作就是往输入框里填字,那件事一个文件装得下。
     */
    private val filesAllowedToAct = setOf("CopilotFill.kt")

    /**
     * 在那个文件里,动作只能作用在这些接收者上。
     *
     * 它们都只能从 `findEditable()` 拿到 —— 也就是说,能被操作的节点
     * **只有输入框**。一个叫 `sendButton` 的变量在这里过不去。
     */
    private val allowedReceivers = setOf("box", "edit", "focused", "input")

    private val performAction = Regex("""(\w+)(?:\.\w+)*\.performAction\(""")

    /**
     * 去掉注释之后的源码。**这个守卫要量的是代码,不是讲代码的话。**
     *
     * 副驾这几个文件里到处在解释"为什么不点发送键",而那些句子里出现
     * `performAction` 是正常的 —— 不去掉的话,把话说清楚这件事本身会让用例变红,
     * 而下一个人修红的办法多半是把解释删掉。
     *
     * `//` 那条规则会把行内的 `https://` 之后截断。这里能接受:
     * 要漏报,得有人把一个 `performAction` 写在同一行的 URL 后面。
     */
    private fun codeOnly(file: File): String =
        file.readText()
            .replace(Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("""//.*"""), "")

    private fun copilotSources(): List<File> {
        val dir = repoFile("android/app/src/main/java/ltd/iclab/lifein/copilot")
        assertTrue("找不到副驾的源码目录:${dir.absolutePath}", dir.isDirectory)
        val files = dir.walkTopDown().filter { it.extension == "kt" }.toList()
        assertTrue("副驾一个源文件都没有?那这个守卫在守什么", files.isNotEmpty())
        return files
    }

    @Test
    fun `only the fill module may perform accessibility actions`() {
        val offenders = copilotSources()
            .filter { it.name !in filesAllowedToAct }
            .filter { performAction.containsMatchIn(codeOnly(it)) }
            .map { it.name }

        assertEquals("这些文件里出现了无障碍动作,而只有填入那个文件可以", emptyList<String>(), offenders)
    }

    @Test
    fun `inside the fill module, only the input box is ever acted on`() {
        for (file in copilotSources().filter { it.name in filesAllowedToAct }) {
            for (match in performAction.findAll(codeOnly(file))) {
                val receiver = match.groupValues[1]
                assertTrue(
                    "${file.name} 对 `$receiver` 做了动作 —— 只有输入框可以被操作",
                    receiver in allowedReceivers,
                )
            }
        }
    }

    @Test
    fun `the accessibility config never asks for gesture dispatch`() {
        // **手势能力等于能点屏幕上任何一个位置,包括发送键。**
        // 副驾不需要它:填输入框走的是 ACTION_SET_TEXT,不是模拟点击。
        // 这一条能在今天就验,不用等填入那部分写出来
        //
        // **注释要先去掉再扫。** 第一版没去,而那个配置文件里恰好有一段注释在解释
        // "为什么不要 canPerformGestures" —— 于是这条用例被自己要守的那句话绊倒了。
        // 留个记号:这类文本守卫的第一个假阳性,基本都来自讲清楚它的那段话
        val config = repoFile("android/app/src/main/res/xml/copilot_accessibility.xml")
            .readText()
            .replace(Regex("""<!--.*?-->""", RegexOption.DOT_MATCHES_ALL), "")
        assertFalse("副驾的无障碍配置里声明了 canPerformGestures", config.contains("canPerformGestures"))
    }

    /**
     * 从测试的工作目录往上找到仓库根。
     *
     * Gradle 跑单元测试时工作目录是模块目录(`android/app`),但 IDE 里不一定 ——
     * 写死相对路径的话,这个守卫会在某些人的机器上**静默地找不到文件**,
     * 而找不到文件的表现和"扫过了,没问题"长得一模一样。
     */
    private fun repoFile(path: String): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val candidate = File(dir, path)
            if (candidate.exists()) return candidate
            dir = dir.parentFile
        }
        throw AssertionError("从 ${File(".").absolutePath} 一路往上都没找到 $path")
    }
}
