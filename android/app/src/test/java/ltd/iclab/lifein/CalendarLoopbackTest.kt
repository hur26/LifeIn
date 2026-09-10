package ltd.iclab.lifein

import ltd.iclab.lifein.calendar.CalendarWriter
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 回环:**App 自己写进系统日历的日程不能再被读回来**(06 §6.14)。
 *
 * 漏了这一条,一条日程会指数级地繁殖:
 *
 *     邮件提到"周三三点开会" → 提取 → todo → 写进日历
 *                                    ↓
 *              日历采集读到它 → raw_events → 提取 agent 又看见 → 再建一条 → …
 *
 * 而每一轮都长得像真的。
 *
 * **第一版只有一层防线(本地 `calendar_links` 表),那是个 bug。** 那张表在
 * 清除数据、恢复出厂、换手机重装、以及"insert 成功之后写表之前进程被杀"时会丢,
 * 而丢了的后果不是"少挡一次"是**永久性的** —— 那条事件从此对过滤器不可见。
 *
 * 所以现在有第二层:**印在事件自己身上**。这一组测的就是那个印。
 */
class CalendarLoopbackTest {

    private val ours = "ltd.iclab.lifein"
    private val theirs = "com.google.android.calendar"

    @Test
    fun `the footer survives having notes`() {
        // **这一条就是那个 bug。** 原来是 `notes ?: FOOTER` —— 有 notes 的时候
        // 印就没了,而提取出来的日程基本都有 notes,于是那个印几乎从不存在
        val stamped = CalendarWriter.stamped("腾讯会议 123-456")

        assertTrue(stamped.contains("腾讯会议 123-456"))
        assertTrue(stamped.trimEnd().endsWith(CalendarWriter.FOOTER))
    }

    @Test
    fun `no notes still gets the footer`() {
        assertTrue(CalendarWriter.stamped(null).endsWith(CalendarWriter.FOOTER))
        assertTrue(CalendarWriter.stamped("   ").endsWith(CalendarWriter.FOOTER))
    }

    @Test
    fun `our own event is recognised by either mark`() {
        // 两个印都留,是因为它们的失效方式不重叠:CUSTOM_APP_PACKAGE 在某些
        // 同步适配器和 ROM 上会被丢掉,而描述跟着同步走
        assertTrue(CalendarWriter.looksSelfWritten(null, ours, ours))
        assertTrue(
            CalendarWriter.looksSelfWritten(CalendarWriter.stamped("笔记"), null, ours)
        )
        assertTrue(
            CalendarWriter.looksSelfWritten(CalendarWriter.stamped("笔记") + "\n", null, ours)
        )
    }

    @Test
    fun `someone elses event is not filtered out`() {
        // **反过来不能误伤。** 挡过头的表现是日历里的会议一条都进不了摘要,
        // 而那和"日历采集没跑"看起来一模一样
        assertFalse(CalendarWriter.looksSelfWritten("周会,带上季度数据", theirs, ours))
        assertFalse(CalendarWriter.looksSelfWritten(null, theirs, ours))
        assertFalse(CalendarWriter.looksSelfWritten(null, null, ours))
        assertFalse(CalendarWriter.looksSelfWritten("", "", ours))
    }

    @Test
    fun `the footer must be at the end not merely mentioned`() {
        // 别人在描述里提到"由 LifeIn 写入"这几个字不该让整条被吞掉 ——
        // 印是行尾那一条,不是关键词
        assertFalse(
            CalendarWriter.looksSelfWritten(
                "${CalendarWriter.FOOTER} 的那套东西要在会上讲一下",
                theirs,
                ours,
            )
        )
    }
}
