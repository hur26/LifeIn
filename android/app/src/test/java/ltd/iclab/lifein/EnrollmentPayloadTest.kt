package ltd.iclab.lifein

import ltd.iclab.lifein.data.EnrollmentPayload
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * 配码解析 —— **朋友接入的第一道门**。
 *
 * 这一组存在的理由是一次真实的错位:服务端的 `admin invite` 早就在打
 * `{"v":2,"claim":…}` 了(P4 第 1 片),而 App 这边只会解 `issue-device`
 * 那种带明文密钥的 payload。**代码在服务端,客户端还走旧洞** ——
 * 而症状是扫完之后一句"配码里缺 base_url / user_id / device_id",
 * 完全指不到真正的原因。
 *
 * 下面的两串是服务端真打出来的形状:`admin.py` 的 `cmd_invite` 与
 * `cmd_issue_device`。改那两处时这里会红,那正是要的效果。
 */
class EnrollmentPayloadTest {

    private val invite =
        """{"v": 2, "claim": "Zm9vYmFy-128-bit-random", "base_url": "https://life.example.com"}"""

    private val issueDevice = """
        {"base_url": "https://life.example.com",
         "user_id": "8f14e45f-ea8d-4b3c-9a1b-2c3d4e5f6a7b",
         "device_id": "pixel-7a",
         "collector_secret": "c2VjcmV0LWNvbGxlY3Rvcg==",
         "query_secret": "c2VjcmV0LXF1ZXJ5"}
    """.trimIndent()

    @Test
    fun `an invite is recognised as one`() {
        val parsed = EnrollmentPayload.parse(invite)
        assertTrue("v:2 的 payload 必须解成 Invite", parsed is EnrollmentPayload.Invite)
        parsed as EnrollmentPayload.Invite
        assertEquals("Zm9vYmFy-128-bit-random", parsed.claim)
        assertEquals("https://life.example.com", parsed.baseUrl)
    }

    @Test
    fun `an issue-device payload still works`() {
        // 自己给自己配码时它更省一步(不需要服务端在线),所以不能因为
        // 加了新的一种就把旧的那种废掉
        val parsed = EnrollmentPayload.parse(issueDevice)
        assertTrue(parsed is EnrollmentPayload.Ready)
        parsed as EnrollmentPayload.Ready
        assertEquals("pixel-7a", parsed.enrollment.deviceId)
        assertTrue(parsed.enrollment.isComplete)
    }

    @Test
    fun `an invite is not mistaken for a broken enrollment`() {
        // **这一条就是那个 bug 的回归测试。**
        // 旧代码会走 Enrollment.parse,报"配码里缺 base_url / user_id / device_id"
        // —— 一句完全指错方向的话,而用户手上只有这句话
        val parsed = EnrollmentPayload.parse(invite)
        assertTrue(parsed !is EnrollmentPayload.Ready)
    }

    @Test
    fun `an invite without a claim says what to do`() {
        try {
            EnrollmentPayload.parse("""{"v": 2, "base_url": "https://x"}""")
            fail("缺 claim 的 invite 应该报错")
        } catch (e: IllegalStateException) {
            assertTrue("要说清楚该干什么", e.message!!.contains("admin invite"))
        }
    }

    @Test
    fun `random text is refused`() {
        for (junk in listOf("", "   ", "不是配码", "{", "[1,2,3]")) {
            try {
                EnrollmentPayload.parse(junk)
                fail("这串不该被当成配码:$junk")
            } catch (e: IllegalStateException) {
                assertTrue(e.message!!.isNotBlank())
            }
        }
    }

    @Test
    fun `a future version is not silently treated as an invite`() {
        // v:3 出现时这边还不认识它。**走旧的那条解析路径并报错**,
        // 好过按 v:2 的形状去猜 —— 猜错的代价是拿一张换取码去当密钥用
        try {
            EnrollmentPayload.parse("""{"v": 3, "claim": "x", "base_url": "https://x"}""")
            fail("不认识的版本不该被当成 invite")
        } catch (e: IllegalStateException) {
            assertTrue(e.message!!.isNotBlank())
        }
    }
}
