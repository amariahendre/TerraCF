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

        geom = drawings[-1].get("geometry")

        if not geom:

            st.sidebar.error(
                "Poligonul desenat nu conține o geometrie validă. "
                "Ștergeți-l și desenați-l din nou."
            )

        else:

            with st.spinner(
                "Downloading ANCPI parcels..."
            ):

                try:

                    # Transformă geometria desenată în formatul
                    # acceptat de serviciul ArcGIS ANCPI.
                    esri_geom = (
                        geojson_polygon_to_esri_geometry(
                            geom
                        )
                    )

                    # Prima solicitare către ANCPI:
                    # obține ID-urile parcelelor din poligon.
                    object_ids = get_object_ids(
                        esri_geom
                    )

                    st.sidebar.info(
                        f"Found "
                        f"{len(object_ids)} parcels "
                        f"with CF."
                    )

                    if object_ids:

                        # A doua etapă:
                        # descarcă efectiv geometria și atributele.
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

                        st.sidebar.success(
                            f"{len(fc.get('features', []))} "
                            f"parcele au fost descărcate cu succes."
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
                        "Conexiunea cu serverul ANCPI nu a putut "
                        "fi realizată în timpul disponibil. "
                        "Vă rugăm să încercați din nou."
                    )

                except requests.exceptions.ReadTimeout:

                    logger.exception(
                        "ANCPI response timeout"
                    )

                    st.sidebar.error(
                        "Serverul ANCPI răspunde prea greu. "
                        "Încercați să desenați un poligon mai mic "
                        "sau reveniți peste câteva momente."
                    )

                except requests.exceptions.Timeout:

                    logger.exception(
                        "ANCPI request timeout"
                    )

                    st.sidebar.error(
                        "Solicitarea către serverul ANCPI a expirat. "
                        "Vă rugăm să încercați din nou."
                    )

                except requests.exceptions.SSLError:

                    logger.exception(
                        "ANCPI SSL certificate error"
                    )

                    st.sidebar.error(
                        "Nu a putut fi verificată conexiunea securizată "
                        "cu serverul ANCPI. Serviciul poate avea temporar "
                        "o problemă de certificat."
                    )

                except requests.exceptions.ConnectionError:

                    logger.exception(
                        "Could not connect to ANCPI"
                    )

                    st.sidebar.error(
                        "Nu s-a putut realiza conexiunea cu serverul "
                        "ANCPI. Serviciul poate fi temporar indisponibil. "
                        "Vă rugăm să încercați din nou mai târziu."
                    )

                except requests.exceptions.HTTPError as exc:

                    logger.exception(
                        "ANCPI returned an HTTP error"
                    )

                    status_code = (
                        exc.response.status_code
                        if exc.response is not None
                        else None
                    )

                    if status_code == 400:

                        st.sidebar.error(
                            "Serverul ANCPI a respins solicitarea. "
                            "Încercați să ștergeți poligonul și să "
                            "desenați unul nou, mai mic."
                        )

                    elif status_code in (401, 403):

                        st.sidebar.error(
                            "Serverul ANCPI nu permite momentan "
                            "accesarea acestui serviciu."
                        )

                    elif status_code == 404:

                        st.sidebar.error(
                            "Serviciul ANCPI solicitat nu a fost găsit. "
                            "Adresa serviciului poate fi temporar "
                            "indisponibilă sau modificată."
                        )

                    elif status_code == 408:

                        st.sidebar.error(
                            "Serverul ANCPI a închis solicitarea deoarece "
                            "procesarea a durat prea mult. Încercați cu un "
                            "poligon mai mic."
                        )

                    elif status_code == 429:

                        st.sidebar.error(
                            "Serverul ANCPI a primit prea multe solicitări. "
                            "Vă rugăm să încercați din nou peste câteva "
                            "momente."
                        )

                    elif status_code is not None and status_code >= 500:

                        st.sidebar.error(
                            "Serverul ANCPI întâmpină momentan probleme. "
                            "Vă rugăm să încercați din nou mai târziu."
                        )

                    else:

                        st.sidebar.error(
                            "Solicitarea către serverul ANCPI nu a putut "
                            "fi procesată."
                        )

                except requests.exceptions.RequestException:

                    logger.exception(
                        "Unexpected ANCPI request error"
                    )

                    st.sidebar.error(
                        "A apărut o problemă la comunicarea cu serverul "
                        "ANCPI. Vă rugăm să încercați din nou."
                    )

                except json.JSONDecodeError:

                    logger.exception(
                        "ANCPI returned invalid JSON"
                    )

                    st.sidebar.error(
                        "Serverul ANCPI a returnat un răspuns invalid. "
                        "Vă rugăm să încercați din nou mai târziu."
                    )

                except ValueError as exc:

                    logger.exception(
                        "Invalid polygon or ANCPI data: %s",
                        exc,
                    )

                    st.sidebar.error(
                        "Poligonul desenat sau datele primite nu sunt "
                        "valide. Ștergeți poligonul, desenați-l din nou "
                        "și repetați exportul."
                    )

                except KeyError:

                    logger.exception(
                        "Missing field in ANCPI response"
                    )

                    st.sidebar.error(
                        "Răspunsul primit de la ANCPI este incomplet. "
                        "Vă rugăm să încercați din nou mai târziu."
                    )

                except RuntimeError:

                    logger.exception(
                        "ANCPI service returned an ArcGIS error"
                    )

                    st.sidebar.error(
                        "Serviciul ANCPI a returnat o eroare la procesarea "
                        "poligonului. Încercați cu un poligon mai mic sau "
                        "reveniți mai târziu."
                    )

                except Exception:

                    logger.exception(
                        "Unexpected error while exporting ANCPI parcels"
                    )

                    st.sidebar.error(
                        "A apărut o eroare neașteptată în timpul "
                        "exportului. Vă rugăm să încercați din nou."
                    )
