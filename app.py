import json
import html
import re
import math
import requests
import streamlit as st
import folium

from folium.plugins import Draw
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim, Photon
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# =========================================================
# CONFIG
# =========================================================

LAYER_QUERY_URL = (
    "https://geoportal.ancpi.ro/"
    "hosted_services/rest/services/"
    "INIS/INIS_Viewer/MapServer/4/query"
)

WMS_URL = (
    "https://geoportal.ancpi.ro/"
    "hosted_services/services/"
    "INIS/INIS_Viewer/MapServer/WMSServer"
)

# UAT (administrative unit) boundaries come from OpenStreetMap
# via the Overpass API. These are the same dashed limits the OSM
# basemap shows (admin_level = 8 = comune / orașe / municipii),
# but fetched as vector data so we can draw them in red. Coverage
# is country-wide and the data is in WGS84, so it aligns with the
# web-mercator basemap.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

st.set_page_config(
    page_title="ANCPI Parcel Export",
    layout="wide",
)

# =========================================================
# DEFAULT MAP POSITION
# =========================================================

if "center" not in st.session_state:
    st.session_state.center = [44.75, 27.70]

if "zoom" not in st.session_state:
    st.session_state.zoom = 9

if "geojson" not in st.session_state:
    st.session_state.geojson = None

if "kml" not in st.session_state:
    st.session_state.kml = None

if "nonconform" not in st.session_state:
    st.session_state.nonconform = 0

if "uat_geojson" not in st.session_state:
    st.session_state.uat_geojson = None

# Current map view, captured from st_folium each render, so the
# "Load UAT limits" button can query the right extent and so the
# map stays put when it is rebuilt with the overlay.
if "last_bounds" not in st.session_state:
    st.session_state.last_bounds = None

if "last_view_center" not in st.session_state:
    st.session_state.last_view_center = None

if "last_view_zoom" not in st.session_state:
    st.session_state.last_view_zoom = None

# Max number of parcels to draw on the interactive map.
# Above this, st_folium would freeze the browser, so we
# skip rendering and only offer the download files.
MAX_RENDER_FEATURES = 1500

# =========================================================
# FUNCTIONS
# =========================================================

def _geocode_with(geolocator, query):

    # Try once, retry a couple of times on transient errors.
    # Returns (coords, found_flag). Raises only if the service
    # is unreachable on every attempt.

    last_error = None

    for _attempt in range(3):

        try:

            loc = geolocator.geocode(query)

            if loc:
                return [loc.latitude, loc.longitude]

            return None

        except (GeocoderServiceError, GeocoderTimedOut) as exc:

            last_error = exc

    raise last_error


def search_location(q):

    # Both Nominatim and Photon are free public services with
    # no API key. On shared hosts (e.g. Streamlit Cloud) the
    # server IP often gets rate-limited, so we try Nominatim
    # first and fall back to Photon before giving up. Errors
    # are returned as a string instead of crashing the app.

    query = q + ", Romania"

    geocoders = [
        Nominatim(
            user_agent="ancpi_polygon_export_app (amaria.hendre@gmail.com)",
            timeout=10,
        ),
        Photon(
            user_agent="ancpi_polygon_export_app (amaria.hendre@gmail.com)",
            timeout=10,
        ),
    ]

    service_down = True

    for geolocator in geocoders:

        try:

            coords = _geocode_with(geolocator, query)

            service_down = False

            if coords:
                return coords, None

        except (GeocoderServiceError, GeocoderTimedOut):

            # This provider is unavailable — try the next one.
            continue

    if service_down:

        return None, (
            "Geocoding services are temporarily unavailable "
            "(rate-limited). Please try again in a moment."
        )

    return None, "Location not found."


def parse_highlight_groups(text):

    groups = {}

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        if ":" in line:

            group_name, cf_list = line.split(":", 1)

            group_name = group_name.strip()

        else:

            group_name = "Highlighted CF Numbers"
            cf_list = line

        cfs = {
            x.strip()
            for x in re.split(r"[,\s]+", cf_list)
            if x.strip()
        }

        groups.setdefault(
            group_name,
            set()
        ).update(cfs)

    return groups


def geojson_polygon_to_esri_geometry(
    geojson_geom
):

    coords = geojson_geom["coordinates"]

    if geojson_geom["type"] == "Polygon":

        rings = coords

    elif geojson_geom["type"] == "MultiPolygon":

        rings = []

        for polygon in coords:
            rings.extend(polygon)

    else:

        raise ValueError(
            "Please draw a polygon or rectangle."
        )

    return {
        "rings": rings,
        "spatialReference": {
            "wkid": 4326
        },
    }


def get_object_ids(esri_geom):

    params = {

        "f": "json",

        "where":
            "NR_CARTE_FUNCIARA IS NOT NULL",

        "returnIdsOnly": "true",

        "geometry":
            json.dumps(esri_geom),

        "geometryType":
            "esriGeometryPolygon",

        "inSR": "4326",

        "spatialRel":
            "esriSpatialRelIntersects",
    }

    r = requests.get(
        LAYER_QUERY_URL,
        params=params,
        timeout=120
    )

    r.raise_for_status()

    data = r.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data.get("objectIds", [])


def _bounds_span(bounds):

    sw, ne = bounds["_southWest"], bounds["_northEast"]

    return max(
        ne["lat"] - sw["lat"],
        ne["lng"] - sw["lng"],
    )


def fetch_uat_boundaries(bounds):

    # Fetch UAT (admin_level=8) boundaries from OpenStreetMap for
    # the current map view. bounds comes from st_folium and looks
    # like {"_southWest": {"lat","lng"}, "_northEast": {...}}.
    #
    # We draw the boundary ways as red lines, so there is no need
    # to assemble closed multipolygon rings — each way is emitted
    # as its own LineString, tagged with the UAT name.

    sw = bounds.get("_southWest")
    ne = bounds.get("_northEast")

    if not sw or not ne:
        return None

    south, west = sw["lat"], sw["lng"]
    north, east = ne["lat"], ne["lng"]

    query = (
        "[out:json][timeout:60];"
        'rel["boundary"="administrative"]'
        '["admin_level"="8"]'
        f"({south},{west},{north},{east});"
        "out geom;"
    )

    r = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers={
            "User-Agent":
                "ancpi_parcel_export/1.0 "
                "(amaria.hendre@gmail.com)",
        },
        timeout=90,
    )

    r.raise_for_status()

    data = r.json()

    features = []

    for el in data.get("elements", []):

        if el.get("type") != "relation":
            continue

        name = (el.get("tags", {}) or {}).get("name", "")

        for member in el.get("members", []):

            if member.get("type") != "way":
                continue

            geom = member.get("geometry")

            if not geom or len(geom) < 2:
                continue

            coords = [[p["lon"], p["lat"]] for p in geom]

            features.append({
                "type": "Feature",
                "properties": {"name": name},
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords,
                },
            })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def _fetch_chunk(chunk):

    params = {

        "f": "geojson",

        "objectIds":
            ",".join(map(str, chunk)),

        "outFields": "*",

        "returnGeometry": "true",

        "outSR": "4326",
    }

    r = requests.get(
        LAYER_QUERY_URL,
        params=params,
        timeout=120
    )

    r.raise_for_status()

    return r.json().get("features", [])


def fetch_features_by_ids(object_ids):

    features = []

    chunk_size = 250

    # Keep concurrency low: the ANCPI server is slow and tends
    # to queue parallel requests, which makes the progress bar
    # jump at the end instead of moving smoothly. A few workers
    # speed things up while still giving steady progress.
    max_workers = 4

    total = len(object_ids)

    # Show the bar immediately so the user sees it before any
    # request returns.
    progress = st.sidebar.progress(
        0,
        text=f"Downloading 0 / {total} parcels...",
    )

    chunks = [
        object_ids[i:i + chunk_size]
        for i in range(0, total, chunk_size)
    ]

    done_ids = 0

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        futures = {
            executor.submit(_fetch_chunk, chunk): len(chunk)
            for chunk in chunks
        }

        for future in as_completed(futures):

            features.extend(future.result())

            done_ids += futures[future]

            progress.progress(
                min(done_ids / total, 1.0),
                text=f"Downloading "
                     f"{done_ids} / {total} parcels...",
            )

    progress.empty()

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def polygon_to_kml(coords):

    outer = " ".join([
        f"{x},{y},0"
        for x, y in coords[0]
    ])

    holes = ""

    for hole in coords[1:]:

        hole_coords = " ".join([
            f"{x},{y},0"
            for x, y in hole
        ])

        holes += f"""
        <innerBoundaryIs>
          <LinearRing>
            <coordinates>
              {hole_coords}
            </coordinates>
          </LinearRing>
        </innerBoundaryIs>
        """

    return f"""
    <Polygon>
      <outerBoundaryIs>
        <LinearRing>
          <coordinates>
            {outer}
          </coordinates>
        </LinearRing>
      </outerBoundaryIs>

      {holes}

    </Polygon>
    """


def geom_to_kml(geom):

    if not geom:
        return ""

    if geom["type"] == "Polygon":

        return polygon_to_kml(
            geom["coordinates"]
        )

    if geom["type"] == "MultiPolygon":

        parts = "".join([
            polygon_to_kml(poly)
            for poly in geom["coordinates"]
        ])

        return f"""
        <MultiGeometry>
          {parts}
        </MultiGeometry>
        """

    return ""


# =========================================================
# PARCEL WIDTH (pure Python — no shapely/pyproj, so it works
# on Python 3.14 where those wheels may be missing)
# =========================================================

def _shoelace_area(pts):

    s = 0.0

    n = len(pts)

    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1

    return abs(s) / 2.0


def _convex_hull(points):

    # Andrew's monotone chain. Returns hull vertices (no repeat
    # of the first point at the end).

    pts = sorted(set(points))

    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - \
               (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and \
                cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and \
                cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _min_width(points):

    # Minimum width of the polygon = smallest distance between
    # two parallel lines that enclose it (rotating calipers on
    # the convex hull). Points must already be in meters.

    hull = _convex_hull(points)

    n = len(hull)

    if n < 3:
        return None

    min_w = float("inf")

    for i in range(n):

        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]

        ex, ey = bx - ax, by - ay

        elen = math.hypot(ex, ey)

        if elen == 0:
            continue

        # Max perpendicular distance of any hull vertex from
        # the line through this edge.
        max_d = 0.0

        for px, py in hull:
            d = abs(ex * (py - ay) - ey * (px - ax)) / elen
            if d > max_d:
                max_d = d

        if max_d < min_w:
            min_w = max_d

    return min_w if min_w != float("inf") else None


def _geom_min_width(geom):

    # Minimum width in meters of a GeoJSON Polygon/MultiPolygon
    # given in lon/lat (WGS84). Uses a local equirectangular
    # projection — accurate to well under a meter at parcel
    # scale. For MultiPolygon, the largest part is measured.
    # This is the geometry-based fallback used only when the
    # API perimeter/area fields are missing or the parcel is
    # not rectangle-like.

    if not geom:
        return None

    t = geom.get("type")

    if t == "Polygon":
        polys = [geom["coordinates"]]
    elif t == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return None

    if not polys or not polys[0] or not polys[0][0]:
        return None

    lon0, lat0 = polys[0][0][0][0], polys[0][0][0][1]

    k = math.cos(math.radians(lat0))

    def proj(pt):
        return (
            (pt[0] - lon0) * 111320.0 * k,
            (pt[1] - lat0) * 110540.0,
        )

    best_ring = None
    best_area = -1.0

    for rings in polys:

        outer = [proj(p) for p in rings[0]]

        area = _shoelace_area(outer)

        if area > best_area:
            best_area = area
            best_ring = outer

    if not best_ring:
        return None

    return _min_width(best_ring)


# Candidate API field names for perimeter (length) and area.
# ANCPI's "Imobile" layer uses SHAPE.LEN (m) and SHAPE.AREA (m2),
# but we list common variants so the code keeps working if the
# service is reconfigured.
_PERIM_FIELDS = [
    "SHAPE.LEN", "SHAPE_LEN", "SHAPE.STLength()",
    "Shape_Length", "SHAPE_Length", "PERIMETRU", "Perimeter",
]

_AREA_FIELDS = [
    "SHAPE.AREA", "SHAPE_AREA", "SHAPE.STArea()",
    "Shape_Area", "SHAPE_Area", "SUPRAFATA", "Suprafata", "ARIE",
]


def _first_positive(props, names):

    for n in names:
        if n in props:
            try:
                v = float(props[n])
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v

    return None


def parcel_dimensions(feature):

    # Returns (width_m, length_m, source).
    #
    # Primary method: the parcel is modeled as a rectangle a x b.
    # The API gives perimeter P and area A directly, so:
    #     a + b = P / 2     (sum)
    #     a * b = A         (product)
    # a and b are the roots of  x^2 - (P/2) x + A = 0 :
    #     x = (P/2 +/- sqrt((P/2)^2 - 4A)) / 2
    # width = smaller root, length = larger root.
    #
    # If the fields are missing, or the shape is far from a
    # rectangle (negative discriminant), we fall back to the
    # geometry-based minimum width.

    props = feature.get("properties", {}) or {}

    P = _first_positive(props, _PERIM_FIELDS)
    A = _first_positive(props, _AREA_FIELDS)

    if P and A:

        s = P / 2.0
        disc = s * s - 4.0 * A

        if disc >= 0:
            root = math.sqrt(disc)
            a = (s + root) / 2.0
            b = (s - root) / 2.0
            return min(a, b), max(a, b), "rect"

    # Fallback for irregular parcels / missing fields.
    w = _geom_min_width(feature.get("geometry"))

    return w, None, ("geom" if w is not None else None)


def geojson_to_kml(
    fc,
    highlight_groups,
    min_width_m=30.0,
):

    # group_name -> uat -> [features]
    highlighted_folders = defaultdict(lambda: defaultdict(list))

    uat_folders = defaultdict(list)

    # Build a mapping cf -> set of group names
    # (a CF could theoretically belong to multiple groups)
    cf_to_groups = defaultdict(set)

    for group_name, cfs in highlight_groups.items():
        for cf in cfs:
            cf_to_groups[str(cf).strip()].add(group_name)

    for feature in fc["features"]:

        props = feature.get("properties", {})

        cf = str(
            props.get("NR_CARTE_FUNCIARA", "")
        ).strip()

        uat = props.get("UAT", "NO_UAT") or "NO_UAT"

        groups_for_cf = cf_to_groups.get(cf)

        if groups_for_cf:
            # Add the feature to every group it belongs to,
            # under its own UAT sub-folder
            for group_name in groups_for_cf:
                highlighted_folders[group_name][uat].append(feature)
        else:
            uat_folders[uat].append(feature)

    def make_placemark(
        feature,
        style_id,
        width_m=None,
        length_m=None,
        source=None,
    ):

        props = feature.get("properties", {})

        geom = feature.get("geometry")

        cf = props.get("NR_CARTE_FUNCIARA", "")

        identifier = props.get("IDENTIFIER", "")

        name = (
            f"CF {cf}"
            if cf
            else f"Parcel {identifier}"
        )

        nonconform = (
            width_m is not None
            and width_m < min_width_m
        )

        if nonconform:
            name = f"⚠ NECONFORM ({width_m:.1f} m) - {name}"

        desc = ""

        if width_m is not None:

            src_label = (
                "from perimeter & area"
                if source == "rect"
                else "from geometry (irregular)"
            )

            desc += (
                f"<b>Width</b>: {width_m:.1f} m "
                f"({'NONCONFORM' if nonconform else 'OK'}, "
                f"threshold {min_width_m:.0f} m)<br/>"
            )

            if length_m is not None:
                desc += (
                    f"<b>Length</b>: {length_m:.1f} m<br/>"
                )

            desc += (
                f"<i>Dimensions {src_label}</i><br/><br/>"
            )

        for k, v in props.items():

            desc += (
                f"<b>{html.escape(str(k))}</b>: "
                f"{html.escape(str(v))}<br/>"
            )

        return f"""
        <Placemark>

          <name>
            {html.escape(str(name))}
          </name>

          <styleUrl>
            #{style_id}
          </styleUrl>

          <description><![CDATA[
            {desc}
          ]]></description>

          {geom_to_kml(geom)}

        </Placemark>
        """

    folder_kml = []

    nonconform_count = 0

    # =====================================================
    # HIGHLIGHTED GROUPS — cu subgrupuri pe UAT
    # Parcels narrower than min_width_m are flagged red.
    # =====================================================

    for group_name, uat_map in sorted(highlighted_folders.items()):

        uat_subfolders = []

        for uat, features in sorted(uat_map.items()):

            placemarks = []

            for feature in features:

                width_m, length_m, source = parcel_dimensions(
                    feature
                )

                if (
                    width_m is not None
                    and width_m < min_width_m
                ):
                    nonconform_count += 1
                    style_id = "redNonConformStyle"
                else:
                    style_id = "yellowMarkedStyle"

                placemarks.append(
                    make_placemark(
                        feature,
                        style_id,
                        width_m,
                        length_m,
                        source,
                    )
                )

            uat_subfolders.append(f"""
        <Folder>

          <name>
            {html.escape(str(uat))}
          </name>

          {''.join(placemarks)}

        </Folder>
        """)

        folder_kml.append(f"""
        <Folder>

          <name>
            {html.escape(str(group_name))}
          </name>

          {''.join(uat_subfolders)}

        </Folder>
        """)

    # =====================================================
    # UAT FOLDERS
    # =====================================================

    for uat, features in sorted(uat_folders.items()):

        placemarks = [
            make_placemark(feature, "blueParcelStyle")
            for feature in features
        ]

        folder_kml.append(f"""
        <Folder>

          <name>
            {html.escape(str(uat))}
          </name>

          {''.join(placemarks)}

        </Folder>
        """)

    kml_doc = f"""<?xml version="1.0"
encoding="UTF-8"?>

<kml xmlns="http://www.opengis.net/kml/2.2">

<Document>

  <name>
    ANCPI parcel export
  </name>

  <!-- BLUE STYLE -->

  <Style id="blueParcelStyle">

    <LineStyle>
      <color>ffff0000</color>
      <width>2</width>
    </LineStyle>

    <PolyStyle>
      <color>80ff0000</color>
      <fill>1</fill>
      <outline>1</outline>
    </PolyStyle>

  </Style>

  <!-- YELLOW STYLE -->

  <Style id="yellowMarkedStyle">

    <LineStyle>
      <color>ff00ffff</color>
      <width>3</width>
    </LineStyle>

    <PolyStyle>
      <color>8000ffff</color>
      <fill>1</fill>
      <outline>1</outline>
    </PolyStyle>

  </Style>

  <!-- RED STYLE (non-conform width) -->

  <Style id="redNonConformStyle">

    <LineStyle>
      <color>ff0000ff</color>
      <width>3</width>
    </LineStyle>

    <PolyStyle>
      <color>800000ff</color>
      <fill>1</fill>
      <outline>1</outline>
    </PolyStyle>

  </Style>

  {''.join(folder_kml)}

</Document>
</kml>
"""

    return kml_doc, nonconform_count


# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:

    st.title(
        "ANCPI parcel export"
    )

    q = st.text_input(

        "Search location",

        value="",

        placeholder="Victoria, Braila"

    )

    if st.button(
        "Go to location",
        use_container_width=True
    ):

        center, error = search_location(q)

        if center:

            st.session_state.center = center
            st.session_state.zoom = 15
            st.session_state.geojson = None

            st.rerun()

        else:

            st.warning(error)

    st.divider()

    marked_cf_text = st.text_area(

        "Highlighted CF groups",

        placeholder=
            "Example:\n"
            "CU 65: 70155\n"
            "Environmental study: 70425, 70426",

        height=220,
    )

    highlight_groups = parse_highlight_groups(
        marked_cf_text
    )

    total_highlighted = sum(
        len(cfs)
        for cfs in highlight_groups.values()
    )

    st.caption(
        f"{total_highlighted} highlighted "
        f"CF numbers in "
        f"{len(highlight_groups)} folders."
    )

    min_width_m = st.number_input(
        "Non-conform width threshold (m)",
        min_value=0.0,
        value=30.0,
        step=1.0,
        help=
            "Highlighted parcels narrower than this width "
            "are flagged red (non-conform) in the KML. "
            "Change it to whatever limit you need.",
    )

    st.divider()

    export_clicked = st.button(
        "Export KML from polygon",
        use_container_width=True,
    )

    st.divider()

    # ---- UAT / cadastral limits overlay (drawing aid) ----

    show_uat = st.checkbox(
        "Show UAT limits (red)",
        value=st.session_state.get("show_uat", False),
        help=
            "Draws the UAT (commune / town) administrative "
            "boundaries in red. Set the zoom/area on the map "
            "first, then click the button below to load them. "
            "Source: OpenStreetMap (admin_level 8).",
    )

    st.session_state.show_uat = show_uat

    if st.button(
        "Load UAT limits for current view",
        use_container_width=True,
    ):

        b = st.session_state.last_bounds

        if not b:
            st.warning(
                "Move the map once, then click again."
            )

        elif _bounds_span(b) > 1.0:
            st.warning(
                "Zoom in first — the current view is too "
                "large to load UAT limits."
            )

        else:

            with st.spinner("Loading UAT limits..."):
                try:
                    data = fetch_uat_boundaries(b)
                except Exception as exc:
                    data = None
                    st.error(
                        f"Could not load UAT limits "
                        f"(OpenStreetMap busy): {exc}"
                    )

            if data is not None:

                st.session_state.uat_geojson = data
                st.session_state.show_uat = True
                show_uat = True

                # Keep the map on the area the user was viewing
                # when they clicked (the map is rebuilt below
                # with the overlay).
                if st.session_state.last_view_center:
                    st.session_state.center = \
                        st.session_state.last_view_center
                if st.session_state.last_view_zoom:
                    st.session_state.zoom = \
                        st.session_state.last_view_zoom

                if data.get("features"):
                    n_uat = len({
                        f["properties"].get("name")
                        for f in data["features"]
                        if f["properties"].get("name")
                    })
                    st.caption(
                        f"{n_uat} UAT boundary/-ies loaded."
                    )
                else:
                    st.info(
                        "No UAT boundaries found in this view."
                    )

    st.markdown(
    """
    <style>

    [data-testid="stSidebar"] > div:first-child {
        padding-bottom: 80px;
    }

    .custom-footer {
        position: fixed;
        bottom: 0;
        left: 0;
        width: 21rem;
        padding: 10px 15px;
        background: white;
        border-top: 1px solid #e6e6e6;

        font-size: 11px;
        color: gray;
        line-height: 1.4;
        z-index: 999999;
    }

    .custom-footer a {
        color: #4F8BF9;
        text-decoration: none;
    }

    </style>

    <div class="custom-footer">
    Created by
    <a href="https://ro.linkedin.com/in/amariahendre"
       target="_blank">
       Ana-Maria Hendre
    </a>

    </div>
    """,
    unsafe_allow_html=True,
)

# =========================================================
# MAP
# =========================================================

m = folium.Map(
    location=st.session_state.center,
    zoom_start=st.session_state.zoom,
    tiles=None,
)

# OSM BASEMAP
# Added first; the LAST base layer added is the one shown by
# default, so satellite (added after) stays selected even after
# the map is rebuilt for a UAT refresh.

folium.TileLayer(
    tiles="OpenStreetMap",
    name="OpenStreetMap",
    overlay=False,
    control=True,
).add_to(m)

# SATELLITE BASEMAP (default)

folium.TileLayer(
    tiles=
    "https://server.arcgisonline.com/"
    "ArcGIS/rest/services/"
    "World_Imagery/MapServer/"
    "tile/{z}/{y}/{x}",

    attr="Esri World Imagery",

    name="Satellite imagery",

    overlay=False,

    control=True,

    max_zoom=22,

).add_to(m)

# ANCPI PARCELS

folium.raster_layers.WmsTileLayer(

    url=WMS_URL,

    layers="4",

    name="ANCPI Parcels",

    fmt="image/png",

    transparent=True,

    overlay=True,

    control=True,

).add_to(m)

# DRAW TOOLS

Draw(

    export=False,

    draw_options={

        "polyline": False,
        "rectangle": True,
        "polygon": True,
        "circle": False,
        "marker": False,
        "circlemarker": False,
    },

    edit_options={
        "edit": True,
        "remove": True,
    },

).add_to(m)

# DISPLAY DOWNLOADED FEATURES
# Only render on the interactive map if the parcel count is
# small enough. Above MAX_RENDER_FEATURES, st_folium would
# serialize every polygon to the browser and freeze the tab —
# the files are still available via the download buttons.

_feature_count = (
    len(st.session_state.geojson["features"])
    if st.session_state.geojson
    else 0
)

if st.session_state.geojson and _feature_count <= MAX_RENDER_FEATURES:

    folium.GeoJson(

        st.session_state.geojson,

        name="Downloaded Parcels",

        tooltip=folium.GeoJsonTooltip(

            fields=[
                "NR_CARTE_FUNCIARA",
                "IDENTIFIER",
                "UAT",
                "LOCALITATE",
            ],

            aliases=[
                "CF",
                "Identifier",
                "UAT",
                "Locality",
            ],
        ),

    ).add_to(m)

# UAT / CADASTRAL LIMITS (red drawing aid)

if (
    show_uat
    and st.session_state.uat_geojson
    and st.session_state.uat_geojson.get("features")
):

    folium.GeoJson(

        st.session_state.uat_geojson,

        name="UAT limits",

        style_function=lambda _f: {
            "color": "#ff0000",
            "weight": 2.5,
            "opacity": 1,
        },

        tooltip=folium.GeoJsonTooltip(
            fields=["name"],
            aliases=["UAT"],
        ),

    ).add_to(m)

folium.LayerControl().add_to(m)

map_data = st_folium(

    m,

    height=930,

    use_container_width=True,

    returned_objects=[
        "all_drawings",
        "bounds",
        "center",
        "zoom",
    ],
)

# Remember the current map view so the "Load UAT limits" button
# can query the right extent, and so the map stays on this view
# when it is rebuilt with the overlay after the button is clicked.

if map_data:

    if map_data.get("bounds"):
        st.session_state.last_bounds = map_data["bounds"]

    c = map_data.get("center")
    if c:
        st.session_state.last_view_center = [c["lat"], c["lng"]]

    z = map_data.get("zoom")
    if z:
        st.session_state.last_view_zoom = z

# =========================================================
# EXPORT
# =========================================================

if export_clicked:

    drawings = (
        map_data.get("all_drawings")
        if map_data
        else None
    )

    if not drawings:

        st.sidebar.error(
            "Please draw a polygon first."
        )

    else:

        geom = drawings[-1]["geometry"]

        with st.spinner(
            "Downloading ANCPI parcels..."
        ):

            esri_geom = (
                geojson_polygon_to_esri_geometry(
                    geom
                )
            )

            object_ids = get_object_ids(
                esri_geom
            )

            st.sidebar.info(
                f"Found "
                f"{len(object_ids)} parcels "
                f"with CF."
            )

            if object_ids:

                fc = fetch_features_by_ids(
                    object_ids
                )

                st.session_state.geojson = fc

                kml, nonconform = geojson_to_kml(
                    fc,
                    highlight_groups,
                    min_width_m,
                )

                st.session_state.kml = kml
                st.session_state.nonconform = nonconform

            else:

                st.session_state.geojson = None
                st.session_state.kml = None
                st.session_state.nonconform = 0

                st.sidebar.warning(
                    "No parcels found "
                    "inside polygon."
                )

# =========================================================
# DOWNLOAD BUTTONS
# =========================================================
# Rendered unconditionally from session_state so they survive
# the rerun that Streamlit triggers when a download_button is
# clicked (otherwise the first download wipes out the others).

if st.session_state.geojson and st.session_state.kml:

    n_features = len(st.session_state.geojson["features"])

    if n_features > MAX_RENDER_FEATURES:

        st.sidebar.info(
            f"{n_features} parcels downloaded. "
            f"Too many to draw on the map — "
            f"use the download buttons below."
        )

    if st.session_state.nonconform:

        st.sidebar.error(
            f"⚠ {st.session_state.nonconform} highlighted "
            f"parcel(s) non-conform (width < "
            f"{min_width_m:.0f} m) — marked red in the KML."
        )

    st.sidebar.download_button(

        "Download KML",

        data=st.session_state.kml.encode("utf-8"),

        file_name=
            "ancpi_parcels.kml",

        mime=
            "application/vnd.google-earth.kml+xml",

        use_container_width=True,
    )

    st.sidebar.download_button(

        "Download GeoJSON",

        data=json.dumps(
            st.session_state.geojson
        ).encode("utf-8"),

        file_name=
            "ancpi_parcels.geojson",

        mime=
            "application/geo+json",

        use_container_width=True,
    )
