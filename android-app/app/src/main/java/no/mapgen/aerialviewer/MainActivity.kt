package no.mapgen.aerialviewer

import android.Manifest
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Spinner
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.floatingactionbutton.FloatingActionButton
import org.osmdroid.config.Configuration
import org.osmdroid.tileprovider.modules.MBTilesFileArchive
import org.osmdroid.tileprovider.modules.MapTileFileArchiveProvider
import org.osmdroid.tileprovider.modules.SimpleRegisterReceiver
import org.osmdroid.tileprovider.tilesource.TileSourceFactory
import org.osmdroid.tileprovider.tilesource.XYTileSource
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.TilesOverlay
import org.osmdroid.views.overlay.mylocation.GpsMyLocationProvider
import org.osmdroid.views.overlay.mylocation.MyLocationNewOverlay

/** Shows a bundled year of historical aerial photo (see
 * generate_mbtiles.py / MbtilesCatalog) with the phone's live GPS
 * position on top, so walking the property shows where you are on
 * that year's photo - the whole point of this app. A year spinner
 * swaps which HistoricalLayer's tiles are shown; the GPS "my location"
 * overlay is independent of it and stays on across every year. */
class MainActivity : AppCompatActivity() {

    private lateinit var mapView: MapView
    private lateinit var locationOverlay: MyLocationNewOverlay
    private var layers: List<HistoricalLayer> = emptyList()
    private var currentTilesOverlay: TilesOverlay? = null

    private val requestLocationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) locationOverlay.enableMyLocation() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // osmdroid requires a distinct user agent (OSM tile-usage policy)
        // for the online base map fallback - see the comment on
        // setTileSource() below.
        Configuration.getInstance().userAgentValue = packageName
        setContentView(R.layout.activity_main)

        mapView = findViewById(R.id.map)
        mapView.setMultiTouchControls(true)
        // Plain OSM streets as a fallback base layer, visible only where
        // no bundled historical overlay covers the screen (zoomed out
        // past the property, or no network at all - it just won't
        // load, which is fine: the historical overlay + GPS dot over
        // the property itself never needs it).
        mapView.setTileSource(TileSourceFactory.DEFAULT_TILE_SOURCE)

        locationOverlay = MyLocationNewOverlay(GpsMyLocationProvider(this), mapView)
        mapView.overlays.add(locationOverlay)

        layers = MbtilesCatalog.loadLayers(this)
        if (layers.isEmpty()) {
            findViewById<TextView>(R.id.property_label).text = getString(R.string.no_years_found)
            findViewById<Spinner>(R.id.year_spinner).visibility = View.GONE
        } else {
            setupYearSpinner()
            showLayer(layers.last())   // most recent year by default
        }

        findViewById<FloatingActionButton>(R.id.recenter_button).setOnClickListener {
            locationOverlay.myLocation?.let { location -> mapView.controller.animateTo(location) }
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
            == PackageManager.PERMISSION_GRANTED
        ) {
            locationOverlay.enableMyLocation()
        } else {
            requestLocationPermission.launch(Manifest.permission.ACCESS_FINE_LOCATION)
        }
    }

    private fun setupYearSpinner() {
        val spinner = findViewById<Spinner>(R.id.year_spinner)
        val years = layers.map { it.year.toString() }
        spinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, years)
        spinner.setSelection(years.size - 1)
        spinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                showLayer(layers[position])
            }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
    }

    private fun showLayer(layer: HistoricalLayer) {
        currentTilesOverlay?.let { mapView.overlays.remove(it) }

        val tileSource = XYTileSource(
            "historical-${layer.year}", layer.minZoom, layer.maxZoom, 256, ".png", arrayOf()
        )
        val archive = MBTilesFileArchive.getDatabaseFileArchive(layer.file)
        val provider = MapTileFileArchiveProvider(SimpleRegisterReceiver(this), tileSource, arrayOf(archive))
        val tilesOverlay = TilesOverlay(provider, this)
        tilesOverlay.loadingBackgroundColor = Color.TRANSPARENT
        tilesOverlay.loadingLineColor = Color.TRANSPARENT

        // Keep the historical overlay below the GPS dot (which must
        // always render on top) but above the base map.
        val insertAt = mapView.overlays.indexOf(locationOverlay).coerceAtLeast(0)
        mapView.overlays.add(insertAt, tilesOverlay)
        currentTilesOverlay = tilesOverlay

        findViewById<TextView>(R.id.property_label).text = layer.year.toString()
        mapView.zoomToBoundingBox(layer.bounds, false, 50)
        mapView.invalidate()
    }

    override fun onResume() {
        super.onResume()
        mapView.onResume()
        if (::locationOverlay.isInitialized) locationOverlay.enableMyLocation()
    }

    override fun onPause() {
        super.onPause()
        mapView.onPause()
        if (::locationOverlay.isInitialized) locationOverlay.disableMyLocation()
    }
}
