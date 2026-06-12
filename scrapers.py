"""
scrapers.py — Búsqueda de precios en Rex, Sagitario, ML, Sodimac y Easy.

Enfoque "humano": cada scraper primero visita la homepage del sitio para
establecer cookies y sesión (igual que haría un navegador real), y recién
después realiza la búsqueda. Esto evita que los sistemas anti-bot bloqueen
las requests "frías" directas a URLs de búsqueda.

Tecnologías (jun 2026):
  Rex        → somosrex.com               → Magento 2 GraphQL + REST
  Sagitario  → pintureriasagitario.com.ar → WooCommerce SSR (parser posicional)
  ML         → mercadolibre.com.ar        → API + HTML (warmup sesión)
  Sodimac    → sodimac.com.ar             → VTEX Catalog API (warmup sesión)
  Easy       → easy.com.ar               → VTEX Catalog API (warmup sesión, confirmado ✓)
"""

import re
import json
import html as _html
import logging
import difflib
import concurrent.futures
import urllib.parse

import requests

try:
    import cloudscraper as _cs_mod
    _HAS_CLOUDSCRAPER = True
except ImportError:
    _HAS_CLOUDSCRAPER = False

logger = logging.getLogger(__name__)

# ── Headers base ──────────────────────────────────────────────────────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_SEC_CH = (
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
)

# Como cuando una persona navega a una página web
HEADERS_NAV = {
    "User-Agent":               _UA,
    "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":          "es-AR,es;q=0.9,es-419;q=0.8",
    "Accept-Encoding":          "gzip, deflate, br",
    "Sec-Ch-Ua":                _SEC_CH,
    "Sec-Ch-Ua-Mobile":         "?0",
    "Sec-Ch-Ua-Platform":       '"Windows"',
    "Sec-Fetch-Dest":           "document",
    "Sec-Fetch-Mode":           "navigate",
    "Sec-Fetch-User":           "?1",
    "Upgrade-Insecure-Requests":"1",
    "Connection":               "keep-alive",
}

# Como cuando el JS del navegador llama a una API (XHR/Fetch)
HEADERS_API = {
    "User-Agent":       _UA,
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "es-AR,es;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Sec-Ch-Ua":        _SEC_CH,
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
    "Connection":       "keep-alive",
}

# Compatibilidad retroactiva para código que usa HEADERS
HEADERS = HEADERS_NAV

SCORE_MIN = 0.20

_STOPWORDS = {
    "de", "la", "el", "los", "las", "un", "una", "y", "o", "con", "para",
    "por", "en", "del", "al", "lt", "lts", "kg", "kgs", "x", "und", "uni",
    "unidad", "color", "tono",
}

_SINONIMOS = {
    "recup":    "recuplast",
    "memb":     "membrana",
    "membr":    "membrana",
    "imperm":   "impermeabilizante",
    "ext":      "exterior",
    "int":      "interior",
    "blco":     "blanco",
    "blca":     "blanca",
    "negr":     "negro",
    "antiox":   "antioxido",
    "sintet":   "sintetico",
    "diluy":    "diluyente",
    "fij":      "fijador",
    "fijad":    "fijador",
}


# ── Session factory ───────────────────────────────────────────────────────────
def _make_scraper():
    """cloudscraper con bypass de Cloudflare; fallback a requests.Session."""
    if _HAS_CLOUDSCRAPER:
        sc = _cs_mod.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        sc.headers.update(HEADERS_NAV)
        return sc
    s = requests.Session()
    s.headers.update(HEADERS_NAV)
    return s


def _warm_session(scraper, base_url: str, referer: str = "https://www.google.com.ar/",
                  timeout: int = 10) -> bool:
    """
    Visita la homepage del sitio para establecer cookies y sesión.
    Un humano hace esto antes de buscar — el servidor reconoce la sesión
    y no bloquea las requests posteriores como si fueran bots.
    Devuelve True si el calentamiento fue exitoso.
    """
    try:
        r = scraper.get(
            base_url,
            headers={
                **HEADERS_NAV,
                "Referer": referer,
                "Sec-Fetch-Site": "cross-site",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        logger.debug("Warmup %s → HTTP %d", base_url, r.status_code)
        return r.status_code < 400
    except Exception as exc:
        logger.warning("Warmup fallido %s: %s", base_url, exc)
        return False


# ── Helpers de precio y texto ─────────────────────────────────────────────────
def _clean_price(value) -> float | None:
    """Convierte string de precio AR (1.234,56) o float/int a float."""
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
        if re.match(r"^\d{1,3},\d{3}$", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    else:
        if re.match(r"^\d{1,3}(\.\d{3})+$", text):
            text = text.replace(".", "")
    try:
        v = float(text)
        return v if v > 0 else None
    except ValueError:
        return None


def _normalizar_texto(s: str) -> str:
    if not s:
        return ""
    s = _html.unescape(str(s)).lower()
    for a, b in (("á","a"),("é","e"),("í","i"),("ó","o"),("ú","u"),("ü","u"),("ñ","n")):
        s = s.replace(a, b)
    s = re.sub(r"(\d+)\s*(kgs?|kilos?)\b",    r"\1kg", s)
    s = re.sub(r"(\d+)\s*(lts?|litros?|l)\b", r"\1lt", s)
    s = re.sub(r"(\d+)\s*(grs?|gramos?)\b",   r"\1gr", s)
    s = re.sub(r"(\d+)\s*(ml|cc)\b",          r"\1ml", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> list[str]:
    out = []
    for t in _normalizar_texto(s).split():
        if t in _STOPWORDS or len(t) <= 1:
            continue
        out.append(_SINONIMOS.get(t, t))
    return out


def score_similitud(buscado: str, encontrado: str) -> float:
    tb = _tokens(buscado)
    te = _tokens(encontrado)
    if not tb or not te:
        return 0.0
    set_b, set_e = set(tb), set(te)
    comunes = set_b & set_e
    cobertura = len(comunes) / len(set_b)
    if cobertura < 1.0:
        extra = sum(
            1 for tok in (set_b - set_e)
            if any(tok in e or e in tok for e in set_e if len(tok) >= 4 and len(e) >= 4)
        )
        cobertura = min(1.0, cobertura + extra / len(set_b))
    nums_b = {t for t in set_b if any(c.isdigit() for c in t)}
    nums_e = {t for t in set_e if any(c.isdigit() for c in t)}
    match_num = len(nums_b & nums_e) / len(nums_b) if nums_b else 1.0
    fuzzy = difflib.SequenceMatcher(None, " ".join(tb), " ".join(te)).ratio()
    return round(min(1.0, 0.60 * cobertura + 0.25 * match_num + 0.15 * fuzzy), 3)


# ── Construcción de queries ───────────────────────────────────────────────────
def _cod_str(cod_proveedor) -> str | None:
    if cod_proveedor in (None, "", "nan", "None"):
        return None
    try:
        return str(int(float(cod_proveedor)))
    except (ValueError, TypeError):
        s = str(cod_proveedor).strip()
        return s or None


def _query_comercial(detalle: str, marca: str) -> str:
    toks = _tokens(detalle)
    marca_toks = _tokens(marca)
    base = [t for t in toks if t not in set(marca_toks)][:6]
    return " ".join(base + marca_toks).strip()


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[dict]:
    detalle = (detalle or "").strip()
    marca   = (marca   or "").strip()
    ref     = f"{detalle} {marca}".strip()
    cod     = _cod_str(cod_proveedor)
    q_com   = _query_comercial(detalle, marca)
    q_det   = " ".join(_tokens(detalle)[:6])
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


def _dedup_candidatos(cands: list[dict]) -> list[dict]:
    vistos, unicos = set(), []
    for c in cands:
        k = (c.get("nombre") or "").lower()[:60]
        if k and k not in vistos:
            vistos.add(k)
            unicos.append(c)
    return unicos


def _elegir_mejor(candidatos: list[dict], ref: str) -> dict | None:
    if not candidatos:
        return None
    for c in candidatos:
        c["score"] = score_similitud(ref, c.get("nombre", ""))
    candidatos.sort(key=lambda c: c["score"], reverse=True)
    for c in candidatos:
        if c["score"] >= SCORE_MIN and c.get("precio"):
            return c
    mejor = candidatos[0]
    return mejor if mejor["score"] >= SCORE_MIN and mejor.get("precio") else None


# ── Extractor JSON-LD genérico ────────────────────────────────────────────────
def _push_ld_product(node: dict, cands: list[dict]):
    if not isinstance(node, dict):
        return
    nombre = node.get("name")
    if not nombre:
        return
    link = node.get("url") or node.get("@id")
    offers = node.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    precio = (
        _clean_price(offers.get("price") or offers.get("lowPrice"))
        if isinstance(offers, dict) else None
    )
    cands.append({"nombre": nombre, "link": link, "precio": precio})


def _candidatos_de_jsonld(html: str) -> list[dict]:
    cands: list[dict] = []
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            obj = json.loads(blk.strip())
        except Exception:
            continue
        nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
        nodes = nodes if isinstance(nodes, list) else [nodes]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            tlist = t if isinstance(t, list) else [t]
            if "ItemList" in tlist:
                for el in node.get("itemListElement", []):
                    prod = el.get("item", el) if isinstance(el, dict) else {}
                    _push_ld_product(prod, cands)
            if "Product" in tlist:
                _push_ld_product(node, cands)
    return cands


# ════════════════════════════════════════════════════════════════════════════════
# SAGITARIO — WooCommerce SSR
# ════════════════════════════════════════════════════════════════════════════════

def _sagitario_candidatos_de_html(html: str) -> list[dict]:
    """
    Parser posicional para WooCommerce.

    PROBLEMA DEL PARSER ANTERIOR: usaba <li>(.*?)</li> con re.DOTALL,
    que es "lazy" y se detiene en el primer </li> anidado dentro de la card,
    cortando el bloque antes de llegar al precio.

    NUEVA ESTRATEGIA: no delimitamos por </li>. En cambio:
      1. Encontramos todos los headings de producto (h2/h6 con link a /producto/)
      2. Encontramos todos los precios en el HTML (patrón "El precio actual es:")
      3. Unimos heading ↔ precio más cercano por posición en el documento
         (igual que haría una persona leyendo la página de arriba a abajo)
    """
    cands: list[dict] = []

    # ── Paso 1: encontrar todos los headings de producto con su URL ──────────
    # WooCommerce pone el nombre del producto en un heading con clase
    # "woocommerce-loop-product__title", que contiene un <a href="/producto/...">
    headings: list[tuple[int, str, str]] = []  # (pos, url, nombre)

    # Patrón A: heading con clase WooCommerce explícita
    for m in re.finditer(
        r'woocommerce-loop-product__title[^>]*>.*?'
        r'<a\s[^>]*href="(https://pintureriasagitario\.com\.ar/producto/[^"]+)"[^>]*>'
        r'(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        nombre = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if nombre:
            headings.append((m.start(), m.group(1), nombre))

    # Patrón B: cualquier heading h2/h6 que contenga un link a /producto/
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
        logger.warning("Sagitario: no se encontraron headings de producto")
        return []

    # ── Paso 2: encontrar todos los precios con su posición ──────────────────

    # A) Texto accesible WooCommerce: "El precio actual es: $ 154.287,00."
    #    Este es el patrón más confiable porque es texto literal, no HTML.
    precios_actuales: list[tuple[int, float]] = []  # (pos, precio)
    for m in re.finditer(
        r'El precio actual es:.*?\$\s*([\d.,]+)', html, re.DOTALL
    ):
        p = _clean_price(m.group(1))
        if p and p > 100:
            precios_actuales.append((m.start(), p))

    # B) Precio de oferta: dentro de <ins>...<bdi>PRECIO</bdi>...</ins>
    precios_ins: list[tuple[int, float]] = []
    for m in re.finditer(
        r'<ins[^>]*>.*?<bdi[^>]*>(.*?)</bdi>.*?</ins>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        p = _clean_price(text)
        if p and p > 100:
            precios_ins.append((m.start(), p))

    # C) Fallback: cualquier <bdi> fuera de <del>
    #    (para productos sin descuento, el precio está en <bdi> sin <ins>/<del>)
    html_sin_del = re.sub(r"<del[^>]*>.*?</del>", "", html,
                          flags=re.DOTALL | re.IGNORECASE)
    precios_bdi: list[tuple[int, float]] = []
    for m in re.finditer(
        r'<bdi[^>]*>(.*?)</bdi>', html_sin_del, re.DOTALL | re.IGNORECASE
    ):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        p = _clean_price(text)
        if p and p > 100:
            precios_bdi.append((m.start(), p))

    # ── Paso 3: para cada heading, tomar el primer precio que aparece después ─
    # (un humano leería de arriba hacia abajo y asociaría el precio con el
    #  nombre que está justo arriba de él en la grilla)
    for i, (hpos, url, nombre) in enumerate(headings):
        # El rango es desde este heading hasta el próximo heading
        next_pos = headings[i + 1][0] if i + 1 < len(headings) else len(html)

        precio = None

        # Intentar con "El precio actual es:" primero (más preciso)
        for ppos, p in precios_actuales:
            if hpos <= ppos < next_pos:
                precio = p
                break

        # Si no: intentar con <ins>
        if precio is None:
            for ppos, p in precios_ins:
                if hpos <= ppos < next_pos:
                    precio = p
                    break

        # Si no: intentar con <bdi> genérico
        if precio is None:
            for ppos, p in precios_bdi:
                if hpos <= ppos < next_pos:
                    precio = p
                    break

        cands.append({"nombre": nombre, "link": url, "precio": precio})

    return _dedup_candidatos(cands)


def _sagitario_precio_de_pagina(scraper, url: str) -> tuple[float | None, str | None]:
    """Visita la ficha del producto y extrae precio + nombre (fallback)."""
    try:
        r = scraper.get(url, headers=HEADERS_NAV, timeout=12)
        h = r.text

        # JSON-LD del producto individual
        cands = _candidatos_de_jsonld(h)
        for c in cands:
            if c.get("precio"):
                return c["precio"], c.get("nombre")

        # Regex directos en la ficha
        precio = None
        m = re.search(r'"price"\s*:\s*["\']?([\d.,]+)', h)
        if m:
            precio = _clean_price(m.group(1))

        nombre = None
        m = re.search(r"<h1[^>]*>(.*?)</h1>", h, re.DOTALL)
        if m:
            nombre = re.sub(r"<[^>]+>", "", m.group(1)).strip()

        return precio, nombre
    except Exception as exc:
        logger.warning("Sagitario ficha error: %s", exc)
        return None, None


def scrape_sagitario(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    WooCommerce SSR — como un humano:
    1. Visita la homepage (establece cookies/sesión Cloudflare + WordPress)
    2. Realiza la búsqueda con las cookies ya establecidas
    3. Parsea el HTML usando posición (no límites de </li>)
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    # ── Paso humano 1: visitar homepage como si vinieras de Google ────────────
    _warm_session(
        scraper,
        "https://pintureriasagitario.com.ar/",
        referer="https://www.google.com.ar/search?q=pintureria+sagitario+argentina",
    )

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        try:
            # ── Paso humano 2: buscar con Referer del mismo sitio ─────────────
            url = (
                "https://pintureriasagitario.com.ar/?"
                + urllib.parse.urlencode({"s": query, "post_type": "product"})
            )
            resp = scraper.get(
                url, timeout=20,
                headers={
                    **HEADERS_NAV,
                    "Referer": "https://pintureriasagitario.com.ar/",
                    "Sec-Fetch-Site": "same-origin",
                },
            )

            if resp.status_code != 200:
                result["error"] = f"Sagitario HTTP {resp.status_code}"
                logger.warning("Sagitario HTTP %d para '%s'", resp.status_code, query)
                continue

            # Detectar página de bloqueo / captcha
            if "suspicious" in resp.url or "captcha" in resp.text.lower():
                result["error"] = "Sagitario: bloqueado por anti-bot"
                logger.warning("Sagitario: posible captcha/bloqueo")
                continue

            candidatos = _sagitario_candidatos_de_html(resp.text)
            if not candidatos:
                logger.warning("Sagitario: 0 candidatos para '%s'", query)
                continue

            # Scoring
            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            relevantes = [c for c in candidatos if c["score"] >= SCORE_MIN]

            if not relevantes:
                logger.warning(
                    "Sagitario: score insuficiente (mejor %.2f para '%s')",
                    candidatos[0]["score"], candidatos[0]["nombre"]
                )
                continue

            # Confirmar precio (visitar ficha si no vino en el listado)
            for c in relevantes[:3]:
                precio = c.get("precio")
                nombre = c["nombre"]
                if precio is None and c.get("link"):
                    precio, nom_ficha = _sagitario_precio_de_pagina(scraper, c["link"])
                    nombre = nom_ficha or nombre
                if precio:
                    result.update(
                        precio=precio, url=c.get("link") or url,
                        nombre=nombre, intento=i, score=c["score"],
                    )
                    return result

        except Exception as exc:
            logger.warning("Sagitario error intento %d: %s", i, exc)
            result["error"] = str(exc)

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados relevantes en Sagitario"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# EASY — VTEX Catalog API (pública, confirmada ✓)
# ════════════════════════════════════════════════════════════════════════════════

def _vtex_candidatos_de_json(data, base_url: str) -> list[dict]:
    """
    Parsea el JSON de la API legacy VTEX.
    Sirve para Easy y Sodimac (mismo formato VTEX).
    """
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
                offer = (
                    seller.get("commertialOffer")
                    or seller.get("commercialOffer")
                    or {}
                )
                precio = _clean_price(
                    offer.get("Price")
                    or offer.get("ListPrice")
                    or offer.get("price")
                )
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def _vtex_intelligent_search_candidatos(data, base_url: str) -> list[dict]:
    """Parsea el JSON de VTEX Intelligent Search API."""
    cands = []
    prods = []
    if isinstance(data, dict):
        prods = (
            data.get("products")
            or data.get("items")
            or data.get("data", {}).get("productSearch", {}).get("products", [])
            or []
        )
    elif isinstance(data, list):
        prods = data

    for prod in prods[:10]:
        if not isinstance(prod, dict):
            continue
        nombre = (
            prod.get("productName") or prod.get("name")
            or prod.get("productTitle") or prod.get("title")
        )
        link_text = prod.get("linkText") or prod.get("slug") or ""
        link = f"{base_url}/{link_text}/p" if link_text else None
        precio = None
        pr = prod.get("priceRange", {})
        if pr:
            precio = _clean_price(
                pr.get("sellingPrice", {}).get("lowPrice")
                or pr.get("listPrice", {}).get("lowPrice")
            )
        if precio is None:
            for item in prod.get("items", [])[:1]:
                for seller in item.get("sellers", [])[:1]:
                    offer = seller.get("commertialOffer") or {}
                    precio = _clean_price(
                        offer.get("Price") or offer.get("ListPrice")
                    )
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def scrape_easy(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    VTEX — como un humano:
    1. Visita easy.com.ar (VTEX establece cookies vtex_session, vtex_segment, etc.)
    2. Llama a la API de catálogo con esas cookies → JSON con precios
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    # ── Paso humano 1: visitar homepage para obtener cookies VTEX ────────────
    _warm_session(scraper, "https://www.easy.com.ar/",
                  referer="https://www.google.com.ar/search?q=easy+materiales+construccion")

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        # Usar solo las primeras palabras significativas para la búsqueda en path
        q_palabras = " ".join(query.split()[:4])
        q_enc      = urllib.parse.quote(q_palabras)
        candidatos: list[dict] = []

        # También probar con solo la primera palabra (VTEX path-search funciona mejor así)
        q_corta_enc = urllib.parse.quote(query.split()[0]) if query.split() else q_enc

        endpoints = [
            # API Catalog legacy (confirmada vía web_fetch — devuelve JSON con precios)
            (
                f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_enc}"
                f"?_from=0&_to=9",
                "application/json",
            ),
            # Con solo la primera keyword (más específico para VTEX)
            (
                f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_corta_enc}"
                f"?_from=0&_to=9",
                "application/json",
            ),
            # VTEX Intelligent Search
            (
                f"https://www.easy.com.ar/_v/api/intelligent-search/product_search/trade-policy/1"
                f"?query={q_enc}&count=10&sort=score_desc",
                "application/json",
            ),
        ]

        for api_url, accept in endpoints:
            try:
                resp = scraper.get(
                    api_url, timeout=20,
                    headers={
                        **HEADERS_API,
                        "Accept": accept,
                        "Referer": "https://www.easy.com.ar/",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                if resp.status_code != 200:
                    logger.debug("Easy API HTTP %d: %s", resp.status_code, api_url[:80])
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue  # Devolvió HTML en vez de JSON (sin cookies)

                if isinstance(data, list):
                    candidatos = _vtex_candidatos_de_json(data, "https://www.easy.com.ar")
                else:
                    candidatos = _vtex_intelligent_search_candidatos(
                        data, "https://www.easy.com.ar"
                    )
                if candidatos:
                    break
            except Exception as exc:
                logger.warning("Easy endpoint error: %s", exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(
                precio=mejor["precio"], url=mejor["link"],
                nombre=mejor["nombre"], intento=i, score=mejor["score"],
            )
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados en Easy (VTEX puede requerir sesión)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# REX — Magento 2 (CSR/React) → GraphQL + REST
# ════════════════════════════════════════════════════════════════════════════════

def _rex_graphql_candidatos(scraper, query: str) -> list[dict]:
    gql_query = {
        "query": """
        {
          products(search: "%s", pageSize: 10) {
            items {
              name
              url_key
              price_range {
                minimum_price {
                  regular_price { value currency }
                  final_price   { value currency }
                }
              }
            }
          }
        }
        """ % query.replace('"', '\\"')
    }
    try:
        resp = scraper.post(
            "https://www.somosrex.com/graphql",
            json=gql_query,
            headers={
                **HEADERS_API,
                "Content-Type": "application/json",
                "Referer": "https://www.somosrex.com/",
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data.get("data", {}).get("products", {}).get("items", [])
        cands = []
        for prod in items[:10]:
            nombre = prod.get("name")
            url_key = prod.get("url_key", "")
            link = f"https://www.somosrex.com/{url_key}.html" if url_key else None
            pr = prod.get("price_range", {}).get("minimum_price", {})
            precio = _clean_price(
                pr.get("final_price", {}).get("value")
                or pr.get("regular_price", {}).get("value")
            )
            if nombre:
                cands.append({"nombre": nombre, "link": link, "precio": precio})
        return cands
    except Exception as exc:
        logger.warning("Rex GraphQL error: %s", exc)
        return []


def _rex_rest_candidatos(scraper, query: str) -> list[dict]:
    q_enc = urllib.parse.quote(f"%{query}%")
    try:
        url = (
            f"https://www.somosrex.com/rest/all/V1/products"
            f"?searchCriteria[filterGroups][0][filters][0][field]=name"
            f"&searchCriteria[filterGroups][0][filters][0][value]={q_enc}"
            f"&searchCriteria[filterGroups][0][filters][0][condition_type]=like"
            f"&searchCriteria[pageSize]=10"
            f"&fields=items[name,custom_attributes,price,extension_attributes,url_key]"
        )
        resp = scraper.get(
            url, timeout=20,
            headers={**HEADERS_API, "Referer": "https://www.somosrex.com/"},
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        cands = []
        for prod in items[:10]:
            nombre = prod.get("name")
            url_key = prod.get("url_key") or ""
            link = f"https://www.somosrex.com/{url_key}.html" if url_key else None
            precio = _clean_price(prod.get("price"))
            if nombre:
                cands.append({"nombre": nombre, "link": link, "precio": precio})
        return cands
    except Exception as exc:
        logger.warning("Rex REST error: %s", exc)
        return []


def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Magento 2 — GraphQL POST primero, REST fallback. (Ya funciona ✓)"""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])

        candidatos = _rex_graphql_candidatos(scraper, q_corta)
        if not candidatos:
            candidatos = _rex_rest_candidatos(scraper, q_corta)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(
                precio=mejor["precio"], url=mejor["link"],
                nombre=mejor["nombre"], intento=i, score=mejor["score"],
            )
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados en Rex"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# MERCADO LIBRE — API + HTML (warmup de sesión)
# ════════════════════════════════════════════════════════════════════════════════

def _ml_candidatos_de_html(html: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)

    if len(cands) < 3:
        for blk in re.split(
            r'<li[^>]*class="[^"]*ui-search-layout__item[^"]*"', html
        )[1:]:
            t = (
                re.search(r'class="[^"]*ui-search-item__title[^"]*"[^>]*>(.*?)<', blk, re.DOTALL)
                or re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            )
            href  = re.search(r'href="(https://[^"]*mercadolibre[^"]*)"', blk)
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

    return _dedup_candidatos(cands)


def scrape_ml(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    MercadoLibre — como un humano:
    1. Visita la homepage de ML (establece cookies de sesión)
    2. Intenta la API oficial con esas cookies
    3. Si falla la API, intenta el HTML de listado
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    # ── Paso humano 1: visitar ML para establecer sesión ──────────────────────
    _warm_session(scraper, "https://www.mercadolibre.com.ar/",
                  referer="https://www.google.com.ar/search?q=mercadolibre+argentina")

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:3])
        candidatos: list[dict] = []

        # A) API oficial
        try:
            api_url = (
                f"https://api.mercadolibre.com/sites/MLA/search"
                f"?q={urllib.parse.quote(q_corta)}&limit=10"
            )
            resp = scraper.get(
                api_url, timeout=12,
                headers={
                    **HEADERS_API,
                    "Referer": "https://www.mercadolibre.com.ar/",
                },
            )
            if resp.status_code == 200:
                for prod in resp.json().get("results", [])[:10]:
                    candidatos.append({
                        "nombre": prod.get("title"),
                        "link":   prod.get("permalink"),
                        "precio": _clean_price(prod.get("price")),
                    })
        except Exception as exc:
            logger.warning("ML API error: %s", exc)

        # B) Listado HTML (paso humano: navegar a la URL de listado)
        if not candidatos:
            for url_listado in [
                f"https://listado.mercadolibre.com.ar/{urllib.parse.quote(q_corta.replace(' ', '-'))}",
                f"https://www.mercadolibre.com.ar/buscar?q={urllib.parse.quote(q_corta)}",
            ]:
                try:
                    resp = scraper.get(
                        url_listado, timeout=15,
                        headers={
                            **HEADERS_NAV,
                            "Referer": "https://www.mercadolibre.com.ar/",
                            "Sec-Fetch-Site": "same-origin",
                        },
                    )
                    if resp.status_code == 200 and "suspicious-traffic" not in resp.url:
                        candidatos = _ml_candidatos_de_html(resp.text)
                        if candidatos:
                            break
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
        mejor   = top[0]
        result.update(
            precio=mediana, url=mejor.get("link"),
            nombre=mejor.get("nombre"), intento=i, score=mejor["score"],
        )
        break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados en ML (bloqueado por anti-bot)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# SODIMAC — VTEX (mismo enfoque que Easy)
# ════════════════════════════════════════════════════════════════════════════════

def scrape_sodimac(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Sodimac Argentina — VTEX, como un humano:
    1. Visita la homepage (establece cookies VTEX)
    2. Llama a la API de catálogo/intelligent-search con esas cookies
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    # ── Paso humano 1: visitar homepage para obtener cookies VTEX ────────────
    _warm_session(scraper, "https://www.sodimac.com.ar/",
                  referer="https://www.google.com.ar/search?q=sodimac+argentina+materiales")

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_palabras    = " ".join(query.split()[:4])
        q_enc         = urllib.parse.quote(q_palabras)
        q_corta_enc   = urllib.parse.quote(query.split()[0]) if query.split() else q_enc
        candidatos: list[dict] = []

        endpoints = [
            # VTEX Catalog API legacy (mismo formato que Easy, probado)
            (
                f"https://www.sodimac.com.ar/api/catalog_system/pub/products/search/{q_enc}"
                f"?_from=0&_to=9",
                "list",
            ),
            (
                f"https://www.sodimac.com.ar/api/catalog_system/pub/products/search/{q_corta_enc}"
                f"?_from=0&_to=9",
                "list",
            ),
            # VTEX Intelligent Search
            (
                f"https://www.sodimac.com.ar/_v/api/intelligent-search/product_search/trade-policy/1"
                f"?query={q_enc}&count=10&sort=score_desc",
                "is",
            ),
            # Sodimac search antiguo
            (
                f"https://www.sodimac.com.ar/sodimac-ar/search?Ntt={q_enc}&format=json",
                "is",
            ),
        ]

        for api_url, fmt in endpoints:
            try:
                resp = scraper.get(
                    api_url, timeout=20,
                    headers={
                        **HEADERS_API,
                        "Referer": "https://www.sodimac.com.ar/",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                if resp.status_code != 200:
                    logger.debug("Sodimac HTTP %d: %s", resp.status_code, api_url[:80])
                    continue
                try:
                    data = resp.json()
                except Exception:
                    continue  # Devolvió HTML (sin cookies válidas)

                if fmt == "list":
                    candidatos = _vtex_candidatos_de_json(data, "https://www.sodimac.com.ar")
                else:
                    candidatos = _vtex_intelligent_search_candidatos(
                        data, "https://www.sodimac.com.ar"
                    )
                if candidatos:
                    break
            except Exception as exc:
                logger.warning("Sodimac endpoint error: %s", exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(
                precio=mejor["precio"], url=mejor["link"],
                nombre=mejor["nombre"], intento=i, score=mejor["score"],
            )
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados en Sodimac (VTEX puede requerir sesión)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# Orquestador principal
# ════════════════════════════════════════════════════════════════════════════════

def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """Ejecuta los 5 scrapers en paralelo."""
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers_map = {
        "rex":       scrape_rex,
        "sagitario": scrape_sagitario,
        "ml":        scrape_ml,
        "sodimac":   scrape_sodimac,
        "easy":      scrape_easy,
    }
    resultados: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {name: ex.submit(fn, *args) for name, fn in scrapers_map.items()}
        for name, fut in futures.items():
            try:
                resultados[name] = fut.result(timeout=45)
            except Exception as exc:
                r = _empty_result()
                r["error"] = str(exc)
                resultados[name] = r
    return resultados
