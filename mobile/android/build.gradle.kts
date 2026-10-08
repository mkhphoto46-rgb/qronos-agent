// Android Gradle Plugin 9 compiles Kotlin itself ("built-in Kotlin"), and the
// org.jetbrains.kotlin.android plugin is incompatible with it, so the app
// module applies only com.android.application. The JVM-only protocol core
// uses the ordinary Kotlin JVM plugin; declaring its version here also sets
// the Kotlin version AGP's built-in support uses.
plugins {
    id("com.android.application") version "9.4.1" apply false
    id("org.jetbrains.kotlin.jvm") version "2.4.21" apply false
}
