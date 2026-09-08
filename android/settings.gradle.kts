// 安卓端的构建入口。服务端那半边是 Python,两边各build各的 —— 它们之间的
// 唯一契约是 HTTP 接口(06 §6),不是任何构建产物。
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "LifeIn"
include(":app")
