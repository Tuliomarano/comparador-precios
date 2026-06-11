"""
app.py — UI principal del Comparador de Precios.

Ejecutar localmente:
    streamlit run app.py
"""

import io
import streamlit as st
import pandas as pd
from datetime import datetime

from data import cargar_desde_uploads, cargar_desde_disco, buscar_productos, get_producto_por_codigo
from scrapers import buscar_precios

# ── Configuración de página ─────────────────────────────────────────────────────
st.set_page_config(
    page_title="Comparador de Precios",
    page_icon="🔍",
    layout="wide",
)

# ── Sidebar: carga de archivos ──────────────────────────────────────────────────
with st.sidebar:
    st.header("📂 Archivos de datos")

    costos_file = st.file_uploader(
        "Costos + Precios (.xlsx)",
        type=["xlsx", "xls"],
        help="Archivo con código, detalle, costo, precio neto y marca.",
    )
    prov_file = st.file_uploader(
        "Master Proveedores (.xlsx)  *(opcional)*",
        type=["xlsx", "xls"],
        help="Tabla con código y nombre de proveedor. Si no la subís, el nombre de proveedor no se mostrará.",
    )

    if costos_file:
        st.success("✅ Archivo de costos cargado")
    if prov_file:
        st.success("✅ Master de proveedores cargado")

    st.divider()
    st.caption("Los archivos se procesan en memoria y no se almacenan en ningún servidor.")

    # ── Sección futura: reporte por mail ────────────────────────────────────────
    st.header("📧 Reporte por mail")
    st.info(
        "**Próximamente:** elegí hasta 5 productos y recibís un mail automático "
        "con los precios scrapeados y la fecha del archivo de costos utilizado.",
        icon="🔜",
    )

# ── Carga de datos ──────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Procesando archivo…")
def _procesar_uploads(costos_bytes: bytes, costos_nombre: str,
                      prov_bytes: bytes | None, prov_nombre: str | None):
    costos_io = io.BytesIO(costos_bytes)
    costos_io.name = costos_nombre
    prov_io = io.BytesIO(prov_bytes) if prov_bytes else None
    return cargar_desde_uploads(costos_io, prov_io)


# Intentar cargar: primero desde upload, luego desde disco (uso local)
df = None
fecha_costos = None

if costos_file:
    prov_bytes = prov_file.read() if prov_file else None
    prov_nombre = prov_file.name if prov_file else None
    costos_bytes = costos_file.read()
    try:
        df, fecha_costos, _ = _procesar_uploads(
            costos_bytes, costos_file.name, prov_bytes, prov_nombre
        )
    except Exception as e:
        st.error(f"Error al procesar el archivo: {e}")
        st.stop()
else:
    # Intentar desde disco (desarrollo local)
    try:
        df, fecha_costos, _ = cargar_desde_disco()
    except FileNotFoundError:
        pass

# ── Pantalla de bienvenida si no hay datos ──────────────────────────────────────
if df is None:
    st.title("🔍 Comparador de Precios")
    st.divider()
    st.markdown(
        """
        ### Para comenzar, subí el archivo de costos desde el panel izquierdo.

        **Qué necesitás:**
        - 📄 `Costos + Precios DD-MM-YYYY.xlsx` — lista completa de productos
        - 📄 `Master Prov - DD-MM.XLSX` *(opcional)* — tabla de proveedores para ver el nombre

        Una vez cargado el archivo, podrás buscar cualquier producto y consultar
        los precios actuales en Rex, Sagitario y MercadoLibre.
        """
    )
    st.stop()

# ── Header principal ────────────────────────────────────────────────────────────
st.title("🔍 Comparador de Precios")
st.caption(
    f"📅 Información de costos vigente al **{fecha_costos.strftime('%d/%m/%Y')}**  "
    f"— {len(df):,} productos cargados"
)
st.divider()

# ── Tabs ────────────────────────────────────────────────────────────────────────
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

    producto_seleccionado = None

    if query:
        resultados = buscar_productos(query, df, max_resultados=10)

        if resultados.empty:
            st.warning("No se encontraron productos para esa búsqueda.")
        elif len(resultados) == 1:
            producto_seleccionado = resultados.iloc[0]
        else:
            opciones = {
                f"{row['detalle']}  "
                f"[{int(row['cod_interno']) if pd.notna(row.get('cod_interno')) else '—'}]"
                f"  •  {row.get('marca', '')}": idx
                for idx, row in resultados.iterrows()
            }
            seleccion_label = st.selectbox(
                f"{len(resultados)} resultado(s) — elegí uno:",
                options=list(opciones.keys()),
            )
            producto_seleccionado = resultados.loc[opciones[seleccion_label]]

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

        k1, k2, k3 = st.columns(3)
        costo       = p.get("costo")
        precio_neto = p.get("precio_neto")

        with k1:
            st.metric("Costo formación", f"${costo:,.2f}" if pd.notna(costo) else "—")
        with k2:
            st.metric("Precio neto venta", f"${precio_neto:,.2f}" if pd.notna(precio_neto) else "—")
        with k3:
            if pd.notna(costo) and pd.notna(precio_neto) and precio_neto > 0:
                margen = (precio_neto - costo) / precio_neto * 100
                st.metric("Margen bruto", f"{margen:.1f}%")
            else:
                st.metric("Margen bruto", "—")

        st.divider()

        if st.button("🌐 Buscar precios en Rex / Sagitario / ML", type="primary"):
            detalle          = str(p.get("detalle", ""))
            marca            = str(p.get("marca", ""))
            cod_proveedor    = p.get("cod_proveedor")
            nombre_proveedor = str(p.get("nombre_proveedor", ""))

            with st.spinner("Consultando precios… puede tardar hasta 30 segundos"):
                precios = buscar_precios(detalle, marca, cod_proveedor, nombre_proveedor)

            filas = []
            for sitio, res in precios.items():
                precio_ext = res.get("precio")
                intento    = res.get("intento")
                fallback_label = (
                    "" if intento == 1 else
                    f" (fallback {intento})" if intento else ""
                )
                diff = diff_pct = None
                if precio_ext and pd.notna(precio_neto) and precio_neto:
                    diff     = precio_ext - precio_neto
                    diff_pct = diff / precio_neto * 100

                filas.append({
                    "Sitio":             sitio.capitalize() + fallback_label,
                    "Nombre encontrado": res.get("nombre") or "—",
                    "Precio ($)":        f"${precio_ext:,.2f}" if precio_ext else res.get("error", "Sin resultado"),
                    "vs. Precio neto":   f"{'+' if diff and diff >= 0 else ''}{diff:,.2f}" if diff is not None else "—",
                    "Diferencia %":      f"{'+' if diff_pct and diff_pct >= 0 else ''}{diff_pct:.1f}%" if diff_pct is not None else "—",
                    "URL":               res.get("url") or "—",
                })

            tabla = pd.DataFrame(filas)
            st.dataframe(tabla, use_container_width=True, hide_index=True)

            # ── Exportar a Excel ────────────────────────────────────────────────
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                # Hoja 1: resultados de scraping
                tabla.to_excel(writer, sheet_name="Precios competencia", index=False)
                # Hoja 2: ficha del producto
                ficha = pd.DataFrame([{
                    "Detalle":          p.get("detalle"),
                    "Marca":            p.get("marca"),
                    "Proveedor":        p.get("nombre_proveedor"),
                    "Cód. interno":     int(p["cod_interno"]) if pd.notna(p.get("cod_interno")) else None,
                    "Cód. proveedor":   int(p["cod_proveedor"]) if pd.notna(p.get("cod_proveedor")) else None,
                    "Costo":            costo,
                    "Precio neto":      precio_neto,
                    "Fecha costos":     fecha_costos.strftime("%d/%m/%Y"),
                }])
                ficha.to_excel(writer, sheet_name="Ficha producto", index=False)

            st.download_button(
                "⬇️ Descargar resultado en Excel",
                data=output.getvalue(),
                file_name=f"precios_{detalle[:30].replace(' ', '_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — Búsqueda masiva
# ════════════════════════════════════════════════════════════════════════════════
with tab_masivo:

    st.subheader("Consultar múltiples productos a la vez")
    st.caption("Ingresá una lista de códigos internos (uno por línea).")

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
                p   = get_producto_por_codigo(cod, df)
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
                    "cod_interno":    cod,
                    "detalle":        p.get("detalle"),
                    "marca":          p.get("marca"),
                    "proveedor":      p.get("nombre_proveedor"),
                    "costo":          p.get("costo"),
                    "precio_neto":    p.get("precio_neto"),
                    "rex ($)":        precios["rex"].get("precio"),
                    "sagitario ($)":  precios["sagitario"].get("precio"),
                    "ml ($)":         precios["ml"].get("precio"),
                    "rex fallback":   precios["rex"].get("intento"),
                    "sag fallback":   precios["sagitario"].get("intento"),
                    "ml fallback":    precios["ml"].get("intento"),
                    "fecha costos":   fecha_costos.strftime("%d/%m/%Y"),
                }

            with st.spinner(f"Consultando {len(codigos)} productos…"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                    resultados_masivos = list(ex.map(_buscar_uno, codigos))

            tabla_masiva = pd.DataFrame(resultados_masivos)
            st.dataframe(tabla_masiva, use_container_width=True, hide_index=True)

            # ── Exportar ────────────────────────────────────────────────────────
            output_masivo = io.BytesIO()
            with pd.ExcelWriter(output_masivo, engine="openpyxl") as writer:
                tabla_masiva.to_excel(writer, sheet_name="Comparación masiva", index=False)

            st.download_button(
                "⬇️ Descargar resultado en Excel",
                data=output_masivo.getvalue(),
                file_name=f"comparacion_masiva_{datetime.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_masivo",
            )
