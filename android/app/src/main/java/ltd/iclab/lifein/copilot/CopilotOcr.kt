package ltd.iclab.lifein.copilot

import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.TextRecognizer
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions

/**
 * 本机离线中文 OCR。**打包版 ML Kit,不是 Play 服务版。**
 *
 * 这个选择和小票识别那边是同一个(ADR-021 的 2026-09 补充):免捆绑版依赖
 * Google Play 服务,而目标机型是国内主力机 —— 那上面不一定有,
 * 而缺了的表现是**静默地识别不出来**,和"这一屏本来就没字"长得一模一样。
 * 代价是 APK 大一些,而这个模型已经为小票在包里了,副驾用它是**零新增体积**。
 *
 * **图片不出手机**(09 §3)。识别在这台机器上做,位图用完就 recycle。
 */
class CopilotOcr {

    /** 一行识别结果。坐标已经换算回**屏幕坐标**,和节点矩形同一个空间。 */
    data class Line(val text: String, val top: Int, val bottom: Int)

    /**
     * 认一块区域里的字。[region] 是**位图坐标**,调用方负责换算。
     *
     * 回调在主线程上,而且只会被调一次 —— 失败也调,给的是空列表:
     * 一块区域认不出字是很正常的事(表情、图片、语音条),
     * 不该让整轮识别停在这里等一个永远不来的回调。
     */
    fun recognize(bitmap: Bitmap, region: Rect, onResult: (List<Line>) -> Unit) {
        val clipped = Rect(region)
        if (!clipped.intersect(0, 0, bitmap.width, bitmap.height) ||
            clipped.width() < MIN_SIDE ||
            clipped.height() < MIN_SIDE
        ) {
            main.post { onResult(emptyList()) }
            return
        }

        val cropped = runCatching {
            Bitmap.createBitmap(bitmap, clipped.left, clipped.top, clipped.width(), clipped.height())
        }.getOrElse {
            Log.w(TAG, "裁剪失败:${it::class.simpleName}")
            main.post { onResult(emptyList()) }
            return
        }

        val image = runCatching { InputImage.fromBitmap(cropped, 0) }.getOrElse {
            cropped.recycle()
            main.post { onResult(emptyList()) }
            return
        }

        client.process(image)
            .addOnSuccessListener { text ->
                val lines = text.textBlocks
                    .flatMap { it.lines }
                    .mapNotNull { line ->
                        val box = line.boundingBox ?: return@mapNotNull null
                        val body = line.text.trim()
                        if (body.isEmpty()) return@mapNotNull null
                        Line(body, box.top + clipped.top, box.bottom + clipped.top)
                    }
                    .sortedBy { it.top }
                cropped.recycle()
                main.post { onResult(lines) }
            }
            .addOnFailureListener { error ->
                Log.w(TAG, "识别失败:${error::class.simpleName}")
                cropped.recycle()
                main.post { onResult(emptyList()) }
            }
    }

    private val main = Handler(Looper.getMainLooper())

    companion object {
        private const val TAG = "LifeIn/copilot"

        /** 比这还小的区域不用试了,多半是个头像或者一个已读角标。 */
        private const val MIN_SIDE = 8

        /** 全进程一个识别器 —— 造它就是在加载那个打包模型。 */
        private val client: TextRecognizer by lazy {
            TextRecognition.getClient(ChineseTextRecognizerOptions.Builder().build())
        }

        /**
         * 提前把模型加载起来。
         *
         * 第一次识别要付模型加载的钱,而那一次是在**截屏回调里**跑的 ——
         * 那个回调在主线程上。服务连上时在工作线程上先叫一次,
         * 把这笔开销挪到没人等的时候。
         */
        fun warmUp() {
            runCatching { client }
        }
    }
}
