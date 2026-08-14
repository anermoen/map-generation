plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "no.mapgen.aerialviewer"
    compileSdk = 34

    defaultConfig {
        applicationId = "no.mapgen.aerialviewer"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
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
    kotlinOptions {
        jvmTarget = "17"
    }

    // MBTiles files are plain SQLite databases - AAPT would otherwise
    // try to compress them into the APK, which breaks SQLite's random
    // file access when read straight out of the archive.
    androidResources {
        noCompress += "mbtiles"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // Offline-first Android map view with built-in MBTiles archive support
    // (org.osmdroid.tileprovider.modules.MBTilesFileArchive) and a GPS
    // "my location" overlay - see MainActivity.kt.
    implementation("org.osmdroid:osmdroid-android:6.1.20")
}
