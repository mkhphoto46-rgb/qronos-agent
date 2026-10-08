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

rootProject.name = "qronos-remote"

// The protocol core is plain Kotlin/JVM and builds and tests without the
// Android SDK. The app needs the SDK; pass -Pqronos.withApp=false to build
// and test the core alone on a machine that does not have it.
include(":linkcore")

if (providers.gradleProperty("qronos.withApp").orNull != "false") {
    include(":app")
}
