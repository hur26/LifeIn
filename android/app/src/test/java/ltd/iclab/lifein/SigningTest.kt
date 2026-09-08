package ltd.iclab.lifein

import ltd.iclab.lifein.net.Signing
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 签名的**跨端一致性**。
 *
 * 下面这组值是拿服务端 `lifein/api/auth.py` 真跑出来的,服务端那边有一份
 * 一模一样的用例(`tests/test_api_auth.py`)。**同一个向量,两端各测一次** ——
 * 这是唯一能在不联网的情况下发现"两边算得不一样"的办法,
 * 而那种不一样的表现是所有请求都 401 且服务端不说原因(06 §6.11)。
 *
 * 改签名格式时两边的用例会同时红。那正是要的效果。
 */
class SigningTest {

    private val secret = "bGlmZWluLXRlc3Qtc2VjcmV0LWtleS0zMi1ieXRlcyE="
    private val body = """{"device_id":"pixel-7a","events":[]}"""

    @Test
    fun `signing string is method path timestamp and body digest`() {
        val message = Signing.signingString(
            method = "POST",
            path = "/ingest/events",
            timestamp = "1788768000",
            body = body.toByteArray(),
        )

        assertEquals(
            "POST\n/ingest/events\n1788768000\n" +
                "583c733aa7218d83853381698672eaf8e8091e50eff2740850aa47100e8843dc",
            String(message),
        )
    }

    @Test
    fun `signature matches the server implementation`() {
        val message = Signing.signingString(
            method = "POST",
            path = "/ingest/events",
            timestamp = "1788768000",
            body = body.toByteArray(),
        )

        assertEquals(
            "b91d913afc346c404c453bfb44154c134fa2d4464c6e930cd626f35514b65858",
            Signing.sign(secret, message),
        )
    }

    @Test
    fun `method is upper-cased and the digest covers an empty body`() {
        // 空 body 也要有摘要:少了它,一次心跳能被改成一次上报
        val message = Signing.signingString("post", "/ingest/heartbeat", "1", ByteArray(0))
        assertEquals(
            "POST\n/ingest/heartbeat\n1\n" +
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            String(message),
        )
    }
}
