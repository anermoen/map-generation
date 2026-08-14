package no.mapgen.aerialviewer

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import org.osmdroid.util.BoundingBox
import java.io.File

/** One bundled year of historical aerial imagery - generate_mbtiles.py's
 * output, read back from internal storage. bounds/minZoom/maxZoom come
 * straight from the MBTiles metadata table it wrote. */
data class HistoricalLayer(
    val year: Int,
    val file: File,
    val bounds: BoundingBox,
    val minZoom: Int,
    val maxZoom: Int,
)

object MbtilesCatalog {
    private const val ASSET_DIR = "mbtiles"
    private val YEAR_SUFFIX = Regex("(\\d{4})\\.mbtiles$")

    /** Copies every bundled .mbtiles from assets into internal storage -
     * SQLite needs real random file access, which a compressed APK
     * asset entry can't provide - then reads each one's own metadata to
     * build the catalog. Skips copying a file that's already present
     * locally (by name), so repeat launches stay fast; a new app
     * version that ships an additional year's filename still gets it
     * copied on its first run. */
    fun loadLayers(context: Context): List<HistoricalLayer> {
        val assetNames = context.assets.list(ASSET_DIR)?.filter { it.endsWith(".mbtiles") } ?: emptyList()
        val layers = mutableListOf<HistoricalLayer>()
        for (name in assetNames.sorted()) {
            val destFile = File(context.filesDir, name)
            if (!destFile.exists()) {
                context.assets.open("$ASSET_DIR/$name").use { input ->
                    destFile.outputStream().use { output -> input.copyTo(output) }
                }
            }
            val year = YEAR_SUFFIX.find(name)?.groupValues?.get(1)?.toIntOrNull() ?: continue
            val metadata = readMetadata(destFile) ?: continue
            layers.add(HistoricalLayer(year, destFile, metadata.bounds, metadata.minZoom, metadata.maxZoom))
        }
        return layers.sortedBy { it.year }
    }

    private data class Metadata(val bounds: BoundingBox, val minZoom: Int, val maxZoom: Int)

    private fun readMetadata(file: File): Metadata? {
        SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
            val values = mutableMapOf<String, String>()
            db.rawQuery("SELECT name, value FROM metadata", null).use { cursor ->
                while (cursor.moveToNext()) {
                    values[cursor.getString(0)] = cursor.getString(1)
                }
            }
            val parts = values["bounds"]?.split(",")?.map { it.trim().toDoubleOrNull() } ?: return null
            if (parts.size != 4 || parts.any { it == null }) return null
            val (west, south, east, north) = parts.map { it!! }
            return Metadata(
                bounds = BoundingBox(north, east, south, west),
                minZoom = values["minzoom"]?.toIntOrNull() ?: 10,
                maxZoom = values["maxzoom"]?.toIntOrNull() ?: 20,
            )
        }
    }
}
