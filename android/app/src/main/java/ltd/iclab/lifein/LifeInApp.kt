package ltd.iclab.lifein

import android.app.Application
import ltd.iclab.lifein.data.Secrets
import ltd.iclab.lifein.work.Schedules

/**
 * 进程级的装配 —— 和服务端 `bootstrap.py` 是同一个角色:
 * **只有这里知道谁依赖谁**,别的地方靠构造函数收依赖。
 *
 * 安卓上这件事有个额外的理由:通知监听服务、WorkManager 的任务、界面
 * 三者跑在不同的入口上,但它们要用同一份凭据和同一个数据库。
 * 各自 new 一份的话,"到底配没配过"会有三个不同的答案。
 */
class LifeInApp : Application() {

    val secrets: Secrets by lazy { Secrets(this) }

    override fun onCreate() {
        super.onCreate()
        instance = this
        // 每次进程起来都排一遍。KEEP 的语义是"已经排着就别动",
        // 所以重复调用是免费的 —— 而漏调一次的表现是"心跳悄悄停了"
        Schedules.ensure(this)
    }

    companion object {
        lateinit var instance: LifeInApp
            private set
    }
}
