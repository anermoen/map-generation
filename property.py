#!/usr/bin/env python3
"""
Fetch a Norwegian cadastral property boundary (eiendomsgrense / "teig")
from Kartverket's open WFS service, given a kommune name and a
gnr/bnr (gardsnummer/bruksnummer).

Data source
-----------
"Matrikkelen - Eiendomskart Teig" WFS, served by Kartverket via Geonorge -
an open (no authentication, no API key) service, updated daily from the
national cadastre (matrikkelen):

    https://wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig

The "Teig" feature type is a property parcel: its 'område' (area) field
holds the boundary polygon (EPSG:25833 - ETRS89 / UTM zone 33N, the
standard projected CRS for Norwegian national mapping), and
'matrikkelnummerTekst' holds a human-readable "gnr/bnr" string (e.g.
"123/9") generated from the linked matrikkelenhet(er).

Kommunenummer lookup
----------------------
Kommune *names* are not queryable directly against the Teig service (it
only has kommunenummer, the 4-digit official code) - and that code isn't
stable across years (a 2024 nationwide renumbering changed many of them,
including Etnedal: 0541 -> 3450). Rather than hardcoding a name->number
table that will eventually go stale, this module resolves the current
kommunenummer at request time via Kartverket's Kommuneinfo API:

    https://ws.geonorge.no/kommuneinfo/v1/sok?knavn=<name>

Usage
-----
    python3 property.py --kommune Etnedal --gnr 123 --bnr 9

Fetches the boundary, prints a summary, and saves the polygon to
property_polygon.csv - a real CSV, opens directly as a spreadsheet in
Numbers/Excel (see write_polygon_csv) - in the property's own output
folder (default: "<gnr>-<bnr>-<kommune>", e.g. 123-9-Etnedal - see
property_code(); pass --outdir to override).

or as a library:

    from property import fetch_property
    prop = fetch_property("Etnedal", 123, 9)
    prop.polygon        # shapely Polygon/MultiPolygon, EPSG:25833
    prop.kommunenavn
    prop.matrikkelnummer

read_polygon_csv(path) parses a saved property_polygon.csv back into the
same shapely geometry, if you need to work from the cached file directly
rather than re-fetching (e.g. during a Kartverket WFS outage, or to save
a redundant network round trip if you already ran property.py once for
the same property in the same session).
"""

import argparse
import csv
import os
from dataclasses import dataclass
from xml.etree import ElementTree

import requests
from shapely.geometry import Polygon, MultiPolygon

KOMMUNEINFO_URL = "https://ws.geonorge.no/kommuneinfo/v1/sok"
TEIG_WFS_URL = "https://wfs.geonorge.no/skwms1/wfs.matrikkelen-eiendomskart-teig"
TEIG_CRS = "EPSG:25833"

NS = {
    "wfs": "http://www.opengis.net/wfs/2.0",
    "gml": "http://www.opengis.net/gml/3.2",
    "app": "http://skjema.geonorge.no/SOSI/produktspesifikasjon/Matrikkelen-Eiendomskart-Teig/20211101",
}


def _ring_coords(ring_elem):
    """Parse a gml:LinearRing's gml:posList into a list of (x, y) tuples."""
    pos_list = ring_elem.find("gml:posList", NS)
    values = [float(v) for v in pos_list.text.split()]
    return list(zip(values[0::2], values[1::2]))


def _parse_gml_polygon(polygon_elem):
    """Parse a single gml:Polygon (exterior + optional interior rings)."""
    exterior = _ring_coords(polygon_elem.find("gml:exterior/gml:LinearRing", NS))
    interiors = [
        _ring_coords(ring)
        for ring in polygon_elem.findall("gml:interior/gml:LinearRing", NS)
    ]
    return Polygon(exterior, interiors)


def _parse_teig_geometry(omrade_elem):
    """The 'område' (area) element wraps either a single gml:Polygon or a
    gml:MultiSurface of several - Teig features linked to disjoint patches
    (e.g. around a lake, or split by an administrative boundary) use the
    latter."""
    polygon_elem = omrade_elem.find("gml:Polygon", NS)
    if polygon_elem is not None:
        return _parse_teig_geometry_from_polygons([polygon_elem])

    multi_elem = omrade_elem.find("gml:MultiSurface", NS)
    if multi_elem is not None:
        polygons = multi_elem.findall(
            "gml:surfaceMember/gml:Polygon", NS
        ) or multi_elem.findall("gml:surfaceMember/gml:Surface", NS)
        return _parse_teig_geometry_from_polygons(polygons)

    raise ValueError("Unrecognized geometry structure in 'område' element")


def _parse_teig_geometry_from_polygons(polygon_elems):
    polygons = [_parse_gml_polygon(p) for p in polygon_elems]
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


@dataclass
class Property:
    kommunenavn: str
    kommunenummer: str
    gardsnummer: int
    bruksnummer: int
    matrikkelnummer: str
    polygon: object   # shapely geometry, EPSG:25833

    @property
    def code(self):
        return property_code(self.gardsnummer, self.bruksnummer, self.kommunenavn)


def property_code(gardsnummer, bruksnummer, kommune_navn):
    """The property identifier used consistently as both the per-property
    output folder name and the prefix on every file inside it (e.g.
    "123-9-Etnedal") - so the folder/filenames alone establish which
    property they belong to, without needing to open a manifest. Uses
    the kommune name exactly as given/resolved (not the kommunenummer),
    matching how a human would refer to the property, and matching the
    naming convention used for manually-captured screenshots dropped
    into that folder (<gardsnummer>-<bruksnummer>-<kommune>-<year>.png)."""
    return f"{gardsnummer}-{bruksnummer}-{kommune_navn}"


def get_kommunenummer(kommune_navn):
    """Look up the current official kommunenummer for a kommune name."""
    resp = requests.get(KOMMUNEINFO_URL, params={"knavn": kommune_navn}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data["antallTreff"] == 0:
        raise ValueError(f"No kommune found matching {kommune_navn!r}")
    if data["antallTreff"] > 1:
        names = [k["kommunenavn"] for k in data["kommuner"]]
        raise ValueError(f"Ambiguous kommune name {kommune_navn!r}: matches {names}")
    return data["kommuner"][0]["kommunenummer"], data["kommuner"][0]["kommunenavn"]


def fetch_property(kommune_navn, gardsnummer, bruksnummer):
    """Fetch the Teig (parcel) geometry for gnr/bnr in the given kommune.

    Raises ValueError if zero or more than one matching Teig is found -
    a Teig can in principle be linked to several matrikkelenheter (see
    the WFS schema's matrikkelnummerTekst documentation), which would
    need disambiguating rather than silently picking one.
    """
    kommunenummer, kommunenavn_official = get_kommunenummer(kommune_navn)
    matrikkelnummer = f"{gardsnummer}/{bruksnummer}"

    xml_filter = (
        '<Filter xmlns="http://www.opengis.net/fes/2.0">'
        "<And>"
        "<PropertyIsEqualTo><ValueReference>kommunenummer</ValueReference>"
        f"<Literal>{kommunenummer}</Literal></PropertyIsEqualTo>"
        "<PropertyIsEqualTo><ValueReference>matrikkelnummerTekst</ValueReference>"
        f"<Literal>{matrikkelnummer}</Literal></PropertyIsEqualTo>"
        "</And></Filter>"
    )
    params = {
        # No outputFormat specified: this WFS only serves GML (text/xml;
        # subtype=gml/3.2.1), not GeoJSON - confirmed via GetCapabilities.
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "app:Teig", "srsName": TEIG_CRS,
        "Filter": xml_filter,
    }
    resp = requests.get(TEIG_WFS_URL, params=params, timeout=30)
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.content)

    members = root.findall("wfs:member/app:Teig", NS) or root.findall(
        "gml:featureMember/app:Teig", NS
    )
    if len(members) == 0:
        raise ValueError(
            f"No property found for {matrikkelnummer} in {kommunenavn_official} "
            f"(kommunenummer {kommunenummer})"
        )
    if len(members) > 1:
        raise ValueError(
            f"{len(members)} Teig features matched {matrikkelnummer} in "
            f"{kommunenavn_official} - expected exactly 1; inspect manually"
        )

    teig = members[0]
    polygon = _parse_teig_geometry(teig.find("app:område", NS))
    return Property(
        kommunenavn=kommunenavn_official,
        kommunenummer=kommunenummer,
        gardsnummer=gardsnummer,
        bruksnummer=bruksnummer,
        matrikkelnummer=matrikkelnummer,
        polygon=polygon,
    )


def _iter_polygon_parts(geometry):
    """Yields each Polygon making up geometry - just itself for a plain
    Polygon, or each part for a MultiPolygon (a Teig split into disjoint
    patches, e.g. by a lake or an administrative boundary - see
    _parse_teig_geometry)."""
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type == "MultiPolygon":
        yield from geometry.geoms
    else:
        raise ValueError(f"Unsupported geometry type: {geometry.geom_type}")


def polygon_csv_path(outdir):
    return os.path.join(outdir, "property_polygon.csv")


def write_polygon_csv(prop, outdir):
    """Write prop.polygon (EPSG:25833) to <outdir>/property_polygon.csv -
    a real CSV (opens natively as a spreadsheet table in Numbers/Excel,
    unlike a .dat file, which macOS has no default app for even though
    the content is plain text): a few commented metadata lines, then one
    header row and one data row per boundary point - columns part, ring,
    easting, northing. "ring" is "exterior" or "interior_<j>" (a hole -
    rare for a single cadastral parcel, but possible); "part" is 1 for a
    plain Polygon, or the patch number for a MultiPolygon (a Teig split
    into disjoint patches, e.g. by a lake or an administrative
    boundary - see _parse_teig_geometry). This round-trips exactly -
    read_polygon_csv parses it back into the same shapely geometry.
    Returns the path written."""
    os.makedirs(outdir, exist_ok=True)
    path = polygon_csv_path(outdir)
    parts = list(_iter_polygon_parts(prop.polygon))
    with open(path, "w") as f:
        f.write("# Property boundary polygon, from Kartverket's cadastre (property.py)\n")
        f.write(f"# matrikkelnummer: {prop.matrikkelnummer}\n")
        f.write(f"# kommune: {prop.kommunenavn} (kommunenummer {prop.kommunenummer})\n")
        f.write("# crs: EPSG:25833 (ETRS89 / UTM zone 33N)\n")
        f.write(f"# geometry_type: {prop.polygon.geom_type}\n")
        f.write(f"# area_m2: {prop.polygon.area:.3f}\n")
        f.write("part,ring,easting,northing\n")
        for i, poly in enumerate(parts, start=1):
            for x, y in poly.exterior.coords:
                f.write(f"{i},exterior,{x:.3f},{y:.3f}\n")
            for j, interior in enumerate(poly.interiors, start=1):
                for x, y in interior.coords:
                    f.write(f"{i},interior_{j},{x:.3f},{y:.3f}\n")
    return path


def read_polygon_csv(path):
    """Parse a property_polygon.csv file (see write_polygon_csv) back
    into a shapely Polygon/MultiPolygon in EPSG:25833 - the counterpart
    to write_polygon_csv, so the file is a real cache, not just a
    human-readable dump."""
    parts = {}   # part index -> {"exterior": [...], "interiors": {j: [...]}}
    with open(path, newline="") as f:
        reader = csv.reader(row for row in f if not row.startswith("#"))
        header = next(reader)   # part,ring,easting,northing
        assert header == ["part", "ring", "easting", "northing"], \
            f"unexpected property_polygon.csv header: {header}"
        for part_s, ring, x_s, y_s in reader:
            part_i = int(part_s)
            parts.setdefault(part_i, {"exterior": [], "interiors": {}})
            point = (float(x_s), float(y_s))
            if ring == "exterior":
                parts[part_i]["exterior"].append(point)
            else:
                j = int(ring.split("_")[1])
                parts[part_i]["interiors"].setdefault(j, []).append(point)

    polygons = []
    for i in sorted(parts):
        p = parts[i]
        interiors = [p["interiors"][j] for j in sorted(p["interiors"])]
        polygons.append(Polygon(p["exterior"], interiors))
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def main():
    ap = argparse.ArgumentParser(
        description="Fetch a cadastral property boundary and save it to property_polygon.dat.")
    ap.add_argument("--kommune", required=True)
    ap.add_argument("--gnr", type=int, required=True)
    ap.add_argument("--bnr", type=int, required=True)
    ap.add_argument("--outdir", default=None,
                     help="default: a folder named after the property itself, "
                          "'<gnr>-<bnr>-<kommune>' (e.g. 123-9-Etnedal) - see property_code()")
    args = ap.parse_args()

    prop = fetch_property(args.kommune, args.gnr, args.bnr)
    print(f"Kommune:          {prop.kommunenavn} ({prop.kommunenummer})")
    print(f"Matrikkelnummer:  {prop.matrikkelnummer}")
    print(f"Geometry type:    {prop.polygon.geom_type}")
    print(f"Area:             {prop.polygon.area:,.0f} m^2")
    print(f"Bounds (EPSG:25833): {prop.polygon.bounds}")

    outdir = args.outdir or prop.code
    path = write_polygon_csv(prop, outdir)
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
