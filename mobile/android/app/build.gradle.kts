plugins {
    id("com.android.application")
}

android {
    namespace = "app.qronos.remote"
    compileSdk = 36

    defaultConfig {
        // A placeholder identity until the project settles its publisher name;
        // changing it later only matters once an app has been distributed.
        applicationId = "app.qronos.remote"
        // Android 8.0: java.util.Base64, AndroidKeyStore AES-GCM and
        // ConnectivityManager.requestNetwork with a timeout are all present.
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources {
            // BouncyCastle's jars each carry the same multi-release OSGi
            // manifest; one copy is enough and none is needed at run time.
            excludes += setOf("META-INF/versions/9/OSGI-INF/MANIFEST.MF")
        }
    }
}

dependencies {
    implementation(project(":linkcore"))
}
