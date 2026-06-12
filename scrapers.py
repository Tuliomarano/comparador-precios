"""
scrapers.py — Búsqueda de precios en Rex, Sagitario, ML, Sodimac y Easy.

Enfoque: Selenium headless + browser real para cada sitio.
- UN solo driver de Chrome compartido por llamada a buscar_precios()
- Rex corre en background thread (API, no necesita browser)
- Los otros 4 scrapers corren secuencialmente compartiendo el driver

Por qué Selenium:
  Sagitario → Cloudflare JS challenge (solo un browser real puede pasarlo)
  Easy      → VTEX API pública pero bloquea requests de datacenter por fingerprint
  Sodimac   → SPA React, API bloqueada → necesita renderizado real
  ML        → anti-bot por fingerprint → browser real con stealth lo evita
"""

import re
import json
import html as _html
import logging
import difflib
import shutil
import concurrent.futures
import urllib.parse
import time

import requests

try:
    import cloudscraper as _cs_mod
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    _HAS_SELENIUM = True
except ImportError:
    _HAS_SELENIUM = False

try:
    from selenium_stealth import stealth as _apply_stealth
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

logger = logging.getLogger(__name__)

# ── User-Agent compartido ─────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent":               _UA,
    "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":          "es-AR,es;q=0.9,es-419;q=0.8",
    "Accept-Encoding":          "gzip, deflate, br",
    "Connection":               "keep-alive",
    "Upgrade-Insecure-Requests":"1",
}

SCORE_MIN = 0.20

_STOPWORDS = {
    "de", "la", "el", "los", "las", "un", "una", "y", "o", "con", "para",
    "por", "en", "del", "al", "lt", "lts", "kg", "kgs", "x", "und", "uni",
    "unidad", "color", "tono",
}
_SINONIMOS = {
    "recup":  "recuplast", "memb": "membrana", "membr": "membrana",
    "imperm": "impermeabilizante", "ext": "exterior", "int": "interior",
    "blco": "blanco", "blca": "blanca", "antiox": "antioxido",
    "sintet": "sintetico", "diluy": "diluyente", "fij": "fijador",
}


# ════════════════════════════════════════════════════════════════════════════
# SELENIUM — driver factory y helpers
# ════════════════════════════════════════════════════════════════════════════

def _make_driver():
    """
    Crea un driver de Chrome headless listo para scraping.
    Detecta automáticamente el binario de Chromium (Streamlit Cloud o local).
    Aplica selenium-stealth si está disponible para evitar detección.
    """
    if not _HAS_SELENIUM:
        return None

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={_UA}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--lang=es-AR")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Buscar binarios en rutas típicas de Streamlit Cloud y Linux
    chromium_binary = (
        shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if chromium_binary:
        opts.binary_location = chromium_binary
        logger.debug("Chromium encontrado: %s", chromium_binary)

    chromedriver_bin = (
        shutil.which("chromedriver")
        or shutil.which("chromium-chromedriver")
    )

    try:
        if chromedriver_bin:
            svc = Service(chromedriver_bin)
            driver = webdriver.Chrome(service=svc, options=opts)
        else:
            # Intentar con webdriver_manager si está disponible
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                svc = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=svc, options=opts)
            except Exception:
                driver = webdriver.Chrome(options=opts)

        # Eliminar señales de automatización del DOM
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride", {"userAgent": _UA}
        )
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        if _HAS_STEALTH:
            _apply_stealth(
                driver,
                languages=["es-AR", "es"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
            )

        return driver

    except Exception as exc:
        logger.error("No se pudo inicializar Selenium: %s", exc)
        return None


def _fetch_api_en_browser(driver, url: str, timeout_sec: int = 12):
    """
    Llama a una URL via fetch() DESDE DENTRO del browser.
    Hereda cookies, fingerprint y sesión del browser → bypasea anti-bot.
    Retorna el objeto Python parseado del JSON, o None si falla.
    """
    script = """
    var done = arguments[arguments.length - 1];
    fetch(arguments[0], {
        headers: {
            'Accept': 'application/json, text/plain, */*',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin'
        },
        credentials: 'include'
    })
    .then(r => r.text())
    .then(t => done(t))
    .catch(e => done(null));
    """
    try:
        driver.set_script_timeout(timeout_sec)
        raw = driver.execute_async_script(script, url)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("fetch_en_browser error para %s: %s", url[:80], exc)
        return None


def _esperar_render(driver, css_selector: str, timeout: int = 8):
    """Espera a que un elemento CSS esté presente en el DOM (React renderizó)."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css_selector))
        )
    except Exception:
        pass  # Timeout → usamos lo que hay


# ════════════════════════════════════════════════════════════════════════════
# HELPERS compartidos (texto, precio, scoring, queries)
# ════════════════════════════════════════════════════════════════════════════

def _clean_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    text = re.sub(r"[^\d,.]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", "") if re.match(r"^\d{1,3},\d{3}$", text) else text.replace(",", ".")
    else:
        if re.match(r"^\d{1,3}(\.\d{3})+$", text):
            text = text.replace(".", "")
    try:
        v = float(text)
        return v if v > 0 else None
    except ValueError:
        return None


def _normalizar(s: str) -> str:
    if not s:
        return ""
    s = _html.unescape(str(s)).lower()
    for a, b in (("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")):
        s = s.replace(a, b)
    s = re.sub(r"(\d+)\s*(kgs?|kilos?)\b",    r"\1kg", s)
    s = re.sub(r"(\d+)\s*(lts?|litros?|l)\b", r"\1lt", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> list[str]:
    return [
        _SINONIMOS.get(t, t)
        for t in _normalizar(s).split()
        if t not in _STOPWORDS and len(t) > 1
    ]


def score_similitud(buscado: str, encontrado: str) -> float:
    tb, te = _tokens(buscado), _tokens(encontrado)
    if not tb or not te:
        return 0.0
    set_b, set_e = set(tb), set(te)
    comunes = set_b & set_e
    cob = len(comunes) / len(set_b)
    if cob < 1.0:
        extra = sum(
            1 for t in (set_b - set_e)
            if any(t in e or e in t for e in set_e if len(t) >= 4 and len(e) >= 4)
        )
        cob = min(1.0, cob + extra / len(set_b))
    nums_b = {t for t in set_b if any(c.isdigit() for c in t)}
    nums_e = {t for t in set_e if any(c.isdigit() for c in t)}
    mn = len(nums_b & nums_e) / len(nums_b) if nums_b else 1.0
    fz = difflib.SequenceMatcher(None, " ".join(tb), " ".join(te)).ratio()
    return round(min(1.0, 0.60*cob + 0.25*mn + 0.15*fz), 3)


def _cod_str(cod) -> str | None:
    if cod in (None, "", "nan", "None"):
        return None
    try:
        return str(int(float(cod)))
    except (ValueError, TypeError):
        return str(cod).strip() or None


def _query_comercial(detalle: str, marca: str) -> str:
    toks = _tokens(detalle)
    marca_toks = _tokens(marca)
    base = [t for t in toks if t not in set(marca_toks)][:6]
    return " ".join(base + marca_toks).strip()


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[dict]:
    detalle, marca = (detalle or "").strip(), (marca or "").strip()
    ref = f"{detalle} {marca}".strip()
    cod = _cod_str(cod_proveedor)
    q_com = _query_comercial(detalle, marca)
    q_det = " ".join(_tokens(detalle)[:6])
    queries, seen = [], set()
    for q in [q_com, q_det, cod]:
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append({"q": q, "ref": ref or q})
    return queries


def _empty_result() -> dict:
    return {"precio": None, "url": None, "nombre": None,
            "intento": None, "score": None, "error": None}


def _dedup(cands: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cands:
        k = (c.get("nombre") or "").lower()[:60]
        if k and k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _elegir_mejor(cands: list[dict], ref: str) -> dict | None:
    if not cands:
        return None
    for c in cands:
        c["score"] = score_similitud(ref, c.get("nombre", ""))
    cands.sort(key=lambda c: c["score"], reverse=True)
    for c in cands:
        if c["score"] >= SCORE_MIN and c.get("precio"):
            return c
    top = cands[0]
    return top if top["score"] >= SCORE_MIN and top.get("precio") else None


def _candidatos_de_jsonld(html: str) -> list[dict]:
    cands = []
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            obj = json.loads(blk.strip())
        except Exception:
            continue
        nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
        for node in (nodes if isinstance(nodes, list) else [nodes]):
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            tlist = t if isinstance(t, list) else [t]
            if "Product" in tlist:
                nombre = node.get("name")
                link = node.get("url") or node.get("@id")
                offers = node.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                precio = _clean_price(
                    offers.get("price") or offers.get("lowPrice")
                ) if isinstance(offers, dict) else None
                if nombre:
                    cands.append({"nombre": nombre, "link": link, "precio": precio})
            if "ItemList" in tlist:
                for el in node.get("itemListElement", []):
                    prod = el.get("item", el) if isinstance(el, dict) else {}
                    if not isinstance(prod, dict):
                        continue
                    nombre = prod.get("name")
                    link = prod.get("url") or prod.get("@id")
                    offers = prod.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    precio = _clean_price(
                        offers.get("price") or offers.get("lowPrice")
                    ) if isinstance(offers, dict) else None
                    if nombre:
                        cands.append({"nombre": nombre, "link": link, "precio": precio})
    return _dedup(cands)


# ════════════════════════════════════════════════════════════════════════════
# SAGITARIO — WooCommerce SSR (Cloudflare)
# Selenium resuelve el JS challenge de Cloudflare que bloquea requests simples
# ════════════════════════════════════════════════════════════════════════════

def _sagitario_candidatos_de_html(html: str) -> list[dict]:
    """
    Parser posicional — no depende de límites de </li> que se cortan.
    Estrategia: posición de headings de producto ↔ posición de precios en el HTML.
    """
    headings: list[tuple[int, str, str]] = []

    for m in re.finditer(
        r'woocommerce-loop-product__title[^>]*>.*?'
        r'<a\s[^>]*href="(https://pintureriasagitario\.com\.ar/producto/[^"]+)"[^>]*>'
        r'(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        nombre = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if nombre:
            headings.append((m.start(), m.group(1), nombre))

    if not headings:
        for m in re.finditer(
            r'<h[1-6][^>]*>.*?'
            r'<a\s[^>]*href="(https://pintureriasagitario\.com\.ar/producto/[^"]+)"[^>]*>'
            r'(.*?)</a>.*?</h[1-6]>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            nombre = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if nombre:
                headings.append((m.start(), m.group(1), nombre))

    if not headings:
        return []

    # Precios: "El precio actual es: $ NNN" (texto accesible WooCommerce)
    precios_actuales = [
        (m.start(), p)
        for m in re.finditer(r'El precio actual es:.*?\$\s*([\d.,]+)', html, re.DOTALL)
        if (p := _clean_price(m.group(1))) and p > 100
    ]

    # Precio en <ins><bdi> (oferta)
    precios_ins = [
        (m.start(), p)
        for m in re.finditer(
            r'<ins[^>]*>.*?<bdi[^>]*>(.*?)</bdi>.*?</ins>',
            html, re.DOTALL | re.IGNORECASE
        )
        if (p := _clean_price(re.sub(r"<[^>]+>", "", m.group(1)))) and p > 100
    ]

    # Precio en <bdi> sin <del>
    html_sin_del = re.sub(r"<del[^>]*>.*?</del>", "", html,
                          flags=re.DOTALL | re.IGNORECASE)
    precios_bdi = [
        (m.start(), p)
        for m in re.finditer(r'<bdi[^>]*>(.*?)</bdi>', html_sin_del, re.DOTALL | re.IGNORECASE)
        if (p := _clean_price(re.sub(r"<[^>]+>", "", m.group(1)))) and p > 100
    ]

    cands = []
    for i, (hpos, url, nombre) in enumerate(headings):
        next_pos = headings[i + 1][0] if i + 1 < len(headings) else len(html)
        precio = None
        for ppos, p in precios_actuales:
            if hpos <= ppos < next_pos:
                precio = p
                break
        if precio is None:
            for ppos, p in precios_ins:
                if hpos <= ppos < next_pos:
                    precio = p
                    break
        if precio is None:
            for ppos, p in precios_bdi:
                if hpos <= ppos < next_pos:
                    precio = p
                    break
        cands.append({"nombre": nombre, "link": url, "precio": precio})

    return _dedup(cands)


def _scrape_sagitario_browser(driver, detalle: str, marca: str,
                               cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Como un humano:
    1. Entra a pintureriasagitario.com.ar (Cloudflare: solo Chrome real pasa)
    2. Busca el producto con la barra de búsqueda (navega a la URL de search)
    3. Lee el HTML renderizado y extrae productos
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()

    try:
        # Paso 1: Homepage (Cloudflare challenge — Chrome lo resuelve automáticamente)
        driver.get("https://pintureriasagitario.com.ar/")
        time.sleep(3)  # Esperar que Cloudflare valide el browser
    except Exception as exc:
        result["error"] = f"Sagitario warmup error: {exc}"
        return result

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        try:
            # Paso 2: Navegar a resultados de búsqueda (igual que tipear en el buscador)
            search_url = (
                "https://pintureriasagitario.com.ar/?"
                + urllib.parse.urlencode({"s": query, "post_type": "product"})
            )
            driver.get(search_url)
            # Esperar a que aparezcan las tarjetas de producto
            _esperar_render(driver, ".products .product, ul.products li", timeout=8)

            html = driver.page_source

            # Detectar si Cloudflare devolvió captcha en vez de productos
            if "challenge" in driver.current_url or (
                "cloudflare" in html.lower() and "product" not in html.lower()
            ):
                logger.warning("Sagitario: Cloudflare challenge activo")
                time.sleep(4)
                html = driver.page_source

            candidatos = _sagitario_candidatos_de_html(html)
            if not candidatos:
                continue

            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            relevantes = [c for c in candidatos if c["score"] >= SCORE_MIN]

            for c in relevantes[:3]:
                if c.get("precio"):
                    result.update(
                        precio=c["precio"], url=c.get("link") or search_url,
                        nombre=c["nombre"], intento=i, score=c["score"],
                    )
                    return result

        except Exception as exc:
            logger.warning("Sagitario intento %d: %s", i, exc)

    if result["precio"] is None:
        result["error"] = "Sin resultados en Sagitario"
    return result


# ════════════════════════════════════════════════════════════════════════════
# EASY — VTEX (API pública, confirmada ✓)
# El problema es que requests de datacenter son bloqueados.
# Solución: fetch() DESDE DENTRO del browser (hereda cookies + fingerprint)
# ════════════════════════════════════════════════════════════════════════════

def _vtex_candidatos_de_json(data, base_url: str) -> list[dict]:
    """Parsea JSON de la API legacy de VTEX (Easy y Sodimac usan el mismo formato)."""
    if not isinstance(data, list):
        return []
    cands = []
    for prod in data[:10]:
        nombre = prod.get("productName") or prod.get("productTitle")
        link_text = prod.get("linkText", "")
        link = f"{base_url}/{link_text}/p" if link_text else None
        precio = None
        for item in prod.get("items", [])[:1]:
            for seller in item.get("sellers", [])[:1]:
                offer = seller.get("commertialOffer") or seller.get("commercialOffer") or {}
                precio = _clean_price(offer.get("Price") or offer.get("ListPrice"))
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def _vtex_intelligent_search(data, base_url: str) -> list[dict]:
    """Parsea respuesta del Intelligent Search de VTEX."""
    cands = []
    prods = (data or {}).get("products", []) if isinstance(data, dict) else (data or [])
    for prod in prods[:10]:
        if not isinstance(prod, dict):
            continue
        nombre = prod.get("productName") or prod.get("name") or prod.get("productTitle")
        slug = prod.get("linkText") or prod.get("slug") or ""
        link = f"{base_url}/{slug}/p" if slug else None
        precio = None
        pr = prod.get("priceRange", {})
        if pr:
            precio = _clean_price(pr.get("sellingPrice", {}).get("lowPrice"))
        if precio is None:
            for item in prod.get("items", [])[:1]:
                for seller in item.get("sellers", [])[:1]:
                    offer = seller.get("commertialOffer") or {}
                    precio = _clean_price(offer.get("Price") or offer.get("ListPrice"))
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def _scrape_easy_browser(driver, detalle: str, marca: str,
                          cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Como un humano:
    1. Abre easy.com.ar (VTEX setea cookies de sesión)
    2. Llama la API de catálogo desde DENTRO del browser (fetch() hereda cookies)
    3. Extrae productos y precios del JSON
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()

    try:
        driver.get("https://www.easy.com.ar/")
        time.sleep(2)  # VTEX necesita tiempo para setear cookies
    except Exception as exc:
        result["error"] = f"Easy warmup error: {exc}"
        return result

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        palabras = query.split()
        q_full   = urllib.parse.quote(" ".join(palabras[:4]))
        q_short  = urllib.parse.quote(palabras[0]) if palabras else q_full

        candidatos: list[dict] = []

        # Probar las API de VTEX desde dentro del browser
        for api_url in [
            f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_full}?_from=0&_to=9",
            f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_short}?_from=0&_to=9",
            f"https://www.easy.com.ar/_v/api/intelligent-search/product_search/trade-policy/1?query={q_full}&count=10&sort=score_desc",
        ]:
            data = _fetch_api_en_browser(driver, api_url)
            if isinstance(data, list):
                candidatos = _vtex_candidatos_de_json(data, "https://www.easy.com.ar")
            elif isinstance(data, dict):
                candidatos = _vtex_intelligent_search(data, "https://www.easy.com.ar")
            if candidatos:
                break

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                          nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None:
        result["error"] = "Sin resultados en Easy"
    return result


# ════════════════════════════════════════════════════════════════════════════
# SODIMAC — VTEX SPA
# Su API está bloqueada desde afuera. Solución: renderizar la página de
# búsqueda con Selenium y parsear el DOM resultante
# ════════════════════════════════════════════════════════════════════════════

def _sodimac_candidatos_de_html(html: str) -> list[dict]:
    """Extrae productos del HTML renderizado de Sodimac (VTEX IO frontend)."""
    cands = []

    # 1) JSON-LD (VTEX IO incluye esto en el DOM renderizado)
    cands = _candidatos_de_jsonld(html)

    # 2) Regex sobre el HTML para nombres y precios (VTEX IO)
    if not cands:
        # Buscar bloques de producto por patrones de precio + nombre
        for blk in re.split(r'(?=<[a-z][^>]*\bproduct[^>]*>|<article)', html, flags=re.IGNORECASE):
            nom_m = re.search(
                r'(?:productName|product-name|vtex-product-name)[^>]*>(.*?)<',
                blk, re.DOTALL | re.IGNORECASE
            )
            if not nom_m:
                nom_m = re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            link_m = re.search(r'href="(/[^"]*(?:p/?$|/p\?)[^"]*|[^"]*sodimac\.com\.ar[^"]*)"', blk)
            price_m = re.search(
                r'(?:sellingPrice|bestPrice|Price)["\s:]+\$?\s*([\d.,]+)',
                blk, re.IGNORECASE
            )
            if not price_m:
                price_m = re.search(r'\$([\d.,]+)', blk)
            if nom_m:
                nombre = re.sub(r"<[^>]+>", "", nom_m.group(1)).strip()
                precio = _clean_price(price_m.group(1)) if price_m else None
                link = link_m.group(1) if link_m else None
                if link and not link.startswith("http"):
                    link = "https://www.sodimac.com.ar" + link
                if nombre and len(nombre) > 3:
                    cands.append({"nombre": nombre, "link": link, "precio": precio})

    # 3) Extraer del JSON de estado de la tienda (VTEX IO inyecta __STATE__)
    if not cands:
        state_m = re.search(r'window\.__STATE__\s*=\s*({.*?});', html, re.DOTALL)
        if state_m:
            try:
                state = json.loads(state_m.group(1))
                for key, val in state.items():
                    if isinstance(val, dict) and val.get("__typename") == "Product":
                        nombre = val.get("productName") or val.get("name")
                        slug = val.get("linkText") or val.get("slug") or ""
                        link = f"https://www.sodimac.com.ar/{slug}/p" if slug else None
                        precio = None
                        items = val.get("items") or []
                        for item in (items[:1] if isinstance(items, list) else []):
                            sellers = item.get("sellers") or []
                            for seller in (sellers[:1] if isinstance(sellers, list) else []):
                                offer = seller.get("commertialOffer") or {}
                                precio = _clean_price(offer.get("Price") or offer.get("ListPrice"))
                        if nombre:
                            cands.append({"nombre": nombre, "link": link, "precio": precio})
            except Exception:
                pass

    return _dedup(cands)


def _scrape_sodimac_browser(driver, detalle: str, marca: str,
                             cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Como un humano:
    1. Abre sodimac.com.ar (establece sesión VTEX)
    2. Navega a la búsqueda y espera que React renderice los productos
    3. Extrae del DOM renderizado
    También intenta la API de catálogo VTEX desde dentro del browser
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()

    try:
        driver.get("https://www.sodimac.com.ar/")
        time.sleep(2)
    except Exception as exc:
        result["error"] = f"Sodimac warmup error: {exc}"
        return result

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        palabras = query.split()
        q_full   = urllib.parse.quote(" ".join(palabras[:4]))
        q_short  = urllib.parse.quote(palabras[0]) if palabras else q_full

        candidatos: list[dict] = []

        # A) API VTEX catalog desde dentro del browser (misma sesión)
        for api_url in [
            f"https://www.sodimac.com.ar/api/catalog_system/pub/products/search/{q_full}?_from=0&_to=9",
            f"https://www.sodimac.com.ar/api/catalog_system/pub/products/search/{q_short}?_from=0&_to=9",
            f"https://www.sodimac.com.ar/_v/api/intelligent-search/product_search/trade-policy/1?query={q_full}&count=10",
        ]:
            data = _fetch_api_en_browser(driver, api_url)
            if isinstance(data, list):
                candidatos = _vtex_candidatos_de_json(data, "https://www.sodimac.com.ar")
            elif isinstance(data, dict):
                candidatos = _vtex_intelligent_search(data, "https://www.sodimac.com.ar")
            if candidatos:
                break

        # B) Si API vacía: navegar a la página de búsqueda y parsear el DOM renderizado
        if not candidatos:
            try:
                search_url = (
                    f"https://www.sodimac.com.ar/sodimac-ar/search?"
                    + urllib.parse.urlencode({"Ntt": " ".join(palabras[:4])})
                )
                driver.get(search_url)
                # Esperar a que React renderice las tarjetas de producto
                _esperar_render(driver, "[class*='product'], [class*='Product'], article", timeout=8)
                time.sleep(1)  # Margen extra para carga lazy de precios
                candidatos = _sodimac_candidatos_de_html(driver.page_source)
            except Exception as exc:
                logger.warning("Sodimac search page error: %s", exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                          nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None:
        result["error"] = "Sin resultados en Sodimac"
    return result


# ════════════════════════════════════════════════════════════════════════════
# MERCADO LIBRE
# El anti-bot es fuerte pero un browser real con stealth ayuda
# ════════════════════════════════════════════════════════════════════════════

def _ml_candidatos_de_html(html: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)
    if len(cands) < 3:
        for blk in re.split(r'<li[^>]*class="[^"]*ui-search-layout__item[^"]*"', html)[1:]:
            t = (
                re.search(r'class="[^"]*ui-search-item__title[^"]*"[^>]*>(.*?)<', blk, re.DOTALL)
                or re.search(r'class="[^"]*poly-component__title[^"]*"[^>]*>(.*?)<', blk, re.DOTALL)
                or re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            )
            href = re.search(r'href="(https://[^"]*mercadolibre[^"]*)"', blk)
            price = (
                re.search(r'andes-money-amount__fraction[^>]*>([\d.\s]+)<', blk)
                or re.search(r'price-tag-fraction[^>]*>([\d.\s]+)<', blk)
            )
            if t and href:
                nombre = re.sub(r"<[^>]+>", "", t.group(1)).strip()
                if nombre:
                    cands.append({
                        "nombre": _html.unescape(nombre),
                        "link":   href.group(1).split("#")[0],
                        "precio": _clean_price(price.group(1)) if price else None,
                    })
    return _dedup(cands)


def _scrape_ml_browser(driver, detalle: str, marca: str,
                        cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Como un humano:
    1. Abre mercadolibre.com.ar (establece sesión ML)
    2. Intenta la API desde dentro del browser (hereda cookies de sesión ML)
    3. Si no: navega al listado y espera que React renderice
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()

    try:
        driver.get("https://www.mercadolibre.com.ar/")
        time.sleep(2)
    except Exception as exc:
        result["error"] = f"ML warmup error: {exc}"
        return result

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:3])
        candidatos: list[dict] = []

        # A) API de ML desde dentro del browser (hereda sesión ML)
        api_url = (
            f"https://api.mercadolibre.com/sites/MLA/search"
            f"?q={urllib.parse.quote(q_corta)}&limit=10"
        )
        data = _fetch_api_en_browser(driver, api_url)
        if isinstance(data, dict):
            for prod in data.get("results", [])[:10]:
                candidatos.append({
                    "nombre": prod.get("title"),
                    "link":   prod.get("permalink"),
                    "precio": _clean_price(prod.get("price")),
                })

        # B) Si la API sigue bloqueada: navegar al listado y parsear el DOM
        if not candidatos:
            try:
                slug = urllib.parse.quote(q_corta.replace(" ", "-").lower())
                listado_url = f"https://listado.mercadolibre.com.ar/{slug}"
                driver.get(listado_url)
                # Esperar que React renderice los ítems
                _esperar_render(driver, ".ui-search-layout__item, .ui-search-result", timeout=10)
                time.sleep(1)
                html = driver.page_source
                if "suspicious-traffic" not in driver.current_url:
                    candidatos = _ml_candidatos_de_html(html)
            except Exception as exc:
                logger.warning("ML listado error: %s", exc)

        if not candidatos:
            continue

        for c in candidatos:
            c["score"] = score_similitud(ref, c.get("nombre") or "")
        candidatos.sort(key=lambda c: c["score"], reverse=True)
        relevantes = [c for c in candidatos if c["score"] >= SCORE_MIN and c.get("precio")]
        if not relevantes:
            continue

        top = relevantes[:5]
        precios = sorted(c["precio"] for c in top)
        mediana = precios[len(precios) // 2]
        result.update(precio=mediana, url=top[0].get("link"),
                      nombre=top[0].get("nombre"), intento=i, score=top[0]["score"])
        break

    if result["precio"] is None:
        result["error"] = "Sin resultados en ML (anti-bot activo)"
    return result


# ════════════════════════════════════════════════════════════════════════════
# REX — Magento 2 GraphQL + REST (ya funciona ✓)
# ════════════════════════════════════════════════════════════════════════════

def _make_session():
    if _HAS_CLOUDSCRAPER:
        sc = _cs_mod.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        sc.headers.update(HEADERS)
        return sc
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _rex_graphql(session, query: str) -> list[dict]:
    gql = {
        "query": '{ products(search: "%s", pageSize: 10) { items { name url_key '
                 'price_range { minimum_price { final_price { value } regular_price { value } } } } } }'
                 % query.replace('"', '\\"')
    }
    try:
        r = session.post(
            "https://www.somosrex.com/graphql",
            json=gql,
            headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("data", {}).get("products", {}).get("items", [])
        return [
            {
                "nombre": p.get("name"),
                "link":   f"https://www.somosrex.com/{p.get('url_key','')}.html",
                "precio": _clean_price(
                    p.get("price_range", {}).get("minimum_price", {})
                    .get("final_price", {}).get("value")
                    or p.get("price_range", {}).get("minimum_price", {})
                    .get("regular_price", {}).get("value")
                ),
            }
            for p in items[:10] if p.get("name")
        ]
    except Exception as exc:
        logger.warning("Rex GraphQL: %s", exc)
        return []


def _rex_rest(session, query: str) -> list[dict]:
    q_enc = urllib.parse.quote(f"%{query}%")
    url = (
        "https://www.somosrex.com/rest/all/V1/products"
        f"?searchCriteria[filterGroups][0][filters][0][field]=name"
        f"&searchCriteria[filterGroups][0][filters][0][value]={q_enc}"
        f"&searchCriteria[filterGroups][0][filters][0][condition_type]=like"
        f"&searchCriteria[pageSize]=10"
        f"&fields=items[name,price,url_key]"
    )
    try:
        r = session.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        if r.status_code != 200:
            return []
        return [
            {
                "nombre": p.get("name"),
                "link":   f"https://www.somosrex.com/{p.get('url_key','')}.html",
                "precio": _clean_price(p.get("price")),
            }
            for p in r.json().get("items", [])[:10] if p.get("name")
        ]
    except Exception as exc:
        logger.warning("Rex REST: %s", exc)
        return []


def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    session = _make_session()

    for i, item in enumerate(queries, start=1):
        q_corta = " ".join(item["q"].split()[:5])
        ref     = item["ref"]

        cands = _rex_graphql(session, q_corta) or _rex_rest(session, q_corta)
        mejor = _elegir_mejor(cands, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                          nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None:
        result["error"] = "Sin resultados en Rex"
    return result


# ════════════════════════════════════════════════════════════════════════════
# Orquestador principal
# ════════════════════════════════════════════════════════════════════════════

def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """
    Estrategia de ejecución:
    - Rex: background thread (API pura, rápida, no necesita browser)
    - Sagitario / Easy / Sodimac / ML: UNA sola instancia de Chrome compartida,
      ejecutados secuencialmente para no consumir más de ~300MB de RAM

    Si Selenium no está disponible (no hay Chrome), cae a requests.
    """
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    resultados: dict[str, dict] = {}

    # Rex corre en paralelo desde el principio
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    rex_fut  = executor.submit(scrape_rex, *args)

    # Driver compartido para los 4 scrapers restantes
    driver = _make_driver()

    if driver:
        scrapers_browser = [
            ("sagitario", _scrape_sagitario_browser),
            ("easy",      _scrape_easy_browser),
            ("sodimac",   _scrape_sodimac_browser),
            ("ml",        _scrape_ml_browser),
        ]
        try:
            for nombre_sitio, fn in scrapers_browser:
                try:
                    resultados[nombre_sitio] = fn(driver, *args)
                except Exception as exc:
                    r = _empty_result()
                    r["error"] = str(exc)
                    resultados[nombre_sitio] = r
        finally:
            try:
                driver.quit()
            except Exception:
                pass
    else:
        # Fallback: requests con cloudscraper (sin browser)
        logger.warning("Selenium no disponible — usando requests como fallback")
        fallback_scrapers = {
            "sagitario": _scrape_sagitario_requests,
            "easy":      _scrape_easy_requests,
            "sodimac":   _scrape_sodimac_requests,
            "ml":        _scrape_ml_requests,
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {n: ex.submit(fn, *args) for n, fn in fallback_scrapers.items()}
            for n, fut in futs.items():
                try:
                    resultados[n] = fut.result(timeout=40)
                except Exception as exc:
                    r = _empty_result()
                    r["error"] = str(exc)
                    resultados[n] = r

    # Recoger resultado de Rex
    try:
        resultados["rex"] = rex_fut.result(timeout=45)
    except Exception as exc:
        r = _empty_result()
        r["error"] = str(exc)
        resultados["rex"] = r
    finally:
        executor.shutdown(wait=False)

    return resultados


# ════════════════════════════════════════════════════════════════════════════
# FALLBACK con requests (si Selenium no está disponible)
# ============================================================================

def _scrape_sagitario_requests(detalle, marca, cod_proveedor, nombre_proveedor):
    """Fallback Sagitario usando cloudscraper (puede fallar en datacenter IPs)."""
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        q = _build_query(detalle, marca, cod_proveedor, nombre_proveedor)
        url = f"https://pintureriasagitario.com.ar/?s={urllib.parse.quote_plus(q)}&post_type=product"
        # Warmup homepage
        scraper.get("https://pintureriasagitario.com.ar/", timeout=10)
        time.sleep(1)
        r = scraper.get(url, timeout=15)
        if r.status_code != 200:
            res = _empty_result()
            res["error"] = f"HTTP {r.status_code}"
            return res
        candidatos = _sagitario_candidatos_de_html(r.text)
        return _best_match(candidatos, detalle, marca)
    except Exception as exc:
        res = _empty_result()
        res["error"] = str(exc)
        return res


def _scrape_easy_requests(detalle, marca, cod_proveedor, nombre_proveedor):
    """Fallback Easy usando VTEX Catalog API con requests."""
    try:
        q = _build_query(detalle, marca, cod_proveedor, nombre_proveedor)
        q_short = detalle.strip() if detalle else q
        headers = {
            "User-Agent": _UA,
            "Accept": "application/json",
            "Referer": "https://www.easy.com.ar/",
        }
        sess = requests.Session()
        # Warmup to get session cookies
        try:
            sess.get("https://www.easy.com.ar/", headers=headers, timeout=10)
        except Exception:
            pass
        for query in [q, q_short]:
            url = (
                f"https://www.easy.com.ar/api/catalog_system/pub/products/search/"
                f"{urllib.parse.quote(query)}?_from=0&_to=9"
            )
            try:
                resp = sess.get(url, headers=headers, timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        candidatos = _easy_parse_vtex(data)
                        res = _best_match(candidatos, detalle, marca)
                        if res.get("precio"):
                            return res
            except Exception:
                continue
        res = _empty_result()
        res["error"] = "Sin resultados Easy (requiere browser para VTEX)"
        return res
    except Exception as exc:
        res = _empty_result()
        res["error"] = str(exc)
        return res


def _scrape_sodimac_requests(detalle, marca, cod_proveedor, nombre_proveedor):
    """Sodimac requiere browser SPA — no disponible en modo requests."""
    res = _empty_result()
    res["error"] = "Sodimac requiere Selenium (SPA React)"
    return res


def _scrape_ml_requests(detalle, marca, cod_proveedor, nombre_proveedor):
    """Fallback MercadoLibre usando API publica."""
    try:
        q = _build_query(detalle, marca, cod_proveedor, nombre_proveedor)
        url = (
            f"https://api.mercadolibre.com/sites/MLA/search"
            f"?q={urllib.parse.quote(q)}&limit=10"
        )
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            res = _empty_result()
            res["error"] = f"ML API HTTP {r.status_code}"
            return res
        data = r.json()
        candidatos = []
        for item in data.get("results", []):
            nombre = item.get("title", "")
            precio = item.get("price")
            link = item.get("permalink", "")
            if nombre and precio:
                candidatos.append({"nombre": nombre, "precio": float(precio), "link": link})
        return _best_match(candidatos, detalle, marca)
    except Exception as exc:
        res = _empty_result()
        res["error"] = str(exc)
        return res
