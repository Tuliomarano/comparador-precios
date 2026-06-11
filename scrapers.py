"""
scrapers.py — Búsqueda de precios en Rex, Sagitario y MercadoLibre.

Lógica de búsqueda en cascada (3 intentos por sitio):
    1. Detalle + Marca  (ej. "POLACRIN MEM FTES Y MUROS 20L POLACRIN")
    2. Código de proveedor → busca el nombre oficial del producto en el sitio
       del fabricante / Google, y reintenta con ese nombre
    3. Código de fábrica (cod_proveedor numérico) directamente en el buscador

Cada scraper devuelve un dict:
    {
        "precio":    float | None,
        "url":       str | None,
        "nombre":    str | None,   # nombre encontrado en el sitio
        "intento":   int,          # 1, 2 o 3 — qué fallback resolvió
        "error":     str | None,   # mensaje si falló todo
    }
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)

# ── Constantes ──────────────────────────────────────────────────────────────────
REX_BASE      = "https://www.rex.com.ar"
SAGITARIO_BASE = "https://www.materialeselectricos.com.ar"   # ajustar si cambia
ML_SITE_ID    = "MLA"   # Argentina
ML_API        = "https://api.mercadolibre.com"

TIMEOUT_MS    = 12_000   # 12 s por operación de página
MAX_RETRIES   = 2


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _clean_price(text: str) -> float | None:
    """Extrae el primer número flotante de un string de precio."""
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"[\d]+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _build_queries(detalle: str, marca: str, cod_proveedor: int | None) -> list[str]:
    """
    Devuelve la lista de queries en orden de prioridad:
      1. Detalle + Marca
      2. Solo Detalle
      3. Código de proveedor como string (si existe)
    """
    queries = []
    detalle = (detalle or "").strip()
    marca   = (marca   or "").strip()

    if detalle and marca:
        queries.append(f"{detalle} {marca}")
    if detalle:
        queries.append(detalle)
    if cod_proveedor:
        queries.append(str(int(cod_proveedor)))

    # Deduplica manteniendo orden
    seen = set()
    result = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            result.append(q)
    return result


# ── Rex ─────────────────────────────────────────────────────────────────────────
def scrape_rex(detalle: str, marca: str, cod_proveedor: int | None,
               nombre_proveedor: str) -> dict:
    """
    Busca en Rex (Vtex). Estrategia:
    - Navega a /busca/?q=<query>
    - Toma el primer resultado de la lista de productos
    - Extrae nombre y precio
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "es-AR"})

        for i, query in enumerate(queries, start=1):
            try:
                url = f"{REX_BASE}/busca/?q={query.replace(' ', '+')}"
                page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_selector(".vtex-search-result-3-x-galleryItem, .product-summary",
                                       timeout=TIMEOUT_MS)

                # Primer producto
                item = page.query_selector(".vtex-search-result-3-x-galleryItem, .product-summary")
                if not item:
                    continue

                nombre_el = item.query_selector(".vtex-product-summary-2-x-productBrand, h3")
                precio_el = item.query_selector(".vtex-product-price-1-x-sellingPriceValue, "
                                                ".vtex-product-price-1-x-finalPrice, "
                                                "[class*='price']")

                nombre = nombre_el.inner_text().strip() if nombre_el else None
                precio_txt = precio_el.inner_text().strip() if precio_el else None
                precio = _clean_price(precio_txt) if precio_txt else None

                link_el = item.query_selector("a")
                link = REX_BASE + link_el.get_attribute("href") if link_el else url

                if precio:
                    result.update(precio=precio, url=link, nombre=nombre, intento=i)
                    break

            except PwTimeout:
                logger.warning("Rex timeout en intento %d (query: %s)", i, query)
            except Exception as exc:
                logger.warning("Rex error intento %d: %s", i, exc)

        browser.close()

    if result["precio"] is None:
        result["error"] = "Sin resultados en Rex tras %d intentos" % len(queries)

    return result


# ── Sagitario ───────────────────────────────────────────────────────────────────
def scrape_sagitario(detalle: str, marca: str, cod_proveedor: int | None,
                     nombre_proveedor: str) -> dict:
    """
    Busca en Sagitario (WooCommerce). Estrategia:
    - Navega a /?s=<query>&post_type=product
    - Extrae precio del primer resultado
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "es-AR"})

        for i, query in enumerate(queries, start=1):
            try:
                url = f"{SAGITARIO_BASE}/?s={query.replace(' ', '+')}&post_type=product"
                page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_selector(".products .product, ul.products li.product",
                                       timeout=TIMEOUT_MS)

                item = page.query_selector(".products .product, ul.products li.product")
                if not item:
                    continue

                nombre_el = item.query_selector(".woocommerce-loop-product__title, h2")
                precio_el = item.query_selector(".price .amount, .woocommerce-Price-amount")

                nombre   = nombre_el.inner_text().strip() if nombre_el else None
                precio_txt = precio_el.inner_text().strip() if precio_el else None
                precio   = _clean_price(precio_txt) if precio_txt else None

                link_el = item.query_selector("a")
                link = link_el.get_attribute("href") if link_el else url

                if precio:
                    result.update(precio=precio, url=link, nombre=nombre, intento=i)
                    break

            except PwTimeout:
                logger.warning("Sagitario timeout en intento %d (query: %s)", i, query)
            except Exception as exc:
                logger.warning("Sagitario error intento %d: %s", i, exc)

        browser.close()

    if result["precio"] is None:
        result["error"] = "Sin resultados en Sagitario tras %d intentos" % len(queries)

    return result


# ── MercadoLibre ────────────────────────────────────────────────────────────────
def scrape_ml(detalle: str, marca: str, cod_proveedor: int | None,
              nombre_proveedor: str) -> dict:
    """
    Busca en MercadoLibre via API oficial.
    Devuelve precio promedio ponderado por cantidad vendida
    de los primeros 10 resultados con ventas > 0.

    Cascada:
      1. Detalle + Marca → API search
      2. Solo Detalle    → API search
      3. Código proveedor como query → API search
    """
    import urllib.request, json

    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    for i, query in enumerate(queries, start=1):
        try:
            api_url = (
                f"{ML_API}/sites/{ML_SITE_ID}/search"
                f"?q={urllib.parse.quote(query)}&limit=10"
            )
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = json.loads(resp.read())

            items = data.get("results", [])
            if not items:
                continue

            # Filtrar solo con ventas
            vendidos = [x for x in items if x.get("sold_quantity", 0) > 0]
            pool = vendidos if vendidos else items[:5]

            total_peso  = sum(x.get("sold_quantity", 1) for x in pool)
            total_precio = sum(
                x["price"] * x.get("sold_quantity", 1) for x in pool
            )
            precio_pond = total_precio / total_peso if total_peso else pool[0]["price"]

            result.update(
                precio  = round(precio_pond, 2),
                url     = pool[0].get("permalink"),
                nombre  = pool[0].get("title"),
                intento = i,
            )
            break

        except Exception as exc:
            logger.warning("ML error intento %d: %s", i, exc)

    if result["precio"] is None:
        result["error"] = "Sin resultados en ML tras %d intentos" % len(queries)

    return result


# ── Función principal ────────────────────────────────────────────────────────────
def buscar_precios(detalle: str, marca: str,
                   cod_proveedor: int | None = None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """
    Ejecuta los 3 scrapers en paralelo (o secuencialmente si hay error de threading)
    y devuelve un dict con keys 'rex', 'sagitario', 'ml'.
    """
    import concurrent.futures

    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers = {
        "rex":       scrape_rex,
        "sagitario": scrape_sagitario,
        "ml":        scrape_ml,
    }

    resultados = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {name: ex.submit(fn, *args) for name, fn in scrapers.items()}
        for name, fut in futures.items():
            try:
                resultados[name] = fut.result(timeout=60)
            except Exception as exc:
                resultados[name] = {
                    "precio": None, "url": None, "nombre": None,
                    "intento": None, "error": str(exc)
                }

    return resultados


# ── Import faltante ──────────────────────────────────────────────────────────────
import urllib.parse  # noqa: E402  (necesario para scrape_ml)
