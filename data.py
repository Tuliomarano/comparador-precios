"""
data.py — Carga y acceso a datos de productos y proveedores.

Acepta archivos subidos desde la UI (BytesIO) o desde disco (path).
Expone funciones de autocomplete para la UI.
"""

import os
import re
import glob
import pandas as pd
from datetime import datetime
from io import BytesIO

# ── Helpers internos ────────────────────────────────────────────────────────────
def _normalizar_columnas(df: pd.DataFrame) -> pd.DataFrame:
    """Renombra columnas al esquema interno independientemente del nombre original."""
    col_map = {}
    for col in df.columns:
        c = str(col).upper().strip()
        if "CODIGO" in c and "PROVEEDOR" in c and "INTERNO" not in c:
            col_map[col] = "cod_proveedor"
        elif "CODIGO" in c and "INTERNO" in c:
            col_map[col] = "cod_interno"
        elif "DETALLE" in c:
            col_map[col] = "detalle"
        elif "COSTO" in c or "FORMACION" in c:
            col_map[col] = "costo"
        elif "PRECIO" in c or "NETO" in c:
            col_map[col] = "precio_neto"
        elif "MARCA" in c:
            col_map[col] = "marca"
        elif "PROVEEDOR" in c:
            col_map[col] = "cod_empresa_prov"
    return df.rename(columns=col_map)


def _limpiar_productos(df: pd.DataFrame, prov_map: dict) -> pd.DataFrame:
    """Limpia y enriquece el DataFrame de productos."""
    expected = ["cod_proveedor", "cod_interno", "detalle", "costo",
                "precio_neto", "marca", "cod_empresa_prov"]
    df = df[[c for c in expected if c in df.columns]].copy()
    df = df.dropna(subset=["detalle"])
    df = df[df["detalle"].astype(str).str.strip() != ""]

    for num_col in ["cod_proveedor", "cod_interno", "costo", "precio_neto", "cod_empresa_prov"]:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce")

    if "cod_empresa_prov" in df.columns and prov_map:
        df["nombre_proveedor"] = df["cod_empresa_prov"].apply(
            lambda x: prov_map.get(int(x), "") if pd.notna(x) else ""
        )
    else:
        df["nombre_proveedor"] = ""

    df["detalle_upper"] = df["detalle"].str.upper().str.strip()
    return df.reset_index(drop=True)


# ── Carga desde archivos subidos (Streamlit uploader) ──────────────────────────
def cargar_desde_uploads(costos_file, prov_file=None) -> tuple[pd.DataFrame, datetime, dict]:
    """
    Carga datos desde objetos de archivo subidos (st.file_uploader).

    Args:
        costos_file : archivo de Costos + Precios (BytesIO / UploadedFile)
        prov_file   : archivo Master Prov (opcional)

    Returns:
        (df_productos, fecha_archivo, prov_map)
    """
    # Fecha: del nombre del archivo si tiene formato DD-MM-YYYY, sino hoy
    fecha_archivo = datetime.today()
    nombre = getattr(costos_file, "name", "")
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", nombre)
    if m:
        try:
            fecha_archivo = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    # Proveedores
    prov_map = {}
    if prov_file is not None:
        df_prov = pd.read_excel(prov_file, usecols=["codigo", "nombre"])
        df_prov = df_prov.dropna(subset=["codigo", "nombre"])
        prov_map = dict(zip(df_prov["codigo"].astype(int), df_prov["nombre"].str.strip()))

    # Productos (fila 2 es sub-header "PRECIO NETO", la saltamos)
    df = pd.read_excel(costos_file, header=0, skiprows=[1])
    df = _normalizar_columnas(df)
    df = _limpiar_productos(df, prov_map)

    return df, fecha_archivo, prov_map


# ── Carga desde disco (uso local / desarrollo) ──────────────────────────────────
def _find_latest_file(pattern: str, base: str) -> str | None:
    matches = glob.glob(os.path.join(base, pattern))
    return max(matches, key=os.path.getmtime) if matches else None


def cargar_desde_disco() -> tuple[pd.DataFrame, datetime, dict]:
    """
    Intenta cargar los archivos Excel desde la carpeta raíz del proyecto.
    Útil para desarrollo local. Lanza FileNotFoundError si no los encuentra.
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    prov_path = _find_latest_file("Master Prov*.XLS*", base) or \
                _find_latest_file("Master Prov*.xls*", base)
    prov_map = {}
    if prov_path:
        df_prov = pd.read_excel(prov_path, usecols=["codigo", "nombre"])
        df_prov = df_prov.dropna(subset=["codigo", "nombre"])
        prov_map = dict(zip(df_prov["codigo"].astype(int), df_prov["nombre"].str.strip()))

    costos_path = _find_latest_file("Costos + Precios*.xls*", base) or \
                  _find_latest_file("Costos*.xls*", base)
    if not costos_path:
        raise FileNotFoundError("No se encontró el archivo de costos en disco.")

    fecha_archivo = datetime.fromtimestamp(os.path.getmtime(costos_path))
    df = pd.read_excel(costos_path, header=0, skiprows=[1])
    df = _normalizar_columnas(df)
    df = _limpiar_productos(df, prov_map)

    return df, fecha_archivo, prov_map


# ── Autocomplete ────────────────────────────────────────────────────────────────
def buscar_productos(query: str, df: pd.DataFrame, max_resultados: int = 10) -> pd.DataFrame:
    """
    Busca en el DataFrame por código interno, código de proveedor o detalle parcial.
    """
    q = query.strip()
    if not q:
        return df.head(0)

    if q.isdigit():
        cod = int(q)
        result = df[
            (df.get("cod_interno", pd.Series(dtype=float)) == cod) |
            (df.get("cod_proveedor", pd.Series(dtype=float)) == cod)
        ]
        if not result.empty:
            return result.head(max_resultados)

    q_upper = q.upper()
    mask = df["detalle_upper"].str.contains(re.escape(q_upper), na=False)
    result = df[mask]

    if "marca" in df.columns:
        mask_marca = df["marca"].str.upper().str.contains(re.escape(q_upper), na=False)
        result = pd.concat([df[mask_marca & ~mask], result]).drop_duplicates()

    return result.head(max_resultados)


def get_producto_por_codigo(cod_interno: int, df: pd.DataFrame) -> pd.Series | None:
    """Devuelve la fila completa de un producto dado su código interno."""
    rows = df[df["cod_interno"] == cod_interno]
    return rows.iloc[0] if not rows.empty else None
