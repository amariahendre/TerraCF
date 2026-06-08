import json
import html
import re
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

    max_workers = 8

    progress = st.sidebar.progress(0)

    status = st.sidebar.empty()

    total = len(object_ids)

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
                min(done_ids / total, 1.0)
            )

            status.write(
                f"Downloaded "
                f"{done_ids} / {total} parcels..."
            )

    progress.empty()
    status.empty()

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


def geojson_to_kml(
    fc,
    highlight_groups
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

    def make_placemark(feature, style_id):

        props = feature.get("properties", {})

        geom = feature.get("geometry")

        cf = props.get("NR_CARTE_FUNCIARA", "")

        identifier = props.get("IDENTIFIER", "")

        name = (
            f"CF {cf}"
            if cf
            else f"Parcel {identifier}"
        )

        desc = ""

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

    # =====================================================
    # HIGHLIGHTED GROUPS — cu subgrupuri pe UAT
    # =====================================================

    for group_name, uat_map in sorted(highlighted_folders.items()):

        uat_subfolders = []

        for uat, features in sorted(uat_map.items()):

            placemarks = [
                make_placemark(feature, "yellowMarkedStyle")
                for feature in features
            ]

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

    return f"""<?xml version="1.0"
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

  {''.join(folder_kml)}

</Document>
</kml>
"""


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

    st.divider()

    export_clicked = st.button(
        "Export KML from polygon",
        use_container_width=True,
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

# SATELLITE BASEMAP

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

# OSM BASEMAP

folium.TileLayer(
    tiles="OpenStreetMap",
    name="OpenStreetMap",
    overlay=False,
    control=True,
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

folium.LayerControl().add_to(m)

map_data = st_folium(

    m,

    height=930,

    use_container_width=True,

    returned_objects=[
        "all_drawings"
    ],
)

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

                st.session_state.kml = geojson_to_kml(
                    fc,
                    highlight_groups
                )

            else:

                st.session_state.geojson = None
                st.session_state.kml = None

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
