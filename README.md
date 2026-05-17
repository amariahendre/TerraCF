ANCPI Parcel Explorer

Interactive ANCPI parcel visualization and export tool with satellite imagery, polygon-based parcel extraction, CF highlighting and grouping, UAT-based organization, and KML / GeoJSON export for cadastral analysis workflows in Romania.

Features

* ANCPI parcel visualization
* Satellite imagery basemap
* Polygon and rectangle selection
* Export parcels to KML
* Export parcels to GeoJSON
* Highlight and group selected CF numbers
* Automatic UAT folder organization
* Interactive map interface
* Search locations in Romania

Technologies

* Streamlit
* Folium
* Streamlit Folium
* ANCPI Geoportal Services
* GeoJSON / KML export

Installation

Clone the repository:

git clone https://github.com/YOUR_USERNAME/ancpi-parcel-explorer.git
cd ancpi-parcel-explorer

Install dependencies:

pip install -r requirements.txt

Run the app:

streamlit run app.py

Requirements

Create a requirements.txt file with:

streamlit
folium
streamlit-folium
requests
geopy

Usage

1. Search a location in Romania
2. Draw a polygon or rectangle on the map
3. Click Export KML from polygon
4. Download:
    * KML
    * GeoJSON

Optional:

* Add highlighted CF groups using:

CU 65: 70155
Environmental study: 70425, 70426

Highlighted parcels are exported into separate KML folders with dedicated styling.

Data Source

Parcel visualization uses public ANCPI Geoportal services.

Author

Created by Ana-Maria Hendre

LinkedIn:
https://ro.linkedin.com/in/amariahendre
