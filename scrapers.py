"""
scrapers.py — Búsqueda de precios en Rex, Sagitario y MercadoLibre.

Usa SOLO `requests` (sin Playwright/browser) para compatibilidad con
Streamlit Cloud. Cada scraper:

  1. Construye una cascada de queries (detalle+marca → detalle → código).
  2. Por cada query trae VARIOS candidatos.
  3. Puntúa la relevancia de cada candidato contra lo buscado.
  4. Descarta candidatos con score por debajo del umbral (evita devolver
     "Buzo Coronados" cuando se buscaba "RECUP TECHOS BLANCO SINTEPLAST").
  5. Devuelve el mejor candidato relevante.

Cada scraper devuelve:
    {
        "precio":  float | None,
        "url":     str | None,
        "nombre":  str | None,
        "intento": int | None,
        "score":   float | None,   # 0..1, qué tan relevante fue el match
        "error":   str | None,
    }

Notas de plataforma (junio 2026):
  - MercadoLibre desactivó la búsqueda anónima de /sites/MLA/search (devuelve
    403). Por eso ML se scrapea desde el HTML público de listado.mercadolibre.
  - Rex corre sobre VTEX: se usa Intelligent Search + catalog API + fallback
    al __STATE__ embebido en el HTML.
  - Sagitario es una tienda estándar (WooCommerce/Tiendanube): se parsea el
    listado de búsqueda, se puntúa cada producto y se confirma en su página.
"""

import re
import json
import html as _html
import logging
import difflib
import concurrent.futures
import urllib.parse

import requests

logger = logging.getLogger(__name__)

ML_SITE_ID = "MLA"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,es-419;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Score mínimo para aceptar un candidato como "el mismo producto".
# Por debajo de esto se descarta para evitar falsos positivos.
SCORE_MIN = 0.34

# Palabras de relleno que no aportan a la identificación del producto.
_STOPWORDS = {
    "de", "la", "el", "los", "las", "un", "una", "y", "o", "con", "para",
    "por", "en", "del", "al", "lt", "lts", "kg", "kgs", "x", "und", "uni",
    "unidad", "color", "tono",
}

# Sinónimos / normalizaciones de nombres internos → nombre comercial.
# Mapea tokens del detalle técnico a como aparecen en los sitios públicos.
_SINONIMOS = {
    "recup": "recuplast",
    "memb": "membrana",
    "membr": "membrana",
    "imperm": "impermeabilizante",
    "latex": "latex",
    "ext": "exterior",
    "int": "interior",
    "blco": "blanco",
    "blca": "blanca",
    "negr": "negro",
    "antiox": "antioxido",
    "sintet": "sintetico",
    "esmalte": "esmalte",
    "diluy": "diluyente",
    "aguarras": "aguarras",
    "fij": "fijador",
    "fijad": "fijador",
}


# ── Helpers de precio / texto ─────────────────────────────────────────────────────
def _clean_price(value) -> float | None:
    """Convierte string o número a float de precio (formato AR: 1.234,56)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    text = re.sub(r"[^\d,.]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        # 1.234,56 -> 1234.56
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        # 1234,56 -> 1234.56  (pero 1,234 podría ser miles; asumimos decimal AR)
        # Si hay exactamente 3 dígitos tras la coma, es separador de miles.
        if re.match(r"^\d{1,3},\d{3}$", text):
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    else:
        # Solo puntos: si hay un punto con 3 dígitos detrás, es miles.
        if re.match(r"^\d{1,3}(\.\d{3})+$", text):
            text = text.replace(".", "")
    try:
        v = float(text)
        return v if v > 0 else None
    except ValueError:
        return None


def _normalizar_texto(s: str) -> str:
    """Minúsculas, sin acentos, sin signos, con unidades normalizadas."""
    if not s:
        return ""
    s = _html.unescape(str(s)).lower()
    # quitar acentos básicos
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    # normalizar unidades: "20 kgs" / "20 kg." / "20kgs" -> "20kg"
    s = re.sub(r"(\d+)\s*(kgs?|kilos?)\b", r"\1kg", s)
    s = re.sub(r"(\d+)\s*(lts?|litros?|l)\b", r"\1lt", s)
    s = re.sub(r"(\d+)\s*(grs?|gramos?)\b", r"\1gr", s)
    s = re.sub(r"(\d+)\s*(ml|cc)\b", r"\1ml", s)
    # quitar todo lo que no sea alfanumérico
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> list[str]:
    """Tokens significativos (sin stopwords, con sinónimos aplicados)."""
    out = []
    for t in _normalizar_texto(s).split():
        if t in _STOPWORDS or len(t) <= 1:
            continue
        out.append(_SINONIMOS.get(t, t))
    return out


def score_similitud(buscado: str, encontrado: str) -> float:
    """
    Score 0..1 de cuán parecido es `encontrado` a lo `buscado`.

    Combina:
      - Cobertura de tokens clave de la búsqueda presentes en el resultado.
      - Match de números/unidades (ej "20kg") que son muy discriminantes.
      - Similitud difusa global (difflib) como desempate.

    Devuelve 0 si no comparten ningún token relevante.
    """
    tb = _tokens(buscado)
    te = _tokens(encontrado)
    if not tb or not te:
        return 0.0

    set_b, set_e = set(tb), set(te)

    # 1) Cobertura de tokens (cuántas palabras de la búsqueda están en el match)
    comunes = set_b & set_e
    cobertura = len(comunes) / len(set_b)

    # Match parcial (substring) para tokens que no calzan exacto:
    # ej "recuplast" en búsqueda vs "recuplast" en título largo.
    if cobertura < 1.0:
        extra = 0
        faltantes = set_b - set_e
        for tok in faltantes:
            if any(tok in e or e in tok for e in set_e if len(tok) >= 4 and len(e) >= 4):
                extra += 1
        cobertura = min(1.0, cobertura + extra / len(set_b))

    # 2) Bonus/penalización por números y unidades (muy discriminantes)
    nums_b = {t for t in set_b if any(c.isdigit() for c in t)}
    nums_e = {t for t in set_e if any(c.isdigit() for c in t)}
    if nums_b:
        match_num = len(nums_b & nums_e) / len(nums_b)
    else:
        match_num = 1.0  # no había números que verificar

    # 3) Similitud difusa global
    fuzzy = difflib.SequenceMatcher(
        None, " ".join(tb), " ".join(te)
    ).ratio()

    # Ponderación: la cobertura manda, los números afinan, fuzzy desempata.
    score = 0.60 * cobertura + 0.25 * match_num + 0.15 * fuzzy
    return round(min(1.0, score), 3)


# ── Construcción de queries ───────────────────────────────────────────────────────
def _cod_str(cod_proveedor) -> str | None:
    if cod_proveedor in (None, "", "nan", "None"):
        return None
    try:
        return str(int(float(cod_proveedor)))
    except (ValueError, TypeError):
        s = str(cod_proveedor).strip()
        return s or None


def _query_comercial(detalle: str, marca: str) -> str:
    """
    Convierte el detalle técnico interno en una query 'comercial' apta para
    buscadores públicos: aplica sinónimos, normaliza unidades y arma una frase
    corta con las palabras más identificatorias + la marca.
    """
    toks = _tokens(detalle)
    marca_toks = _tokens(marca)
    # Evitar duplicar la marca si ya está en el detalle.
    base = [t for t in toks if t not in set(marca_toks)]
    # Limitar a las primeras ~6 palabras clave para no sobre-restringir.
    base = base[:6]
    full = base + marca_toks
    return " ".join(full).strip()


def _build_queries(detalle: str, marca: str, cod_proveedor) -> list[dict]:
    """
    Devuelve lista ordenada de queries con metadatos:
        {"q": <texto>, "ref": <texto de referencia para scoring>}

    `ref` siempre incluye detalle+marca para puntuar relevancia, aunque la
    query enviada al sitio sea más corta o sea el código.
    """
    detalle = (detalle or "").strip()
    marca = (marca or "").strip()
    ref = f"{detalle} {marca}".strip()
    cod = _cod_str(cod_proveedor)

    q_comercial = _query_comercial(detalle, marca)
    q_solo_det = " ".join(_tokens(detalle)[:6])

    candidatos = [
        q_comercial,            # detalle (comercial) + marca
        q_solo_det,             # solo detalle
        cod,                    # código proveedor
    ]
    queries, seen = [], set()
    for q in candidatos:
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


# ── Rex (VTEX) ────────────────────────────────────────────────────────────────────
def _rex_extraer_precio(prod: dict) -> float | None:
    """Extrae el primer precio válido de un producto VTEX."""
    for item in prod.get("items", []):
        for seller in item.get("sellers", []):
            oferta = seller.get("commertialOffer") or seller.get("commercialOffer") or {}
            p = oferta.get("Price") or oferta.get("spotPrice") or oferta.get("ListPrice")
            precio = _clean_price(p)
            if precio:
                return precio
    # Algunos payloads de intelligent-search traen priceRange.
    pr = prod.get("priceRange", {}).get("sellingPrice", {}).get("lowPrice")
    return _clean_price(pr)


def _rex_candidatos_de_json(data) -> list[dict]:
    """Normaliza distintas formas de respuesta VTEX a [{nombre,link,precio}]."""
    prods = []
    if isinstance(data, list):
        prods = data
    elif isinstance(data, dict):
        prods = (
            data.get("products")
            or data.get("data", {}).get("productSearch", {}).get("products")
            or []
        )
    cands = []
    for prod in prods[:8]:
        nombre = prod.get("productName") or prod.get("name")
        link_text = prod.get("linkText", "")
        link = f"https://www.rex.com.ar/{link_text}/p" if link_text else "https://www.rex.com.ar"
        precio = _rex_extraer_precio(prod)
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Busca en Rex (VTEX) y devuelve el candidato más relevante con precio."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result = _empty_result()

    session = requests.Session()
    session.headers.update({**HEADERS, "Accept": "application/json, text/plain, */*"})
    session.headers["Referer"] = "https://www.rex.com.ar/"

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_enc = urllib.parse.quote(query)
        candidatos: list[dict] = []

        # Endpoints VTEX, en orden de preferencia.
        endpoints = [
            # Intelligent Search (IO) — el más fiable hoy. tradePolicy=1 por defecto.
            f"https://www.rex.com.ar/api/io/_v/api/intelligent-search/product_search/?query={q_enc}&count=8&page=1&locale=es-AR",
            f"https://www.rex.com.ar/_v/api/intelligent-search/product_search/v3?query={q_enc}&count=8&locale=es-AR&hideUnavailableItems=false",
            # Catalog API clásica.
            f"https://www.rex.com.ar/api/catalog_system/pub/products/search?ft={q_enc}&_from=0&_to=7",
        ]
        for ep in endpoints:
            try:
                resp = session.get(ep, timeout=15)
                if resp.status_code != 200 or not resp.text.strip():
                    continue
                data = resp.json()
                candidatos = _rex_candidatos_de_json(data)
                if candidatos:
                    break
            except Exception as exc:
                logger.warning("Rex endpoint error (%s): %s", ep[:60], exc)

        # Fallback: HTML con __STATE__ embebido (VTEX render).
        if not candidatos:
            try:
                url_html = f"https://www.rex.com.ar/{q_enc}?map=ft"
                resp = session.get(url_html, timeout=15,
                                   headers={**session.headers, "Accept": HEADERS["Accept"]})
                html = resp.text
                # Nombres y precios sueltos del __STATE__.
                nombres = re.findall(r'"productName"\s*:\s*"([^"]+)"', html)
                precios = re.findall(r'"Price"\s*:\s*([\d.]+)', html)
                links = re.findall(r'"linkText"\s*:\s*"([^"]+)"', html)
                for idx, nom in enumerate(nombres[:8]):
                    candidatos.append({
                        "nombre": _html.unescape(nom),
                        "link": (f"https://www.rex.com.ar/{links[idx]}/p"
                                 if idx < len(links) else url_html),
                        "precio": _clean_price(precios[idx]) if idx < len(precios) else None,
                    })
            except Exception as exc:
                logger.warning("Rex HTML fallback error: %s", exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                         nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Rex tras {len(queries)} intentos"
    return result


# ── Sagitario (tienda estándar: WooCommerce / Tiendanube) ─────────────────────────
def _sagitario_candidatos_de_html(html: str) -> list[dict]:
    """
    Extrae candidatos de la página de resultados de Sagitario.
    Combina JSON-LD (ItemList/Product) con parsing de tarjetas de producto.
    """
    cands: list[dict] = []

    # 1) JSON-LD: puede traer ItemList con varios productos.
    ld_blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    )
    for blk in ld_blocks:
        try:
            obj = json.loads(blk.strip())
        except Exception:
            continue
        nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if t == "ItemList":
                for el in node.get("itemListElement", []):
                    prod = el.get("item", el) if isinstance(el, dict) else {}
                    _push_ld_product(prod, cands)
            elif t == "Product" or (isinstance(t, list) and "Product" in t):
                _push_ld_product(node, cands)

    # 2) Parsing de tarjetas (WooCommerce <li class="product"> / Tiendanube).
    if len(cands) < 2:
        # WooCommerce: bloques <li class="...product..."> con <a href> y precio.
        for m in re.finditer(
            r'<li[^>]*class="[^"]*\bproduct\b[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            blk = m.group(1)
            href = re.search(r'<a[^>]+href="([^"]+)"', blk)
            nom = re.search(r'<h2[^>]*>(.*?)</h2>', blk, re.DOTALL) \
                or re.search(r'woocommerce-loop-product__title[^>]*>(.*?)<', blk, re.DOTALL)
            price = re.search(r'class="[^"]*price[^"]*"[^>]*>.*?([\d.][\d.,]*)', blk, re.DOTALL)
            if href and nom:
                cands.append({
                    "nombre": re.sub(r"<[^>]+>", "", nom.group(1)).strip(),
                    "link": _html.unescape(href.group(1)),
                    "precio": _clean_price(price.group(1)) if price else None,
                })

    # 3) Tiendanube: links de producto en data-store o anchors a /productos/.
    if not cands:
        for m in re.finditer(
            r'<a[^>]+href="([^"]*/(?:productos|product)/[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            nombre = re.sub(r"<[^>]+>", " ", m.group(2))
            nombre = re.sub(r"\s+", " ", nombre).strip()
            if nombre and len(nombre) > 3:
                link = m.group(1)
                if link.startswith("/"):
                    link = "https://www.sagitario.com.ar" + link
                cands.append({"nombre": nombre, "link": _html.unescape(link), "precio": None})

    # de-duplicar por link
    vistos, unicos = set(), []
    for c in cands:
        k = c.get("link")
        if k and k not in vistos:
            vistos.add(k)
            unicos.append(c)
    return unicos


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
    precio = _clean_price(offers.get("price") or offers.get("lowPrice")) if isinstance(offers, dict) else None
    cands.append({"nombre": nombre, "link": link, "precio": precio})


def _sagitario_precio_de_pagina(session, url: str) -> tuple[float | None, str | None]:
    """Abre la página de producto y confirma precio + nombre."""
    try:
        r = session.get(url, timeout=12)
        h = r.text
        precio = None
        nombre = None
        # JSON-LD del producto.
        for blk in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            h, re.DOTALL | re.IGNORECASE,
        ):
            try:
                obj = json.loads(blk.strip())
            except Exception:
                continue
            nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
            for node in nodes if isinstance(nodes, list) else [nodes]:
                if isinstance(node, dict) and (node.get("@type") == "Product"
                        or (isinstance(node.get("@type"), list) and "Product" in node["@type"])):
                    nombre = node.get("name") or nombre
                    offers = node.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    if isinstance(offers, dict):
                        precio = _clean_price(offers.get("price") or offers.get("lowPrice")) or precio
        # Fallbacks regex.
        if precio is None:
            m = re.search(r'"price"\s*:\s*["\']?([\d.,]+)', h)
            if m:
                precio = _clean_price(m.group(1))
        if nombre is None:
            m = re.search(r'<h1[^>]*>(.*?)</h1>', h, re.DOTALL)
            if m:
                nombre = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return precio, nombre
    except Exception as exc:
        logger.warning("Sagitario página producto error: %s", exc)
        return None, None


def scrape_sagitario(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Busca en Sagitario, puntúa los candidatos y confirma en la ficha."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result = _empty_result()

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        try:
            url_busqueda = (
                "https://www.sagitario.com.ar/?"
                + urllib.parse.urlencode({"s": query, "post_type": "product"})
            )
            resp = session.get(url_busqueda, timeout=15)
            resp.raise_for_status()
            candidatos = _sagitario_candidatos_de_html(resp.text)
            if not candidatos:
                continue

            # Puntuar y ordenar; quedarnos con los mejores para confirmar precio.
            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            candidatos = [c for c in candidatos if c["score"] >= SCORE_MIN]
            if not candidatos:
                continue

            # Confirmar precio del mejor candidato (visitando ficha si hace falta).
            elegido = None
            for c in candidatos[:3]:
                precio = c.get("precio")
                nombre = c.get("nombre")
                if precio is None and c.get("link"):
                    precio, nombre_pg = _sagitario_precio_de_pagina(session, c["link"])
                    nombre = nombre_pg or nombre
                if precio:
                    elegido = {"precio": precio, "link": c.get("link") or url_busqueda,
                               "nombre": nombre, "score": c["score"]}
                    break
            if elegido:
                result.update(precio=elegido["precio"], url=elegido["link"],
                             nombre=elegido["nombre"], intento=i, score=elegido["score"])
                break

        except Exception as exc:
            logger.warning("Sagitario error intento %d: %s", i, exc)

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Sagitario tras {len(queries)} intentos"
    return result


# ── MercadoLibre (scraping HTML — la API anónima fue desactivada) ──────────────────
def _ml_candidatos_de_html(html: str) -> list[dict]:
    """Extrae candidatos del HTML de listado.mercadolibre.com.ar."""
    cands: list[dict] = []

    # 1) JSON-LD ItemList (ML lo incluye en las páginas de resultados).
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            obj = json.loads(blk.strip())
        except Exception:
            continue
        nodes = obj if isinstance(obj, list) else [obj]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "ItemList":
                for el in node.get("itemListElement", []):
                    prod = el.get("item", {}) if isinstance(el, dict) else {}
                    nombre = prod.get("name")
                    link = prod.get("url") or (el.get("url") if isinstance(el, dict) else None)
                    offers = prod.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    precio = _clean_price(offers.get("price")) if isinstance(offers, dict) else None
                    if nombre:
                        cands.append({"nombre": nombre, "link": link, "precio": precio})

    # 2) Parsing de tarjetas de resultado (estructura ui-search-*).
    #    Capturamos pares título/precio/link por bloque de resultado.
    if len(cands) < 3:
        for blk in re.split(r'<li[^>]*class="[^"]*ui-search-layout__item[^"]*"', html)[1:]:
            t = re.search(r'class="[^"]*ui-search-item__title[^"]*"[^>]*>(.*?)<', blk, re.DOTALL) \
                or re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            href = re.search(r'href="(https://[^"]*mercadolibre[^"]*)"', blk)
            price = re.search(
                r'andes-money-amount__fraction[^>]*>([\d.\s]+)<', blk
            ) or re.search(r'price-tag-fraction[^>]*>([\d.\s]+)<', blk)
            if t and href:
                nombre = re.sub(r"<[^>]+>", "", t.group(1)).strip()
                if nombre:
                    cands.append({
                        "nombre": _html.unescape(nombre),
                        "link": href.group(1).split("#")[0],
                        "precio": _clean_price(price.group(1)) if price else None,
                    })

    # de-dup por nombre
    vistos, unicos = set(), []
    for c in cands:
        k = (c.get("nombre") or "").lower()[:60]
        if k and k not in vistos:
            vistos.add(k)
            unicos.append(c)
    return unicos


def scrape_ml(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    Scrapea el HTML público de MercadoLibre (la API /sites/MLA/search anónima
    devuelve 403 desde 2024/2025). Usa queries comerciales simplificadas.
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result = _empty_result()

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        try:
            # ML usa guiones en el path: "recuplast techos blanco" -> "recuplast-techos-blanco"
            slug = re.sub(r"\s+", "-", query.strip())
            url = f"https://listado.mercadolibre.com.ar/{urllib.parse.quote(slug)}"
            resp = session.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            candidatos = _ml_candidatos_de_html(resp.text)
            if not candidatos:
                continue

            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            relevantes = [c for c in candidatos if c["score"] >= SCORE_MIN and c.get("precio")]
            if not relevantes:
                continue

            # Precio: mediana de los relevantes top (evita outliers tipo combos/lotes).
            top = relevantes[:5]
            precios = sorted(c["precio"] for c in top)
            mediana = precios[len(precios) // 2]
            mejor = top[0]

            result.update(precio=mediana, url=mejor.get("link"),
                         nombre=mejor.get("nombre"), intento=i, score=mejor["score"])
            break

        except Exception as exc:
            logger.warning("ML error intento %d: %s", i, exc)

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en ML tras {len(queries)} intentos"
    return result


# ── Selección del mejor candidato (genérico) ──────────────────────────────────────
def _elegir_mejor(candidatos: list[dict], ref: str) -> dict | None:
    """
    De una lista [{nombre, link, precio}], puntúa cada uno vs `ref` y devuelve
    el de mayor score que tenga precio, siempre que supere SCORE_MIN.
    """
    if not candidatos:
        return None
    for c in candidatos:
        c["score"] = score_similitud(ref, c.get("nombre", ""))
    candidatos.sort(key=lambda c: c["score"], reverse=True)
    for c in candidatos:
        if c["score"] >= SCORE_MIN and c.get("precio"):
            return c
    # Si el mejor supera el umbral pero no trae precio, igual lo reportamos sin precio
    mejor = candidatos[0]
    if mejor["score"] >= SCORE_MIN:
        return mejor if mejor.get("precio") else None
    return None


# ── Función principal ─────────────────────────────────────────────────────────────
def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """Ejecuta los 3 scrapers en paralelo."""
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers = {"rex": scrape_rex, "sagitario": scrape_sagitario, "ml": scrape_ml}
    resultados: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {name: ex.submit(fn, *args) for name, fn in scrapers.items()}
        for name, fut in futures.items():
            try:
                resultados[name] = fut.result(timeout=35)
            except Exception as exc:
                r = _empty_result()
                r["error"] = str(exc)
                resultados[name] = r
    return resultados
