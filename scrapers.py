"""
scrapers.py — Búsqueda de precios en Rex, Sagitario y MercadoLibre.

Lógica de búsqueda en cascada (3 intentos por sitio):
    1. Detalle + Marca  (ej. "POLACRIN MEM FTES Y MUROS 20L POLACRIN")
    2. Solo Detalle
    3. Código de proveedor numérico como query

Cada scraper devuelve un dict:
    {
        "precio":  float | None,
        "url":     str | None,
        "nombre":  str | None,
        "intento": int,           # 1, 2 o 3
        "error":   str | None,
    }
"""

import re
import logging
import urllib.request
import urllib.parse
import json
import concurrent.futures

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

logger = logging.getLogger(__name__)

# ── Constantes ──────────────────────────────────────────────────────────────────
REX_BASE       = "https://www.rex.com.ar"
SAGITARIO_BASE = "https://www.sagitario.com.ar"
ML_SITE_ID     = "MLA"
ML_API         = "https://api.mercadolibre.com"
TIMEOUT_MS     = 15_000

# Chromium instalado via packages.txt en Streamlit Cloud
CHROMIUM_PATH  = "/usr/bin/chromium-browser"


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _clean_price(text: str) -> float | None:
    text = re.sub(r"[^\d,.]", "", text)
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[str]:
    queries, seen = [], set()
    detalle = (detalle or "").strip()
    marca   = (marca   or "").strip()

    candidates = [
        f"{detalle} {marca}" if marca else None,
        detalle or None,
        str(int(cod_proveedor)) if cod_proveedor and str(cod_proveedor) != "nan" else None,
    ]
    for q in candidates:
        if q and q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


def _launch_browser(pw):
    """Lanza Chromium usando el binario del sistema si existe, sino el de Playwright."""
    import os
    kwargs = dict(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    if os.path.exists(CHROMIUM_PATH):
        kwargs["executable_path"] = CHROMIUM_PATH
    return pw.chromium.launch(**kwargs)


# ── Rex ─────────────────────────────────────────────────────────────────────────
def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page    = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "es-AR,es;q=0.9"})

        for i, query in enumerate(queries, start=1):
            try:
                url = f"{REX_BASE}/busca/?q={urllib.parse.quote(query)}"
                page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_selector(
                    ".vtex-search-result-3-x-galleryItem, .product-summary",
                    timeout=TIMEOUT_MS,
                )
                item = page.query_selector(".vtex-search-result-3-x-galleryItem, .product-summary")
                if not item:
                    continue

                nombre_el = item.query_selector(
                    ".vtex-product-summary-2-x-productBrand, h3, .product-summary-name"
                )
                precio_el = item.query_selector(
                    ".vtex-product-price-1-x-sellingPriceValue, "
                    ".vtex-product-price-1-x-finalPrice, "
                    "[class*='sellingPrice'], [class*='price']"
                )

                nombre    = nombre_el.inner_text().strip() if nombre_el else None
                precio_txt = precio_el.inner_text().strip() if precio_el else None
                precio    = _clean_price(precio_txt) if precio_txt else None

                link_el = item.query_selector("a")
                link = (REX_BASE + link_el.get_attribute("href")) if link_el else url

                if precio:
                    result.update(precio=precio, url=link, nombre=nombre, intento=i)
                    break

            except PwTimeout:
                logger.warning("Rex timeout intento %d", i)
            except Exception as exc:
                logger.warning("Rex error intento %d: %s", i, exc)

        browser.close()

    if result["precio"] is None:
        result["error"] = f"Sin resultados en Rex tras {len(queries)} intentos"
    return result


# ── Sagitario ───────────────────────────────────────────────────────────────────
def scrape_sagitario(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        page    = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "es-AR,es;q=0.9"})

        for i, query in enumerate(queries, start=1):
            try:
                url = f"{SAGITARIO_BASE}/?s={urllib.parse.quote(query)}&post_type=product"
                page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_selector(
                    "ul.products li.product, .products .product",
                    timeout=TIMEOUT_MS,
                )
                item = page.query_selector("ul.products li.product, .products .product")
                if not item:
                    continue

                nombre_el = item.query_selector(
                    ".woocommerce-loop-product__title, h2, .product-title"
                )
                precio_el = item.query_selector(
                    ".price .amount, .woocommerce-Price-amount, ins .amount"
                )

                nombre    = nombre_el.inner_text().strip() if nombre_el else None
                precio_txt = precio_el.inner_text().strip() if precio_el else None
                precio    = _clean_price(precio_txt) if precio_txt else None

                link_el = item.query_selector("a")
                link = link_el.get_attribute("href") if link_el else url

                if precio:
                    result.update(precio=precio, url=link, nombre=nombre, intento=i)
                    break

            except PwTimeout:
                logger.warning("Sagitario timeout intento %d", i)
            except Exception as exc:
                logger.warning("Sagitario error intento %d: %s", i, exc)

        browser.close()

    if result["precio"] is None:
        result["error"] = f"Sin resultados en Sagitario tras {len(queries)} intentos"
    return result


# ── MercadoLibre ────────────────────────────────────────────────────────────────
def scrape_ml(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = {"precio": None, "url": None, "nombre": None, "intento": None, "error": None}

    for i, query in enumerate(queries, start=1):
        try:
            api_url = (
                f"{ML_API}/sites/{ML_SITE_ID}/search"
                f"?q={urllib.parse.quote(query)}&limit=10"
            )
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            items = data.get("results", [])
            if not items:
                continue

            vendidos = [x for x in items if x.get("sold_quantity", 0) > 0]
            pool     = vendidos if vendidos else items[:5]

            total_peso   = sum(x.get("sold_quantity", 1) for x in pool)
            total_precio = sum(x["price"] * x.get("sold_quantity", 1) for x in pool)
            precio_pond  = round(total_precio / total_peso, 2) if total_peso else pool[0]["price"]

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
    """Ejecuta los 3 scrapers en paralelo y devuelve dict con keys 'rex', 'sagitario', 'ml'."""
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
                    "intento": None, "error": str(exc),
                }
    return resultados
