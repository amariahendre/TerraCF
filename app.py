import json
import html
import re
import math
import logging
import requests
import streamlit as st
import folium

from folium.plugins import Draw
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim, Photon
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed


# Erorile tehnice vor apărea în logurile Streamlit Cloud,
# nu pe ecranul utilizatorului.
logger = logging.getLogger(__name__)


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

if "last_bounds" not in st.session_state:
    st.session_state.last_bounds = None

if "last_view_center" not in st.session_state:
    st.session_state.last_view_center = None

if "last_view_zoom" not in st.session_state:
    st.session_state.last_view_zoom = None

if "show_uat" not in st.session_state:
    st.session_state.show_uat = False


MAX_RENDER_FEATURES = 1500


# =========================================================
# FUNCTIONS
# =========================================================

def _geocode_with(geolocator, query):

    last_error = None

    for _attempt in range(3):

        try:

            loc = geolocator.geocode(query)

            if loc:
                return [loc.latitude, loc.longitude]

            return None

        except (GeocoderServiceError, GeocoderTimedOut) as exc:

            last_error = exc

    if last_error:
        raise last_error

    return None


def search_location(q):

    query = q + ", Romania"

    geocoders = [
        Nominatim(
            user_agent=(
                "ancpi_polygon_export_app "
                "(amaria.hendre@gmail.com)"
            ),
            timeout=10,
        ),
        Photon(
            user_agent=(
                "ancpi_polygon_export_app "
                "(amaria.hendre@gmail.com)"
            ),
            timeout=10,
        ),
    ]

    service_down = True

    for geolocator in geocoders:

        try:

            coords = _geocode_with(
                geolocator,
                query,
            )

            service_down = False

            if coords:
                return coords, None

        except (
            GeocoderServiceError,
            GeocoderTimedOut,
        ):

            continue

    if service_down:

        return None, (
            "Serviciile de căutare a localităților sunt "
            "temporar indisponibile. Încercați din nou."
        )

    return None, "Localitatea nu a fost găsită."


def parse_highlight_groups(text):

    groups = {}

    for line in text.splitlines():

        line = line.strip()

        if not line:
            continue

        if ":" in line:

            group_name, cf_list = line.split(
                ":",
                1,
            )

            group_name = group_name.strip()

        else:

            group_name = "Highlighted CF Numbers"
            cf_list = line

        cfs = {
            x.strip()
            for x in re.split(
                r"[,\s]+",
                cf_list,
            )
            if x.strip()
        }

        groups.setdefault(
            group_name,
            set(),
        ).update(cfs)

    return groups


def geojson_polygon_to_esri_geometry(
    geojson_geom,
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
            "wkid": 4326,
        },
    }


def get_object_ids(esri_geom):

    params = {
        "f": "json",
        "where": "NR_CARTE_FUNCIARA IS NOT NULL",
        "returnIdsOnly": "true",
        "geometry": json.dumps(esri_geom),
        "geometryType": "esriGeometryPolygon",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
    }

    r = requests.get(
        LAYER_QUERY_URL,
        params=params,
        timeout=120,
    )

    r.raise_for_status()

    data = r.json()

    if "error" in data:
        raise RuntimeError(
            data["error"]
        )

    return data.get(
        "objectIds",
        [],
    )


def _bounds_span(bounds):

    sw = bounds["_southWest"]
    ne = bounds["_northEast"]

    return max(
        ne["lat"] - sw["lat"],
        ne["lng"] - sw["lng"],
    )


def fetch_uat_boundaries(bounds):

    sw = bounds.get("_southWest")
    ne = bounds.get("_northEast")

    if not sw or not ne:
        return None

    south = sw["lat"]
    west = sw["lng"]
    north = ne["lat"]
    east = ne["lng"]

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
            "User-Agent": (
                "ancpi_parcel_export/1.0 "
                "(amaria.hendre@gmail.com)"
            ),
        },
        timeout=90,
    )

    r.raise_for_status()

    data = r.json()

    features = []

    for el in data.get(
        "elements",
        [],
    ):

        if el.get("type") != "relation":
            continue

        name = (
            el.get("tags", {}) or {}
        ).get("name", "")

        for member in el.get(
            "members",
            [],
        ):

            if member.get("type") != "way":
                continue

            geom = member.get("geometry")

            if not geom or len(geom) < 2:
                continue

            coords = [
                [p["lon"], p["lat"]]
                for p in geom
            ]

            features.append({
                "type": "Feature",
                "properties": {
                    "name": name,
                },
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
        "objectIds": ",".join(
            map(str, chunk)
        ),
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
    }

    r = requests.get(
        LAYER_QUERY_URL,
        params=params,
        timeout=120,
    )

    r.raise_for_status()

    data = r.json()

    if "error" in data:
        raise RuntimeError(
            data["error"]
        )

    return data.get(
        "features",
        [],
    )


def fetch_features_by_ids(object_ids):

    features = []

    chunk_size = 250
    max_workers = 4
    total = len(object_ids)

    if total == 0:

        return {
            "type": "FeatureCollection",
            "features": [],
        }

    progress = st.sidebar.progress(
        0,
        text=f"Downloading 0 / {total} parcels...",
    )

    chunks = [
        object_ids[i:i + chunk_size]
        for i in range(
            0,
            total,
            chunk_size,
        )
    ]

    done_ids = 0

    try:

        with ThreadPoolExecutor(
            max_workers=max_workers,
        ) as executor:

            futures = {
                executor.submit(
                    _fetch_chunk,
                    chunk,
                ): len(chunk)
                for chunk in chunks
            }

            for future in as_completed(futures):

                chunk_features = future.result()

                features.extend(
                    chunk_features
                )

                done_ids += futures[future]

                progress.progress(
                    min(
                        done_ids / total,
                        1.0,
                    ),
                    text=(
                        f"Downloading "
                        f"{done_ids} / {total} parcels..."
                    ),
                )

    finally:

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
# PARCEL WIDTH
# =========================================================

def _shoelace_area(pts):

    s = 0.0
    n = len(pts)

    for i in range(n):

        x1, y1 = pts[i]

        x2, y2 = pts[
            (i + 1) % n
        ]

        s += (
            x1 * y2
            - x2 * y1
        )

    return abs(s) / 2.0


def _convex_hull(points):

    pts = sorted(set(points))

    if len(pts) <= 2:
        return pts

    def cross(o, a, b):

        return (
            (a[0] - o[0])
            * (b[1] - o[1])
            - (a[1] - o[1])
            * (b[0] - o[0])
        )

    lower = []

    for p in pts:

        while (
            len(lower) >= 2
            and cross(
                lower[-2],
                lower[-1],
                p,
            ) <= 0
        ):
            lower.pop()

        lower.append(p)

    upper = []

    for p in reversed(pts):

        while (
            len(upper) >= 2
            and cross(
                upper[-2],
                upper[-1],
                p,
            ) <= 0
        ):
            upper.pop()

        upper.append(p)

    return (
        lower[:-1]
        + upper[:-1]
    )


def _min_width(points):

    hull = _convex_hull(points)

    n = len(hull)

    if n < 3:
        return None

    min_w = float("inf")

    for i in range(n):

        ax, ay = hull[i]

        bx, by = hull[
            (i + 1) % n
        ]

        ex = bx - ax
        ey = by - ay

        elen = math.hypot(
            ex,
            ey,
        )

        if elen == 0:
            continue

        max_d = 0.0

        for px, py in hull:

            d = abs(
                ex * (py - ay)
                - ey * (px - ax)
            ) / elen

            if d > max_d:
                max_d = d

        if max_d < min_w:
            min_w = max_d

    if min_w == float("inf"):
        return None

    return min_w


def _geom_min_width(geom):

    if not geom:
        return None

    geometry_type = geom.get("type")

    if geometry_type == "Polygon":

        polys = [
            geom["coordinates"]
        ]

    elif geometry_type == "MultiPolygon":

        polys = geom["coordinates"]

    else:

        return None

    if (
        not polys
        or not polys[0]
        or not polys[0][0]
    ):
        return None

    lon0 = polys[0][0][0][0]
    lat0 = polys[0][0][0][1]

    k = math.cos(
        math.radians(lat0)
    )

    def proj(pt):

        return (
            (pt[0] - lon0)
            * 111320.0
            * k,
            (pt[1] - lat0)
            * 110540.0,
        )

    best_ring = None
    best_area = -1.0

    for rings in polys:

        outer = [
            proj(p)
            for p in rings[0]
        ]

        area = _shoelace_area(
            outer
        )

        if area > best_area:

            best_area = area
            best_ring = outer

    if not best_ring:
        return None

    return _min_width(
        best_ring
    )


_PERIM_FIELDS = [
    "SHAPE.LEN",
    "SHAPE_LEN",
    "SHAPE.STLength()",
    "Shape_Length",
    "SHAPE_Length",
    "PERIMETRU",
    "Perimeter",
]

_AREA_FIELDS = [
    "SHAPE.AREA",
    "SHAPE_AREA",
    "SHAPE.STArea()",
    "Shape_Area",
    "SHAPE_Area",
    "SUPRAFATA",
    "Suprafata",
    "ARIE",
]


def _first_positive(props, names):

    for name in names:

        if name not in props:
            continue

        try:

            value = float(
                props[name]
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        if value > 0:
            return value

    return None


def parcel_dimensions(feature):

    props = (
        feature.get(
            "properties",
            {},
        )
        or {}
    )

    perimeter = _first_positive(
        props,
        _PERIM_FIELDS,
    )

    area = _first_positive(
        props,
        _AREA_FIELDS,
    )

    if perimeter and area:

        dimension_sum = (
            perimeter / 2.0
        )

        discriminant = (
            dimension_sum * dimension_sum
            - 4.0 * area
        )

        if discriminant >= 0:

            root = math.sqrt(
                discriminant
            )

            a = (
                dimension_sum + root
            ) / 2.0

            b = (
                dimension_sum - root
            ) / 2.0

            return (
                min(a, b),
                max(a, b),
                "rect",
            )

    width = _geom_min_width(
        feature.get("geometry")
    )

    return (
        width,
        None,
        (
            "geom"
            if width is not None
            else None
        ),
    )


def geojson_to_kml(
    fc,
    highlight_groups,
    min_width_m=30.0,
):

    highlighted_folders = defaultdict(
        lambda: defaultdict(list)
    )

    uat_folders = defaultdict(list)

    cf_to_groups = defaultdict(set)

    for group_name, cfs in highlight_groups.items():

        for cf in cfs:

            cf_to_groups[
                str(cf).strip()
            ].add(group_name)

    for feature in fc["features"]:

        props = feature.get(
            "properties",
            {},
        )

        cf = str(
            props.get(
                "NR_CARTE_FUNCIARA",
                "",
            )
        ).strip()

        uat = (
            props.get(
                "UAT",
                "NO_UAT",
            )
            or "NO_UAT"
        )

        groups_for_cf = (
            cf_to_groups.get(cf)
        )

        if groups_for_cf:

            for group_name in groups_for_cf:

                highlighted_folders[
                    group_name
                ][uat].append(feature)

        else:

            uat_folders[
                uat
            ].append(feature)

    def make_placemark(
        feature,
        style_id,
        width_m=None,
        length_m=None,
        source=None,
    ):

        props = feature.get(
            "properties",
            {},
        )

        geom = feature.get(
            "geometry"
        )

        cf = props.get(
            "NR_CARTE_FUNCIARA",
            "",
        )

        identifier = props.get(
            "IDENTIFIER",
            "",
        )

        if cf:

            name = f"CF {cf}"

        else:

            name = (
                f"Parcel {identifier}"
            )

        nonconform = (
            width_m is not None
            and width_m < min_width_m
        )

        if nonconform:

            name = (
                f"⚠ NECONFORM "
                f"({width_m:.1f} m) - "
                f"{name}"
            )

        desc = ""

        if width_m is not None:

            if source == "rect":

                src_label = (
                    "from perimeter & area"
                )

            else:

                src_label = (
                    "from geometry (irregular)"
                )

            status = (
                "NONCONFORM"
                if nonconform
                else "OK"
            )

            desc += (
                f"<b>Width</b>: "
                f"{width_m:.1f} m "
                f"({status}, "
                f"threshold "
                f"{min_width_m:.0f} m)"
                f"<br/>"
            )

            if length_m is not None:

                desc += (
                    f"<b>Length</b>: "
                    f"{length_m:.1f} m"
                    f"<br/>"
                )

            desc += (
                f"<i>Dimensions "
                f"{src_label}</i>"
                f"<br/><br/>"
            )

        for key, value in props.items():

            desc += (
                f"<b>"
                f"{html.escape(str(key))}"
                f"</b>: "
                f"{html.escape(str(value))}"
                f"<br/>"
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

    for (
        group_name,
        uat_map,
    ) in sorted(
        highlighted_folders.items()
    ):

        uat_subfolders = []

        for (
            uat,
            features,
        ) in sorted(
            uat_map.items()
        ):

            placemarks = []

            for feature in features:

                (
                    width_m,
                    length_m,
                    source,
                ) = parcel_dimensions(
                    feature
                )

                if (
                    width_m is not None
                    and width_m < min_width_m
                ):

                    nonconform_count += 1
                    style_id = (
                        "redNonConformStyle"
                    )

                else:

                    style_id = (
                        "yellowMarkedStyle"
                    )

                placemarks.append(
                    make_placemark(
                        feature,
                        style_id,
                        width_m,
                        length_m,
                        source,
                    )
                )

            uat_subfolders.append(
                f"""
        <Folder>

          <name>
            {html.escape(str(uat))}
          </name>

          {''.join(placemarks)}

        </Folder>
        """
            )

        folder_kml.append(
            f"""
        <Folder>

          <name>
            {html.escape(str(group_name))}
          </name>

          {''.join(uat_subfolders)}

        </Folder>
        """
        )

    for (
        uat,
        features,
    ) in sorted(
        uat_folders.items()
    ):

        placemarks = [
            make_placemark(
                feature,
                "blueParcelStyle",
            )
            for feature in features
        ]

        folder_kml.append(
            f"""
        <Folder>

          <name>
            {html.escape(str(uat))}
          </name>

          {''.join(placemarks)}

        </Folder>
        """
        )

    kml_doc = f"""<?xml version="1.0" encoding="UTF-8"?>

<kml xmlns="http://www.opengis.net/kml/2.2">

<Document>

  <name>
    ANCPI parcel export
  </name>

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

    return (
        kml_doc,
        nonconform_count,
    )


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
        placeholder="Victoria, Braila",
    )

    if st.button(
        "Go to location",
        use_container_width=True,
    ):

        if not q.strip():

            st.warning(
                "Introduceți o localitate."
            )

        else:

            center, error = search_location(
                q
            )

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
        placeholder=(
            "Example:\n"
            "CU 65: 70155\n"
            "Environmental study: "
            "70425, 70426"
        ),
        height=220,
    )

    highlight_groups = (
        parse_highlight_groups(
            marked_cf_text
        )
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
        help=(
            "Highlighted parcels narrower than this width "
            "are flagged red in the KML."
        ),
    )

    st.divider()

    export_clicked = st.button(
        "Export KML from polygon",
        use_container_width=True,
    )

    st.divider()

    show_uat = st.checkbox(
        "Show UAT limits (red)",
        value=st.session_state.show_uat,
        help=(
            "Draws UAT administrative boundaries in red. "
            "Set the map view first, then load the limits."
        ),
    )

    st.session_state.show_uat = show_uat

    if st.button(
        "Load UAT limits for current view",
        use_container_width=True,
    ):

        bounds = (
            st.session_state.last_bounds
        )

        if not bounds:

            st.warning(
                "Move the map once, then click again."
            )

        elif _bounds_span(bounds) > 1.0:

            st.warning(
                "Zoom in first — the current view is "
                "too large to load UAT limits."
            )

        else:

            with st.spinner(
                "Loading UAT limits..."
            ):

                try:

                    data = fetch_uat_boundaries(
                        bounds
                    )

                except requests.exceptions.Timeout:

                    logger.exception(
                        "Overpass timeout"
                    )

                    data = None

                    st.error(
                        "Serviciul pentru limitele UAT "
                        "răspunde prea greu. Încercați din nou."
                    )

                except requests.exceptions.ConnectionError:

                    logger.exception(
                        "Overpass connection error"
                    )

                    data = None

                    st.error(
                        "Nu s-a putut realiza conexiunea pentru "
                        "încărcarea limitelor UAT."
                    )

                except requests.exceptions.RequestException:

                    logger.exception(
                        "Overpass request error"
                    )

                    data = None

                    st.error(
                        "Nu au putut fi încărcate limitele UAT."
                    )

                except Exception:

                    logger.exception(
                        "Unexpected UAT error"
                    )

                    data = None

                    st.error(
                        "A apărut o eroare la încărcarea "
                        "limitelor UAT."
                    )

            if data is not None:

                st.session_state.uat_geojson = data
                st.session_state.show_uat = True
                show_uat = True

                if st.session_state.last_view_center:

                    st.session_state.center = (
                        st.session_state.last_view_center
                    )

                if st.session_state.last_view_zoom:

                    st.session_state.zoom = (
                        st.session_state.last_view_zoom
                    )

                if data.get("features"):

                    n_uat = len({
                        feature[
                            "properties"
                        ].get("name")
                        for feature in data["features"]
                        if feature[
                            "properties"
                        ].get("name")
                    })

                    st.caption(
                        f"{n_uat} UAT boundary/-ies loaded."
                    )

                else:

                    st.info(
                        "No UAT boundaries found "
                        "in this view."
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

folium.TileLayer(
    tiles="OpenStreetMap",
    name="OpenStreetMap",
    overlay=False,
    control=True,
).add_to(m)

folium.TileLayer(
    tiles=(
        "https://server.arcgisonline.com/"
        "ArcGIS/rest/services/"
        "World_Imagery/MapServer/"
        "tile/{z}/{y}/{x}"
    ),
    attr="Esri World Imagery",
    name="Satellite imagery",
    overlay=False,
    control=True,
    max_zoom=22,
).add_to(m)

folium.raster_layers.WmsTileLayer(
    url=WMS_URL,
    layers="4",
    name="ANCPI Parcels",
    fmt="image/png",
    transparent=True,
    overlay=True,
    control=True,
).add_to(m)

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


_feature_count = (
    len(
        st.session_state.geojson[
            "features"
        ]
    )
    if st.session_state.geojson
    else 0
)

if (
    st.session_state.geojson
    and _feature_count <= MAX_RENDER_FEATURES
):

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

if (
    show_uat
    and st.session_state.uat_geojson
    and st.session_state.uat_geojson.get(
        "features"
    )
):

    folium.GeoJson(
        st.session_state.uat_geojson,
        name="UAT limits",
        style_function=lambda _feature: {
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


if map_data:

    if map_data.get("bounds"):

        st.session_state.last_bounds = (
            map_data["bounds"]
        )

    current_center = map_data.get(
        "center"
    )

    if current_center:

        st.session_state.last_view_center = [
            current_center["lat"],
            current_center["lng"],
        ]

    current_zoom = map_data.get(
        "zoom"
    )

    if current_zoom is not None:

        st.session_state.last_view_zoom = (
            current_zoom
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

        geom = drawings[-1].get(
            "geometry"
        )

        if not geom:

            st.sidebar.error(
                "Poligonul desenat nu este valid. "
                "Ștergeți-l și desenați-l din nou."
            )

        else:

            with st.spinner(
                "Downloading ANCPI parcels..."
            ):

                try:

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

                        (
                            kml,
                            nonconform,
                        ) = geojson_to_kml(
                            fc,
                            highlight_groups,
                            min_width_m,
                        )

                        st.session_state.kml = kml

                        st.session_state.nonconform = (
                            nonconform
                        )

                        st.sidebar.success(
                            f"{len(fc.get('features', []))} "
                            f"parcele au fost descărcate."
                        )

                    else:

                        st.session_state.geojson = None
                        st.session_state.kml = None
                        st.session_state.nonconform = 0

                        st.sidebar.warning(
                            "No parcels found "
                            "inside polygon."
                        )

                except requests.exceptions.ConnectTimeout:

                    logger.exception(
                        "ANCPI connection timeout"
                    )

                    st.sidebar.error(
                        "Conexiunea cu serverul ANCPI "
                        "nu a putut fi realizată. "
                        "Vă rugăm să încercați din nou."
                    )

                except requests.exceptions.ReadTimeout:

                    logger.exception(
                        "ANCPI read timeout"
                    )

                    st.sidebar.error(
                        "Serverul ANCPI răspunde prea greu. "
                        "Încercați cu un poligon mai mic sau "
                        "reveniți peste câteva momente."
                    )

                except requests.exceptions.SSLError:

                    logger.exception(
                        "ANCPI SSL error"
                    )

                    st.sidebar.error(
                        "Nu a putut fi verificată conexiunea "
                        "securizată cu serverul ANCPI."
                    )

                except requests.exceptions.ConnectionError:

                    logger.exception(
                        "ANCPI connection error"
                    )

                    st.sidebar.error(
                        "Nu s-a putut realiza conexiunea cu "
                        "serverul ANCPI. Serviciul poate fi "
                        "temporar indisponibil."
                    )

                except requests.exceptions.HTTPError as exc:

                    logger.exception(
                        "ANCPI HTTP error"
                    )

                    status_code = (
                        exc.response.status_code
                        if exc.response is not None
                        else None
                    )

                    if status_code == 400:

                        st.sidebar.error(
                            "Serverul ANCPI a respins solicitarea. "
                            "Încercați cu un poligon mai mic."
                        )

                    elif status_code in (
                        401,
                        403,
                    ):

                        st.sidebar.error(
                            "Serverul ANCPI nu permite momentan "
                            "accesarea serviciului."
                        )

                    elif status_code == 404:

                        st.sidebar.error(
                            "Serviciul ANCPI nu a fost găsit."
                        )

                    elif status_code == 408:

                        st.sidebar.error(
                            "Solicitarea a durat prea mult. "
                            "Încercați cu un poligon mai mic."
                        )

                    elif status_code == 429:

                        st.sidebar.error(
                            "Serverul ANCPI a primit prea multe "
                            "solicitări. Încercați din nou peste "
                            "câteva momente."
                        )

                    elif (
                        status_code is not None
                        and status_code >= 500
                    ):

                        st.sidebar.error(
                            "Serverul ANCPI întâmpină momentan "
                            "probleme. Încercați din nou mai târziu."
                        )

                    else:

                        st.sidebar.error(
                            "Solicitarea către ANCPI nu a putut "
                            "fi procesată."
                        )

                except requests.exceptions.RequestException:

                    logger.exception(
                        "ANCPI request error"
                    )

                    st.sidebar.error(
                        "A apărut o problemă la comunicarea "
                        "cu serverul ANCPI."
                    )

                except json.JSONDecodeError:

                    logger.exception(
                        "ANCPI invalid JSON"
                    )

                    st.sidebar.error(
                        "Serverul ANCPI a returnat un răspuns "
                        "invalid. Încercați din nou mai târziu."
                    )

                except RuntimeError:

                    logger.exception(
                        "ANCPI ArcGIS error"
                    )

                    st.sidebar.error(
                        "Serviciul ANCPI nu a putut procesa "
                        "poligonul. Încercați cu un poligon "
                        "mai mic."
                    )

                except (
                    ValueError,
                    KeyError,
                    TypeError,
                ):

                    logger.exception(
                        "Invalid export data"
                    )

                    st.sidebar.error(
                        "Poligonul sau datele primite nu sunt "
                        "valide. Ștergeți poligonul și "
                        "desenați-l din nou."
                    )

                except Exception:

                    logger.exception(
                        "Unexpected export error"
                    )

                    st.sidebar.error(
                        "A apărut o eroare neașteptată în "
                        "timpul exportului. Vă rugăm să "
                        "încercați din nou."
                    )


# =========================================================
# DOWNLOAD BUTTONS
# =========================================================

if (
    st.session_state.geojson
    and st.session_state.kml
):

    n_features = len(
        st.session_state.geojson[
            "features"
        ]
    )

    if n_features > MAX_RENDER_FEATURES:

        st.sidebar.info(
            f"{n_features} parcels downloaded. "
            f"Too many to draw on the map — "
            f"use the download buttons below."
        )

    if st.session_state.nonconform:

        st.sidebar.error(
            f"⚠ {st.session_state.nonconform} highlighted "
            f"parcel(s) non-conform "
            f"(width < {min_width_m:.0f} m) — "
            f"marked red in the KML."
        )

    st.sidebar.download_button(
        "Download KML",
        data=st.session_state.kml.encode(
            "utf-8"
        ),
        file_name="ancpi_parcels.kml",
        mime=(
            "application/vnd.google-earth."
            "kml+xml"
        ),
        use_container_width=True,
    )

    st.sidebar.download_button(
        "Download GeoJSON",
        data=json.dumps(
            st.session_state.geojson
        ).encode("utf-8"),
        file_name="ancpi_parcels.geojson",
        mime="application/geo+json",
        use_container_width=True,
    )
