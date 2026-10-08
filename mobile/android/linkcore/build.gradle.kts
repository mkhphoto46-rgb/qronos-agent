import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("org.jetbrains.kotlin.jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    // TLS 1.3 with an external pre-shared key; see PskTls.kt for why the
    // platform's own TLS cannot be used.
    implementation("org.bouncycastle:bctls-jdk18on:1.86.2")
    implementation("org.bouncycastle:bcprov-jdk18on:1.86")

    // Android ships org.json in the platform. On the JVM — the tests and the
    // interop driver — the same API comes from the Maven artifact.
    compileOnly("org.json:json:20260814")
    testImplementation("org.json:json:20260814")
    testImplementation("junit:junit:4.13.2")
}

tasks.test {
    systemProperty("qronos.protocolDir", rootProject.file("../protocol").absolutePath)
}
