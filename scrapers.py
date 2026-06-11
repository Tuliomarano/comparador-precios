"""
scrapers.py — Búsqueda de precios en Rex, Sagitario y MercadoLibre.

Usa requests + APIs JSON (sin Playwright/browser) para mayor compatibilidad
con entornos cloud como Streamlit Cloud.

Lógica de cascada por sitio:
    1. Detalle + Marca
    2. Solo Detalle
    3. Código de proveedor numérico

Cada scraper devuelve:
    {
        "precio":  float | None,
        "url":     str | None,
        "nombre":  str | None,
        "intento": int,
        "error":   str | None,
    }
"""

import re
import json
import logging
import concurrent.futures
import urllib.parse
import urllib.request

import requests

logger = logging.getLogger(__name__)

ML_SITE_ID = "MLA"
ML_API     = "https://api.mercadolibre.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
    "Accept": "application/json, text/html, */*",
}


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _clean_price(value) -> float | None:
    """Convierte string o número a float de precio."""
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d,.]", "", str(value))
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[str]:
    queries, seen = [], set()
    detalle = (detalle or "").strip()
    marca   = (marca   or "").strip()
    cod_str = str(int(float(cod_proveedor))) if cod_proveedor and str(cod_proveedor) not in ("nan", "None", "") else None

    for q in [f"{detalle} {marca}" if marca else None, detalle or None, cod_str]:
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


# ── Rex (Vtex API) ──────────────────────────────────────────────────────────────
def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Usa la API de búsqueda de Vtex que Rex expone en:
    /api/catalog_system/pub/products/search?ft=QUERY
    Devuelve JSON con productos y precios — sin necesidad de browser.
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, query in enumerate(queries, start=1):
        try:
            api_url = (
                f"https://www.rex.com.ar/api/catalog_system/pub/products/search"
                f"?ft={urllib.parse.quote(query)}&_from=0&_to=4"
            )
            resp = session.get(api_url, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                continue

            producto = data[0]
            nombre   = producto.get("productName")
            link     = f"https://www.rex.com.ar/{producto.get('linkText', '')}/p"

            # Precio: buscar en items → sellers → commertialOffer
            precio = None
            for item in producto.get("items", []):
                for seller in item.get("sellers", []):
                    oferta = seller.get("commertialOffer", {})
                    p = oferta.get("Price") or oferta.get("ListPrice")
                    if p and float(p) > 0:
                        precio = float(p)
                        break
                if precio:
                    break

            if precio:
                result.update(precio=precio, url=link, nombre=nombre, intento=i)
                break

        except Exception as exc:
            logger.warning("Rex error intento %d: %s", i, exc)

    if result["precio"] is None:
        result["error"] = f"Sin resultados en Rex tras {len(queries)} intentos"
    return result


# ── Sagitario (WooCommerce search) ──────────────────────────────────────────────
def scrape_sagitario(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    WooCommerce expone datos estructurados (JSON-LD / application/ld+json) en cada página
    de resultado. Hacemos GET al search y extraemos el precio del primer producto
    via regex sobre el JSON-LD embebido en el HTML.
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    session = requests.Session()
    session.headers.update({**HEADERS, "Accept": "text/html,application/xhtml+xml"})

    for i, query in enumerate(queries, start=1):
        try:
            url  = f"https://www.sagitario.com.ar/?s={urllib.parse.quote(query)}&post_type=product"
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            html = resp.text

            # Extraer JSON-LD del primer producto
            matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
            precio = nombre = link = None
            for m in matches:
                try:
                    obj = json.loads(m)
                    # Puede ser @graph o directo
                    items = obj.get("@graph", [obj]) if isinstance(obj, dict) else obj
                    for item in items:
                        if item.get("@type") in ("Product", "ItemList"):
                            if item.get("@type") == "Product":
                                nombre = item.get("name")
                                link   = item.get("url")
                                offers = item.get("offers", {})
                                if isinstance(offers, list):
                                    offers = offers[0]
                                precio = _clean_price(offers.get("price"))
                                break
                except Exception:
                    continue
                if precio:
                    break

            # Fallback: regex simple sobre el precio en el HTML
            if not precio:
                m = re.search(r'"price"\s*:\s*"?([\d.,]+)"?', html)
                if m:
                    precio = _clean_price(m.group(1))

            if precio:
                result.update(precio=precio, url=link or url, nombre=nombre, intento=i)
                break

        except Exception as exc:
            logger.warning("Sagitario error intento %d: %s", i, exc)

    if result["precio"] is None:
        result["error"] = f"Sin resultados en Sagitario tras {len(queries)} intentos"
    return result


# ── MercadoLibre (API oficial) ──────────────────────────────────────────────────
def scrape_ml(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    API pública de MercadoLibre. Promedio ponderado por unidades vendidas.
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, query in enumerate(queries, start=1):
        try:
            api_url = f"{ML_API}/sites/{ML_SITE_ID}/search?q={urllib.parse.quote(query)}&limit=10"
            resp    = session.get(api_url, timeout=12)
            resp.raise_for_status()
            data  = resp.json()
            items = data.get("results", [])

            if not items:
                continue

            vendidos = [x for x in items if x.get("sold_quantity", 0) > 0]
            pool     = vendidos if vendidos else items[:5]

            total_peso   = sum(x.get("sold_quantity", 1) for x in pool)
            total_precio = sum(x["price"] * x.get("sold_quantity", 1) for x in pool)
            precio_pond  = round(total_precio / total_peso, 2)

            result.update(
                precio  = precio_pond,
                url     = pool[0].get("permalink"),
                nombre  = pool[0].get("title"),
                intento = i,
            )
            break

        except Exception as exc:
            logger.warning("ML error intento %d: %s", i, exc)

    if result["precio"] is None:
        result["error"] = f"Sin resultados en ML tras {len(queries)} intentos"
    return result


# ── Función principal ────────────────────────────────────────────────────────────
def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """Ejecuta los 3 scrapers en paralelo."""
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers = {"rex": scrape_rex, "sagitario": scrape_sagitario, "ml": scrape_ml}
    resultados = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {name: ex.submit(fn, *args) for name, fn in scrapers.items()}
        for name, fut in futures.items():
            try:
                resultados[name] = fut.result(timeout=30)
            except Exception as exc:
                resultados[name] = {
                    "precio": None, "url": None, "nombre": None,
                    "intento": None, "error": str(exc),
                }
    return resultados
