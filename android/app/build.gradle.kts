plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

android {
    namespace = "ltd.iclab.lifein"
    compileSdk = 35

    defaultConfig {
        applicationId = "ltd.iclab.lifein"
        // minSdk 26 的理由不是覆盖率:NotificationListenerService 在 8.0 之后
        // 才有稳定的重连行为,而 ADR-012 的硬前提是"装在日常主力机上",
        // 那种机器不会停在 7.x(ADR-021)
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            // 混淆先关着:这个 App 只装在自己手机上,而混淆会让崩溃栈变成天书,
            // 而崩溃栈是单人项目唯一的排查手段(ADR-021 否决了崩溃上报)
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

// 库的 schema 要能进版本库:手机上那个库升级失败的表现是"打开就闪退",
// 而没有 schema 就写不出真正的迁移(Db.kt 里那两条)
ksp {
    arg("room.schemaLocation", "$projectDir/schemas")
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    // 协程写明版本而不是靠 work/room 的 ktx 传递进来:
    // 传递依赖的版本会跟着别的库跳,而这个 App 的每个后台入口都在用它
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // 定时拉、心跳、上报重试。ADR-020 点名的就是它
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    // 上报队列与日历幂等表。DAO 手写是 bug 的产地(ADR-021)
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // 签名要对**发出去的字节**签,所以不用 Retrofit(ADR-021)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    testImplementation("junit:junit:4.13.2")
}
