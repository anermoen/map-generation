# Aerial Time Viewer (Android)

Shows a bundled year of historical aerial photo (from this project's
`generate_mbtiles.py`) with your phone's live GPS position on top - walk
the property, see where you are on that year's photo. A year spinner at
the top switches which year's imagery is shown; the GPS "my location"
dot stays on across every year.

## Status

Scaffolded and written against osmdroid's well-established, long-stable
MBTiles API (`MBTilesFileArchive` + `MapTileFileArchiveProvider` +
`TilesOverlay`, the same pattern osmdroid's own sample apps use) - but
**not yet build-verified**: this was written outside Android Studio, in
an environment with no Android SDK/emulator to compile or run it
against. Open it in Android Studio and try a build; if the exact
osmdroid version resolved pulls in a slightly different API shape than
expected, the fix is almost certainly a small one (an import path or a
constructor parameter) in `MainActivity.kt`, not a structural rewrite.

## Opening the project

1. Android Studio -> Open -> select this `android-app/` folder.
2. **Gradle wrapper note**: `gradle/wrapper/gradle-wrapper.properties`
   is included (points at Gradle 8.7), but the actual wrapper JAR isn't
   - it's a small compiled binary, not something to hand-write as text.
   Android Studio will offer to generate/repair it on first sync (or:
   Tools -> "regenerate wrapper", or just let Studio use its own
   bundled Gradle for the first sync). This is expected, not a sign
   something's broken.
3. Let Gradle sync (downloads AndroidX/Material/osmdroid/etc. - needs
   network the first time; nothing at runtime does, per below).
4. Run on a device or emulator with location services available (an
   emulator can fake GPS via its Extended Controls; a real phone is
   more representative of the actual "walk the property" use case).

## How imagery gets here

`app/src/main/assets/mbtiles/` is bundled straight from this project's
Python pipeline - nothing in the Android project regenerates it:

    cd ..
    python3 generate_mbtiles.py --kommune Nittedal --gnr 14 --bnr 987
    cp 14-987-Nittedal/*.mbtiles android-app/app/src/main/assets/mbtiles/

Currently bundled: all 14 fitted years of 14/987 Nittedal (~9 MB). To
add another property (e.g. 123/9 Etnedal), generate its MBTiles the
same way and copy them in too - `MbtilesCatalog` picks up every
`*.mbtiles` file in that folder automatically, keyed by the trailing
`_<year>.mbtiles` in its filename (see `property_code()` in the Python
side for the `<gnr>-<bnr>-<kommune>` naming convention these follow).
Mixing two different properties' years in the same spinner isn't
handled yet - see "Not yet done" below.

MBTiles files are excluded from APK compression
(`androidResources { noCompress += "mbtiles" }` in `app/build.gradle.kts`)
because SQLite needs real random file access, which a compressed asset
entry can't provide - `MbtilesCatalog.loadLayers()` copies each one from
`assets/` into the app's internal storage on first launch (skipped on
later launches if a same-named file is already there) and opens it from
its real filesystem path.

## Why osmdroid, and why bundled (not a tile server)

Chosen over Mapbox/MapLibre because it needs no API key/account - just
a Gradle dependency - and it has built-in MBTiles archive support, so
pan/zoom/projection math and offline raster tiles are both already
solved rather than hand-rolled. Imagery ships bundled inside the APK
(vs. a self-hosted tile server) per this phase's own scoping decision:
simplest to build and fully offline in the field, at the cost of a
rebuild/reinstall to add a property or a new year later.

## Not yet done

- Only one property's years show in the spinner at a time (whichever
  are in `assets/mbtiles/`) - no in-app property picker across multiple
  bundled properties yet.
- No app icon beyond a placeholder vector drawable
  (`res/drawable/ic_launcher.xml`) - fine for development, worth a real
  design pass before any real distribution.
- Real-device GPS accuracy (a few meters typically) isn't visually
  reconciled against the fitted imagery's own RMSE (also a few meters,
  per the main README) - the two error sources will compound a bit;
  not addressed here.
