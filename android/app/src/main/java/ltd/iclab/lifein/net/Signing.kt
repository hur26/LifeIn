package ltd.iclab.lifein.net

import java.security.MessageDigest
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * 请求签名 —— [06 §6.2](../../../../../../../docs/06-data-model.md#6-接口契约) 的手机端一半。
 * 服务端那一半在 `lifein/api/auth.py`,两边必须逐字对应。
 *
 * 四样东西进签名:方法、路径、时间戳、**请求体的摘要**。少任何一样,
 * 截获的请求都能被改成另一个意思重放 —— 少了 path,一次心跳能被改成一次上报。
 *
 * 因为要对**实际发出去的那串字节**签名,所以这里收 `ByteArray` 而不是对象:
 * 序列化一次、签一次、发同一份。这也正是不用 Retrofit 的原因(ADR-021)——
 * 它把序列化藏在转换器后面,拿到那串字节要绕一圈。
 */
object Signing {

    const val HEADER_USER = "X-LifeIn-User"
    const val HEADER_DEVICE = "X-LifeIn-Device"
    const val HEADER_TIMESTAMP = "X-LifeIn-Timestamp"
    const val HEADER_SIGNATURE = "X-LifeIn-Signature"

    fun signingString(method: String, path: String, timestamp: String, body: ByteArray): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256").digest(body).toHex()
        return listOf(method.uppercase(), path, timestamp, digest)
            .joinToString("\n")
            .toByteArray()
    }

    /**
     * 密钥是 base64 存的,签之前解回字节 —— 拿 base64 字符串本身当密钥也能跑,
     * 但那样有效熵少四分之一,而两边得用同一种做法才验得过。
     */
    fun sign(secretBase64: String, message: ByteArray): String {
        // 用 java.util.Base64 而不是 android.util.Base64:前者在 API 26 起就有
        // (我们的 minSdk 正是 26),而且它不依赖安卓运行时 —— 于是这个对象
        // 能在**普通 JVM 单元测试**里跑,签名算法能拿服务端那份向量对一遍
        val key = Base64.getDecoder().decode(secretBase64)
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(message).toHex()
    }

    fun headers(
        userId: String,
        deviceId: String,
        timestamp: String,
        signature: String,
    ): Map<String, String> = mapOf(
        HEADER_USER to userId,
        HEADER_DEVICE to deviceId,
        HEADER_TIMESTAMP to timestamp,
        HEADER_SIGNATURE to signature,
    )

    /** 服务端比的是小写十六进制。大小写不一致的表现是"一直 401 而且不说原因"。 */
    private fun ByteArray.toHex(): String =
        joinToString("") { "%02x".format(it) }
}
