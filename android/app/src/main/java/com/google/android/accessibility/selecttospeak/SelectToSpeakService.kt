package com.google.android.accessibility.selecttospeak

import ltd.iclab.lifein.copilot.ChatCaptureService

/**
 * 副驾的读屏服务,**注册在这个类名下**。逻辑一行都不在这里,
 * 全部在 [ChatCaptureService] —— 唯一的区别就是类名。
 *
 * ## 为什么要换个名字
 *
 * 微信 8.0.52 起对无障碍服务隐藏节点文本:普通命名的服务读到的是一棵
 * 只有一个空节点的树,而这个名字读得到完整的聊天。
 * 这是**这个项目里唯一一处刻意对抗第三方 App 的代码**,
 * [ADR-035](../../../../../../../docs/04-tech-decisions.md) 把它单独列出来并写明了它的性质。
 *
 * ## ADR-035 同时写了一条承诺
 *
 * **微信如果把这条也堵了,这个项目不追。** 不做新的绕法、不做协议逆向、
 * 不做 Xposed、不改安装包。那时候的表现是 [ltd.iclab.lifein.copilot.CopilotState.EMPTY_TREE]
 * ——"在聊天窗里但一句都读不到",而那是 P5 的退出条件之一。
 *
 * 所以:**不要给这个类改名,也不要改清单里的注册名。**
 * 但也不要为了让它继续有效而去找下一个名字。
 */
class SelectToSpeakService : ChatCaptureService()
