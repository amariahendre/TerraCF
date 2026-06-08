import json
import html
import re
import time
import requests
import streamlit as st
import folium

from folium.plugins import Draw
from streamlit_folium import st_folium
from collections import defaultdict

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

# =========================================================
# FUNCTIONS
# =========================================================

def search_location(q):

    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": q + ", Romania",
            "format": "json",
            "limit": 1,
        }
        headers = {
            "User-Agent": "ancpi_polygon_export_app"
        }
        r = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=10,
        )
        data = r.json()
        if data:
            return [
                float(data[0]["lat"]),
                float(data[0]["lon"]),
            ]
        return None
    except Exception:
        return "unavailable"


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


@st.cache_data(show_spinner=False)
def get_object_ids(esri_geom_json):

    params = {

        "f": "json",

        "where":
            "NR_CARTE_FUNCIARA IS NOT NULL",

        "returnIdsOnly": "true",

        "geometry":
            esri_geom_json,

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


@st.cache_data(show_spinner=False)
def fetch_features_by_ids(object_ids):

    features = []

    chunk_size = 50

    progress = st.sidebar.progress(0)

    status = st.sidebar.empty()

    total = len(object_ids)

    for i in range(
        0,
        total,
        chunk_size
    ):

        chunk = object_ids[i:i + chunk_size]

        params = {

            "f": "geojson",

            "objectIds":
                ",".join(map(str, chunk)),

            "outFields": "*",

            "returnGeometry": "true",

            "outSR": "4326",
        }

        for attempt in range(3):
            try:
                r = requests.get(
                    LAYER_QUERY_URL,
                    params=params,
                    timeout=120,
                )
                r.raise_for_status()
                break
            except requests.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        data = r.json()

        features.extend(
            data.get("features", [])
        )

        progress.progress(
            min(len(features) / total, 1.0)
        )

        status.write(
            f"Downloaded "
            f"{len(features)} / {total} parcels..."
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

    # cf -> set of group names
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

        center = search_location(q)

        if center == "unavailable":

            st.warning(
                "Geocoding service unavailable. "
                "Navigate manually on the map."
            )

        elif center:

            st.session_state.center = center
            st.session_state.zoom = 15
            st.session_state.geojson = None

            st.rerun()

        else:

            st.warning(
                "Location not found."
            )

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

if st.session_state.geojson:

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
                json.dumps(esri_geom, sort_keys=True)
            )

            st.sidebar.info(
                f"Found "
                f"{len(object_ids)} parcels "
                f"with CF."
            )

            if object_ids:

                fc = fetch_features_by_ids(
                    tuple(object_ids)
                )

                st.session_state.geojson = fc

                kml = geojson_to_kml(
                    fc,
                    highlight_groups
                )

                st.sidebar.download_button(

                    "Download KML",

                    data=kml.encode("utf-8"),

                    file_name=
                        "ancpi_parcels.kml",

                    mime=
                        "application/vnd.google-earth.kml+xml",
                )

                st.sidebar.download_button(

                    "Download GeoJSON",

                    data=json.dumps(fc).encode(
                        "utf-8"
                    ),

                    file_name=
                        "ancpi_parcels.geojson",

                    mime=
                        "application/geo+json",
                )

            else:

                st.sidebar.warning(
                    "No parcels found "
                    "inside polygon."
                )
