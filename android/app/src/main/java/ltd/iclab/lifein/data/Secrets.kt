package ltd.iclab.lifein.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlinx.serialization.json.Json

/**
 * 设备凭据的落地 —— [R11] 要求"长期凭据存安卓 Keystore,不落明文文件"。
 *
 * 做法和服务端 `crypto.py` 是同一个思路:**密钥管理交给系统,数据自己加**。
 * Keystore 里放的是一把不可导出的 AES/GCM 密钥,两把设备密钥用它加密之后
 * 才写进 SharedPreferences。手机被 root、prefs 文件被拖走,拿到的是密文,
 * 而解它的钥匙在 TEE 里出不来。
 *
 * 不用 `EncryptedSharedPreferences`:那个库长期停在 alpha 且已不再推荐,
 * 而这里自己做只有六十行(ADR-021)。
 *
 * **不做用户认证绑定**(`setUserAuthenticationRequired`):采集器要在锁屏状态下
 * 上报,绑了指纹它就上报不了 —— 而"锁屏期间不采集"等于这个 App 大部分时间不工作。
 */
class Secrets(context: Context) {

    private val prefs = context.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    val isEnrolled: Boolean
        get() = prefs.contains(KEY_BLOB)

    fun save(enrollment: Enrollment) {
        val plain = Json.encodeToString(Enrollment.serializer(), enrollment)
        prefs.edit().putString(KEY_BLOB, encrypt(plain)).apply()
    }

    /** 读不出来就当没配过:密钥被系统换掉(恢复出厂、换机)时会走到这里。 */
    fun load(): Enrollment? {
        val blob = prefs.getString(KEY_BLOB, null) ?: return null
        return try {
            Json.decodeFromString(Enrollment.serializer(), decrypt(blob))
        } catch (e: Exception) {
            // 解不开就是解不开。**不要静默清掉** —— 让用户看到"要重新配码",
            // 比悄悄变成未配置状态好:后者的表现是"App 打开一切正常,就是不采集"
            null
        }
    }

    /** 解绑。手机要送去修、或者要换一台的时候先跑这个,再去服务端吊销。 */
    fun clear() {
        prefs.edit().remove(KEY_BLOB).apply()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val body = cipher.doFinal(plain.toByteArray())
        // iv 拼在前面。GCM 的 iv 不是秘密,但**绝不能重复用**,
        // 所以每次加密都让 Cipher 自己生成一个新的
        val packed = cipher.iv + body
        return Base64.getEncoder().encodeToString(packed)
    }

    private fun decrypt(blob: String): String {
        val packed = Base64.getDecoder().decode(blob)
        val iv = packed.copyOfRange(0, IV_LENGTH)
        val body = packed.copyOfRange(IV_LENGTH, packed.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(TAG_BITS, iv))
        return String(cipher.doFinal(body))
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (store.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        generator.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private companion object {
        const val FILE = "lifein.device"
        const val KEY_BLOB = "enrollment"
        const val PROVIDER = "AndroidKeyStore"
        const val ALIAS = "lifein.device.key"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_LENGTH = 12
        const val TAG_BITS = 128
    }
}
