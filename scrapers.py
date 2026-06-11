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
        v = float(value)
        return v if v > 0 else None
    text = re.sub(r"[^\d,.]", "", str(value))
    # Formato argentino: puntos como separador de miles, coma como decimal
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        v = float(text)
        return v if v > 0 else None
    except ValueError:
        return None


def _limpiar_query(q: str) -> str:
    """Limpia el texto para búsqueda: normaliza espacios y quita caracteres raros."""
    q = re.sub(r"[^\w\s]", " ", q)   # quita puntos, guiones, etc.
    q = re.sub(r"\s+", " ", q).strip()
    return q


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[str]:
    queries, seen = [], set()
    detalle = (detalle or "").strip()
    marca   = (marca   or "").strip()
    cod_str = str(int(float(cod_proveedor))) if cod_proveedor and str(cod_proveedor) not in ("nan", "None", "") else None

    candidatos = [
        f"{detalle} {marca}" if marca else None,
        detalle or None,
        cod_str,
    ]
    for q in candidatos:
        if not q:
            continue
        q_clean = _limpiar_query(q)
        if q_clean and q_clean not in seen:
            seen.add(q_clean)
            queries.append(q_clean)
    return queries


# ── Rex ─────────────────────────────────────────────────────────────────────────
def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Intenta 3 endpoints de Rex en orden:
    1. API Vtex catalog (más precisa)
    2. API Vtex intelligent-search (alternativa)
    3. Página de resultados HTML + regex de precio
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, query in enumerate(queries, start=1):
        q_enc = urllib.parse.quote(query)
        precio = nombre = link = None

        # Endpoint A: catalog API clásica de Vtex
        endpoints = [
            f"https://www.rex.com.ar/api/catalog_system/pub/products/search?ft={q_enc}&_from=0&_to=4",
            f"https://www.rex.com.ar/_v/api/intelligent-search/product_search/v3?query={q_enc}&count=5&locale=es-AR",
        ]
        for ep in endpoints:
            try:
                resp = session.get(ep, timeout=15)
                if resp.status_code != 200:
                    continue
                data = resp.json()

                # Formato catalog API: lista de productos
                if isinstance(data, list) and data:
                    prod = data[0]
                    nombre = prod.get("productName")
                    link   = f"https://www.rex.com.ar/{prod.get('linkText','')}/p"
                    for item in prod.get("items", []):
                        for seller in item.get("sellers", []):
                            oferta = seller.get("commertialOffer", {})
                            p = oferta.get("Price") or oferta.get("ListPrice")
                            precio = _clean_price(p)
                            if precio:
                                break
                        if precio:
                            break

                # Formato intelligent-search: {products: [...]}
                elif isinstance(data, dict):
                    prods = data.get("products", data.get("data", {}).get("productSearch", {}).get("products", []))
                    if prods:
                        prod   = prods[0]
                        nombre = prod.get("productName") or prod.get("name")
                        link   = f"https://www.rex.com.ar/{prod.get('linkText','')}/p"
                        for item in prod.get("items", []):
                            for seller in item.get("sellers", []):
                                oferta = seller.get("commertialOffer", {})
                                p = oferta.get("Price") or oferta.get("ListPrice")
                                precio = _clean_price(p)
                                if precio:
                                    break
                            if precio:
                                break

                if precio:
                    break
            except Exception as exc:
                logger.warning("Rex endpoint error: %s", exc)

        # Endpoint B: HTML fallback — buscar precio en JSON embebido en la página
        if not precio:
            try:
                url_html = f"https://www.rex.com.ar/{q_enc}?map=ft"
                resp = session.get(url_html, timeout=15)
                html = resp.text
                # Vtex embebe __STATE__ con precios
                m = re.search(r'"Price"\s*:\s*([\d.]+)', html)
                if m:
                    precio = _clean_price(m.group(1))
                m2 = re.search(r'"productName"\s*:\s*"([^"]+)"', html)
                if m2:
                    nombre = m2.group(1)
                link = url_html
            except Exception as exc:
                logger.warning("Rex HTML fallback error: %s", exc)

        if precio:
            result.update(precio=precio, url=link, nombre=nombre, intento=i)
            break

    if result["precio"] is None:
        result["error"] = f"Sin resultados en Rex tras {len(queries)} intentos"
    return result


# ── Sagitario (WooCommerce) ──────────────────────────────────────────────────────
def scrape_sagitario(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Busca en Sagitario (WooCommerce). Estrategia:
    1. Página de búsqueda → extrae JSON-LD de los productos listados
    2. Si hay resultado, abre la página del producto para confirmar nombre y precio
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    session = requests.Session()
    session.headers.update({**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"})

    for i, query in enumerate(queries, start=1):
        try:
            url_busqueda = f"https://www.sagitario.com.ar/?s={urllib.parse.quote(query)}&post_type=product"
            resp = session.get(url_busqueda, timeout=15)
            resp.raise_for_status()
            html = resp.text

            precio = nombre = link = None

            # Intentar JSON-LD
            ld_blocks = re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.DOTALL | re.IGNORECASE
            )
            for blk in ld_blocks:
                try:
                    obj = json.loads(blk.strip())
                    nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
                    for node in nodes:
                        if node.get("@type") == "Product":
                            nombre = node.get("name")
                            link   = node.get("url")
                            offers = node.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0]
                            precio = _clean_price(offers.get("price"))
                            if precio:
                                break
                    if precio:
                        break
                except Exception:
                    continue

            # Fallback: extraer URLs de productos del HTML y visitar la primera
            if not precio:
                prod_links = re.findall(
                    r'href="(https://www\.sagitario\.com\.ar/(?!page|categoria|\?)[^"]+)"',
                    html
                )
                # Filtrar URLs que parezcan de producto (no categorías)
                prod_links = [l for l in prod_links if "/?" not in l][:3]
                for prod_url in prod_links:
                    try:
                        r2 = session.get(prod_url, timeout=10)
                        h2 = r2.text
                        # Precio en la página de producto
                        m_price = re.search(
                            r'"price"\s*:\s*["\']?([\d.,]+)["\']?', h2
                        )
                        m_name  = re.search(r'<h1[^>]*class="[^"]*product[^"]*"[^>]*>(.*?)</h1>', h2, re.DOTALL)
                        if m_price:
                            precio = _clean_price(m_price.group(1))
                            nombre = re.sub(r"<[^>]+>", "", m_name.group(1)).strip() if m_name else None
                            link   = prod_url
                            break
                    except Exception:
                        continue

            if precio:
                result.update(precio=precio, url=link or url_busqueda, nombre=nombre, intento=i)
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
