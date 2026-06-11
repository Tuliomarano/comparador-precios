"""
data.py — Carga y acceso a datos de productos y proveedores.

Lee los archivos Excel de la carpeta raíz del proyecto y expone:
- DataFrame de productos con costos, precios y marca
- Lookup de nombre de proveedor por código
- Funciones de autocomplete para la UI
"""

import os
import re
import glob
import pandas as pd
from functools import lru_cache
from datetime import datetime

# ── Rutas ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # carpeta raíz


def _find_latest_file(pattern: str) -> str | None:
    """Devuelve el archivo más reciente que coincida con el patrón glob."""
    matches = glob.glob(os.path.join(BASE_DIR, pattern))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


# ── Carga de proveedores ────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_proveedores() -> dict[int, str]:
    """
    Carga Master Prov y devuelve un dict {codigo_int: nombre_str}.
    Si no encuentra el archivo, devuelve dict vacío.
    """
    path = _find_latest_file("Master Prov*.XLS*") or _find_latest_file("Master Prov*.xls*")
    if not path:
        return {}
    df = pd.read_excel(path, usecols=["codigo", "nombre"], dtype={"codigo": "Int64"})
    df = df.dropna(subset=["codigo", "nombre"])
    return dict(zip(df["codigo"].astype(int), df["nombre"].str.strip()))


def get_nombre_proveedor(codigo: int | None) -> str:
    """Devuelve el nombre del proveedor dado su código, o cadena vacía."""
    if codigo is None:
        return ""
    return load_proveedores().get(int(codigo), "")


# ── Carga de productos ──────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_productos() -> tuple[pd.DataFrame, datetime]:
    """
    Carga el archivo de Costos + Precios más reciente.
    Devuelve (DataFrame, fecha_archivo).

    Columnas del DataFrame resultante:
        cod_proveedor   — código del SKU en el sistema del proveedor
        cod_interno     — código interno de la empresa
        detalle         — descripción del producto
        costo           — costo de formación de listas de venta
        precio_neto     — precio neto de venta
        marca           — marca del producto
        cod_empresa_prov — código del proveedor en la base de datos
        nombre_proveedor — nombre resuelto del proveedor
    """
    path = _find_latest_file("Costos + Precios*.xls*") or _find_latest_file("Costos*.xls*")
    if not path:
        raise FileNotFoundError(
            "No se encontró el archivo de costos. "
            "Colocá un archivo 'Costos + Precios …xlsx' en la carpeta raíz del proyecto."
        )

    fecha_archivo = datetime.fromtimestamp(os.path.getmtime(path))

    # La fila 1 (índice 0) es el header real, la fila 2 tiene el sub-header "PRECIO NETO"
    df = pd.read_excel(path, header=0, skiprows=[1])

    # Renombrar columnas al esquema interno
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

    df = df.rename(columns=col_map)

    # Quedarse solo con las columnas que existen
    expected = ["cod_proveedor", "cod_interno", "detalle", "costo", "precio_neto",
                "marca", "cod_empresa_prov"]
    df = df[[c for c in expected if c in df.columns]].copy()

    # Limpiar filas sin detalle
    df = df.dropna(subset=["detalle"])
    df = df[df["detalle"].astype(str).str.strip() != ""]

    # Tipos
    for num_col in ["cod_proveedor", "cod_interno", "costo", "precio_neto", "cod_empresa_prov"]:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce")

    # Resolver nombre del proveedor
    if "cod_empresa_prov" in df.columns:
        prov_map = load_proveedores()
        df["nombre_proveedor"] = df["cod_empresa_prov"].apply(
            lambda x: prov_map.get(int(x), "") if pd.notna(x) else ""
        )
    else:
        df["nombre_proveedor"] = ""

    # Detalle en mayúsculas para búsqueda
    df["detalle_upper"] = df["detalle"].str.upper().str.strip()

    df = df.reset_index(drop=True)
    return df, fecha_archivo


# ── Autocomplete ────────────────────────────────────────────────────────────────
def buscar_productos(query: str, max_resultados: int = 10) -> pd.DataFrame:
    """
    Busca productos por código interno, código de proveedor o detalle (parcial).
    Devuelve un DataFrame con los mejores resultados.
    """
    df, _ = load_productos()
    q = query.strip()
    if not q:
        return df.head(0)

    # Intento 1: código exacto (numérico)
    if q.isdigit():
        cod = int(q)
        result = df[
            (df.get("cod_interno", pd.Series(dtype=float)) == cod) |
            (df.get("cod_proveedor", pd.Series(dtype=float)) == cod)
        ]
        if not result.empty:
            return result.head(max_resultados)

    # Intento 2: búsqueda textual en detalle
    q_upper = q.upper()
    mask = df["detalle_upper"].str.contains(re.escape(q_upper), na=False)
    result = df[mask]

    # Si también hay coincidencia con marca, priorizarla
    if "marca" in df.columns:
        mask_marca = df["marca"].str.upper().str.contains(re.escape(q_upper), na=False)
        result = pd.concat([df[mask_marca & mask], result]).drop_duplicates()

    return result.head(max_resultados)


def get_producto_por_codigo(cod_interno: int) -> pd.Series | None:
    """Devuelve la fila completa de un producto dado su código interno."""
    df, _ = load_productos()
    rows = df[df["cod_interno"] == cod_interno]
    return rows.iloc[0] if not rows.empty else None
