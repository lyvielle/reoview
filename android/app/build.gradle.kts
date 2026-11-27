plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.gms.google-services")
}

// Version: major.minor from android/VERSION, patch and versionCode from the CI run number.
val build = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
val keystore = System.getenv("KEYSTORE_FILE")

android {
    namespace = "io.github.lyvielle.reoview"
    compileSdk = 35
    defaultConfig {
        applicationId = "io.github.lyvielle.reoview"
        minSdk = 29
        targetSdk = 34  // shortcut: 35 forces edge-to-edge and would need inset handling, raise when Play or Android requires it
        versionCode = build
        versionName = rootProject.file("VERSION").readText().trim() + ".$build"
    }
    signingConfigs {
        create("release") {
            if (keystore != null) {
                storeFile = file(keystore)
                storePassword = System.getenv("KEYSTORE_PASSWORD")
                keyAlias = "reoview"
                keyPassword = System.getenv("KEYSTORE_PASSWORD")
            }
        }
    }
    buildTypes {
        release {
            if (keystore != null) signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

dependencies {
    implementation(platform("com.google.firebase:firebase-bom:33.5.1"))
    implementation("com.google.firebase:firebase-messaging")
}
