# release 暂时不开混淆(build.gradle.kts 里写了理由)。
# 将来开的话,kotlinx.serialization 生成的 serializer 要留住:
-keepclassmembers class ** {
    *** Companion;
}
-keepclasseswithmembers class ** {
    kotlinx.serialization.KSerializer serializer(...);
}
