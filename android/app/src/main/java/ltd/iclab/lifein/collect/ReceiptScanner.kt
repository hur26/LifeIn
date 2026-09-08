package ltd.iclab.lifein.collect

import android.content.Context
import android.net.Uri
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * 拍一张小票,认出金额和商户(P2 第 14 片)。
 *
 * **照片不出手机。** 识别在本机做,上传的只有认出来的那几个字段
 * ([ADR-021 的 2026-09 补充](../../../../../../../docs/04-tech-decisions.md))。
 * 决定这件事的是一条:照片里有的东西比一笔账多得多 —— 同行的人、邻桌、你的手。
 * [R10](../../../../../../../docs/05-risks.md) 对交易类的口径是只留金额、时间、
 * 商户,而**服务端从来没收到过的东西,不需要承诺"不留"**。
 *
 * 这个类薄得有意:它只负责"图片 → 一大段文字",挑金额那件事在
 * [ReceiptText] 里,那是纯函数,测得了。**错都在挑金额那一步**,
 * 而识别引擎认不认得出字是它自己的事。
 */
class ReceiptScanner(private val context: Context) {

    private val recognizer by lazy {
        TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
    }

    /**
     * 认一张图。**认不出来不是异常**,返回一个 `useful = false` 的结果 ——
     * 小票拍糊、光线不好都是日常,而弹一个错误对话框只会让人下次不再用这个功能。
     */
    suspend fun scan(uri: Uri): ReceiptText.Parsed {
        val text = recognize(uri)
        return ReceiptText.parse(text)
    }

    private suspend fun recognize(uri: Uri): String =
        suspendCancellableCoroutine { cont ->
            val image = InputImage.fromFilePath(context, uri)
            recognizer.process(image)
                .addOnSuccessListener { cont.resume(it.text) }
                .addOnFailureListener { cont.resumeWithException(it) }
        }
}
