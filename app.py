"""
app.py — UI principal del Comparador de Precios.

Ejecutar localmente:
    streamlit run app.py

Deploy:
    Subir la carpeta comparador_precios/ a GitHub y conectar en share.streamlit.io
"""

import streamlit as st
import pandas as pd
from datetime import datetime

from data import load_productos, buscar_productos, get_producto_por_codigo
from scrapers import buscar_precios

# ── Configuración de página ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="Comparador de Precios",
    page_icon="🔍",
    layout="wide",
)

# ── Carga de datos ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="Cargando catálogo de productos…")
def _get_data():
    return load_productos()

try:
    df, fecha_costos = _get_data()
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

# ── Header ──────────────────────────────────────────────────────────────────────
st.title("🔍 Comparador de Precios")
st.caption(
    f"📅 Información de costos vigente al **{fecha_costos.strftime('%d/%m/%Y')}**  "
    f"— {len(df):,} productos cargados"
)
st.divider()

# ── Tabs principales ────────────────────────────────────────────────────────────
tab_individual, tab_masivo = st.tabs(["Búsqueda individual", "Búsqueda masiva"])


# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — Búsqueda individual
# ════════════════════════════════════════════════════════════════════════════════
with tab_individual:

    st.subheader("Buscar producto")

    col_input, col_info = st.columns([2, 1])

    with col_input:
        query = st.text_input(
            "Código interno, código de proveedor o descripción",
            placeholder="Ej: 310110  /  85031007  /  polacrin mem",
            key="query_individual",
        )

    # Autocomplete / selector
    producto_seleccionado = None

    if query:
        resultados = buscar_productos(query, max_resultados=10)

        if resultados.empty:
            st.warning("No se encontraron productos para esa búsqueda.")
        elif len(resultados) == 1:
            # Match único — carga directo
            producto_seleccionado = resultados.iloc[0]
        else:
            # Dropdown de opciones
            opciones = {
                f"{row['detalle']}  [{int(row['cod_interno']) if pd.notna(row.get('cod_interno')) else '—'}]"
                f"  •  {row.get('marca', '')}": idx
                for idx, row in resultados.iterrows()
            }
            seleccion_label = st.selectbox(
                f"{len(resultados)} resultado(s) — elegí uno:",
                options=list(opciones.keys()),
            )
            producto_seleccionado = resultados.loc[opciones[seleccion_label]]

    # ── Ficha del producto ──────────────────────────────────────────────────────
    if producto_seleccionado is not None:
        p = producto_seleccionado

        with col_info:
            st.markdown("**Producto seleccionado**")
            st.markdown(f"**{p.get('detalle', '—')}**")
            st.caption(
                f"Marca: {p.get('marca', '—')}  |  "
                f"Proveedor: {p.get('nombre_proveedor', '—')}  |  "
                f"Cód. interno: {int(p['cod_interno']) if pd.notna(p.get('cod_interno')) else '—'}  |  "
                f"Cód. proveedor: {int(p['cod_proveedor']) if pd.notna(p.get('cod_proveedor')) else '—'}"
            )

        st.divider()

        # ── KPIs internos ───────────────────────────────────────────────────────
        k1, k2, k3 = st.columns(3)
        costo      = p.get("costo")
        precio_neto = p.get("precio_neto")

        with k1:
            st.metric("Costo formación", f"${costo:,.2f}" if pd.notna(costo) else "—")
        with k2:
            st.metric("Precio neto venta", f"${precio_neto:,.2f}" if pd.notna(precio_neto) else "—")
        with k3:
            if pd.notna(costo) and pd.notna(precio_neto) and costo > 0:
                margen = (precio_neto - costo) / precio_neto * 100
                st.metric("Margen bruto", f"{margen:.1f}%")
            else:
                st.metric("Margen bruto", "—")

        st.divider()

        # ── Botón de búsqueda de precios ─────────────────────────────────────
        if st.button("🌐 Buscar precios en Rex / Sagitario / ML", type="primary"):

            detalle          = str(p.get("detalle", ""))
            marca            = str(p.get("marca", ""))
            cod_proveedor    = p.get("cod_proveedor")
            nombre_proveedor = str(p.get("nombre_proveedor", ""))

            with st.spinner("Consultando precios… puede tardar hasta 30 segundos"):
                precios = buscar_precios(detalle, marca, cod_proveedor, nombre_proveedor)

            # ── Tabla de resultados ─────────────────────────────────────────
            filas = []
            for sitio, res in precios.items():
                precio_ext = res.get("precio")
                intento    = res.get("intento")
                fallback_label = (
                    "" if intento == 1 else
                    f" *(fallback {intento})*" if intento else ""
                )
                diff = None
                diff_pct = None
                if precio_ext and pd.notna(precio_neto) and precio_neto:
                    diff     = precio_ext - precio_neto
                    diff_pct = diff / precio_neto * 100

                filas.append({
                    "Sitio":           sitio.capitalize() + fallback_label,
                    "Nombre encontrado": res.get("nombre") or "—",
                    "Precio ($)":       f"${precio_ext:,.2f}" if precio_ext else res.get("error", "Sin resultado"),
                    "vs. Precio neto":  f"{'+' if diff >= 0 else ''}{diff:,.2f}" if diff is not None else "—",
                    "Diferencia %":     f"{'+' if diff_pct >= 0 else ''}{diff_pct:.1f}%" if diff_pct is not None else "—",
                    "URL":              res.get("url") or "—",
                })

            tabla = pd.DataFrame(filas)
            st.dataframe(tabla, use_container_width=True, hide_index=True)

            # ── Exportar ────────────────────────────────────────────────────
            excel_bytes = tabla.to_excel(index=False, engine="openpyxl")
            st.download_button(
                "⬇️ Exportar a Excel",
                data=excel_bytes,
                file_name=f"precios_{detalle[:30].replace(' ','_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — Búsqueda masiva
# ════════════════════════════════════════════════════════════════════════════════
with tab_masivo:

    st.subheader("Consultar múltiples productos a la vez")
    st.caption(
        "Ingresá una lista de códigos internos (uno por línea) "
        "y la app buscará los precios de todos en paralelo."
    )

    codigos_raw = st.text_area(
        "Códigos internos (uno por línea)",
        placeholder="310110\n12000037\n153016",
        height=150,
    )

    if st.button("🚀 Buscar todos", type="primary", key="btn_masivo"):
        codigos = [c.strip() for c in codigos_raw.splitlines() if c.strip().isdigit()]

        if not codigos:
            st.warning("Ingresá al menos un código interno válido.")
        else:
            import concurrent.futures

            def _buscar_uno(cod_str: str):
                cod = int(cod_str)
                p   = get_producto_por_codigo(cod)
                if p is None:
                    return {"cod_interno": cod, "detalle": "NO ENCONTRADO",
                            "rex": None, "sagitario": None, "ml": None}
                precios = buscar_precios(
                    str(p.get("detalle", "")),
                    str(p.get("marca", "")),
                    p.get("cod_proveedor"),
                    str(p.get("nombre_proveedor", "")),
                )
                return {
                    "cod_interno":  cod,
                    "detalle":      p.get("detalle"),
                    "marca":        p.get("marca"),
                    "costo":        p.get("costo"),
                    "precio_neto":  p.get("precio_neto"),
                    "rex":          precios["rex"].get("precio"),
                    "sagitario":    precios["sagitario"].get("precio"),
                    "ml":           precios["ml"].get("precio"),
                    "rex_intento":  precios["rex"].get("intento"),
                    "sag_intento":  precios["sagitario"].get("intento"),
                    "ml_intento":   precios["ml"].get("intento"),
                }

            with st.spinner(f"Consultando {len(codigos)} productos…"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                    resultados_masivos = list(ex.map(_buscar_uno, codigos))

            tabla_masiva = pd.DataFrame(resultados_masivos)
            st.dataframe(tabla_masiva, use_container_width=True, hide_index=True)

            # Exportar
            excel_masivo = tabla_masiva.to_excel(index=False, engine="openpyxl")
            st.download_button(
                "⬇️ Exportar resultado a Excel",
                data=excel_masivo,
                file_name=f"comparacion_masiva_{datetime.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_masivo",
            )
