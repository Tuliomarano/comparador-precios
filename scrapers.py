"""
scrapers.py — Búsqueda de precios en Rex, Sagitario, ML, Sodimac y Easy.

Tecnologías detectadas (jun 2026):
  Rex        → somosrex.com              → Magento 2 (CSR/React) → GraphQL POST + REST fallback
  Sagitario  → pintureriasagitario.com.ar→ WooCommerce SSR       → HTML parser (<ins><bdi>)
  ML         → api.mercadolibre.com      → API pública + HTML     → API + listado fallback
  Sodimac    → sodimac.com.ar            → VTEX/CSR              → Intelligent Search API
  Easy       → easy.com.ar              → VTEX CSR              → Catalog API JSON (PROBADO ✓)
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


# ── Session factory ───────────────────────────────────────────────────────────────
def _make_scraper():
    """cloudscraper con bypass de Cloudflare; fallback a requests.Session."""
    if _HAS_CLOUDSCRAPER:
        sc = _cs_mod.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        sc.headers.update(HEADERS)
        return sc
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ── Helpers de precio y texto ─────────────────────────────────────────────────────
def _clean_price(value) -> float | None:
    """Convierte string de precio AR (1.234,56) a float."""
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


# ── Extractor JSON-LD genérico ────────────────────────────────────────────────────
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
# SAGITARIO — WooCommerce SSR (confirmado: devuelve HTML completo)
# ════════════════════════════════════════════════════════════════════════════════
def _woo_precio_de_bloque(blk: str) -> float | None:
    """
    Extrae el precio actual de un bloque de tarjeta WooCommerce.
    En WooCommerce: <del> = precio tachado, <ins> = precio de oferta.
    Si no hay oferta, el precio está en <bdi> sin <del>/<ins>.
    """
    # 1) Precio de oferta → dentro de <ins>
    ins_m = re.search(r"<ins[^>]*>(.*?)</ins>", blk, re.DOTALL | re.IGNORECASE)
    if ins_m:
        bdi_m = re.search(r"<bdi[^>]*>(.*?)</bdi>", ins_m.group(1), re.DOTALL | re.IGNORECASE)
        if bdi_m:
            text = re.sub(r"<[^>]+>", "", bdi_m.group(1))
            precio = _clean_price(text)
            if precio and precio > 100:
                return precio

    # 2) Precio regular (sin descuento): primer <bdi> fuera de <del>
    blk_sin_del = re.sub(r"<del[^>]*>.*?</del>", "", blk, flags=re.DOTALL | re.IGNORECASE)
    bdi_m = re.search(r"<bdi[^>]*>(.*?)</bdi>", blk_sin_del, re.DOTALL | re.IGNORECASE)
    if bdi_m:
        text = re.sub(r"<[^>]+>", "", bdi_m.group(1))
        precio = _clean_price(text)
        if precio and precio > 100:
            return precio

    # 3) Fallback: el precio literal en formato AR "$ 154.287,00" que WooCommerce repite
    #    como texto accesible ("El precio actual es: $ 154.287,00")
    m = re.search(r"El precio actual es:.*?\$\s*([\d.,]+)", blk, re.DOTALL)
    if m:
        return _clean_price(m.group(1))

    return None


def _sagitario_candidatos_de_html(html: str) -> list[dict]:
    cands: list[dict] = []

    # WooCommerce product cards: <li class="...product...">
    for m in re.finditer(
        r"<li[^>]+class=\"[^\"]*\bproduct\b[^\"]*\"[^>]*>(.*?)</li>",
        html, re.DOTALL | re.IGNORECASE,
    ):
        blk = m.group(1)

        # URL: primer href apuntando a /producto/
        href_m = re.search(
            r'href="(https://pintureriasagitario\.com\.ar/producto/[^"]+)"', blk
        )
        if not href_m:
            href_m = re.search(r'href="(/producto/[^"]+)"', blk)

        # Nombre: h2 con clase woocommerce-loop-product__title
        nom_m = re.search(
            r"woocommerce-loop-product__title[^>]*>(.*?)</h[1-6]>",
            blk, re.DOTALL | re.IGNORECASE,
        )
        if not nom_m:
            nom_m = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", blk, re.DOTALL)

        if not nom_m:
            continue

        nombre = re.sub(r"<[^>]+>", "", nom_m.group(1)).strip()
        if not nombre:
            continue

        link = href_m.group(1) if href_m else None
        if link and link.startswith("/"):
            link = "https://pintureriasagitario.com.ar" + link

        precio = _woo_precio_de_bloque(blk)
        cands.append({"nombre": nombre, "link": link, "precio": precio})

    # Si los <li> no matchean: fallback por links a /producto/ con precio cercano
    if not cands:
        # Buscar todos los anchors a /producto/ con su nombre
        for m in re.finditer(
            r'href="(https://pintureriasagitario\.com\.ar/producto/[^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            nombre = re.sub(r"<[^>]+>", " ", m.group(2)).strip()
            nombre = re.sub(r"\s+", " ", nombre)
            if len(nombre) > 5:
                cands.append({"nombre": nombre, "link": m.group(1), "precio": None})

    return _dedup_candidatos(cands)


def _sagitario_precio_de_pagina(scraper, url: str) -> tuple[float | None, str | None]:
    """Visita la ficha del producto y extrae precio + nombre."""
    try:
        r = scraper.get(url, timeout=12)
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
    """WooCommerce SSR — parsea el HTML de resultados de búsqueda."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        try:
            url = (
                "https://pintureriasagitario.com.ar/?"
                + urllib.parse.urlencode({"s": query, "post_type": "product"})
            )
            resp = scraper.get(url, timeout=20)
            if resp.status_code != 200:
                logger.warning("Sagitario HTTP %d", resp.status_code)
                continue

            candidatos = _sagitario_candidatos_de_html(resp.text)
            if not candidatos:
                logger.warning("Sagitario: 0 candidatos para '%s'", query)
                continue

            # Score y filtrado
            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            relevantes = [c for c in candidatos if c["score"] >= SCORE_MIN]

            if not relevantes:
                logger.warning("Sagitario: scores insuficientes (mejor %.2f)", candidatos[0]["score"])
                continue

            # Confirmar precio en los mejores 3 (visitar ficha si el precio no vino en el listado)
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

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Sagitario"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# EASY — VTEX (API pública confirmada: /api/catalog_system/pub/products/search/)
# ════════════════════════════════════════════════════════════════════════════════
def _easy_json_a_candidatos(data) -> list[dict]:
    """Parsea el JSON de la API legacy VTEX de Easy."""
    if not isinstance(data, list):
        return []
    cands = []
    for prod in data[:10]:
        nombre = prod.get("productName") or prod.get("productTitle")
        link_text = prod.get("linkText", "")
        link = f"https://www.easy.com.ar/{link_text}/p" if link_text else None
        precio = None
        for item in prod.get("items", [])[:1]:
            for seller in item.get("sellers", [])[:1]:
                offer = seller.get("commertialOffer") or seller.get("commercialOffer") or {}
                precio = _clean_price(offer.get("Price") or offer.get("ListPrice"))
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def scrape_easy(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """
    VTEX legacy catalog API — retorna JSON directamente sin necesidad de JS.
    URL probada: /api/catalog_system/pub/products/search/QUERY?_from=0&_to=9
    """
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:4])
        q_enc   = urllib.parse.quote(q_corta)
        candidatos: list[dict] = []

        # API VTEX legacy (probada, devuelve JSON)
        for api_url in [
            f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_enc}?_from=0&_to=9",
            f"https://www.easy.com.ar/api/catalog_system/pub/products/search/{q_enc}?_from=0&_to=9&O=OrderByTopSaleDESC",
        ]:
            try:
                resp = scraper.get(
                    api_url, timeout=20,
                    headers={**HEADERS, "Accept": "application/json"},
                )
                if resp.status_code == 200:
                    candidatos = _easy_json_a_candidatos(resp.json())
                    if candidatos:
                        break
            except Exception as exc:
                logger.warning("Easy API error: %s", exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(
                precio=mejor["precio"], url=mejor["link"],
                nombre=mejor["nombre"], intento=i, score=mejor["score"],
            )
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados relevantes en Easy"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# REX — Magento 2 (CSR/React) → intentar via REST y GraphQL
# ════════════════════════════════════════════════════════════════════════════════
def _rex_graphql_candidatos(scraper, query: str) -> list[dict]:
    """
    POST al endpoint GraphQL de Magento 2.
    Magento 2 + PWA Studio/Hyvä usan GraphQL para búsqueda de productos.
    """
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
            headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
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
    """
    Magento 2 REST API: /rest/V1/products con filtro por nombre.
    """
    q_enc = urllib.parse.quote(f"%{query}%")
    try:
        # Búsqueda full-text via REST
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
            headers={**HEADERS, "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        cands = []
        for prod in items[:10]:
            nombre = prod.get("name")
            url_key = prod.get("url_key") or prod.get("custom_attributes", [{}])[0].get("value", "")
            link = f"https://www.somosrex.com/{url_key}.html" if url_key else None
            precio = _clean_price(prod.get("price"))
            if nombre:
                cands.append({"nombre": nombre, "link": link, "precio": precio})
        return cands
    except Exception as exc:
        logger.warning("Rex REST error: %s", exc)
        return []


def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Magento 2 — GraphQL POST primero, REST fallback."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])

        # Intento 1: GraphQL
        candidatos = _rex_graphql_candidatos(scraper, q_corta)

        # Intento 2: REST API
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
        result["error"] = "Sin resultados en Rex (Magento CSR — puede requerir JS)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# MERCADO LIBRE — API pública + HTML fallback
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
    """MercadoLibre: API oficial primero, luego HTML de listado."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

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
            resp = scraper.get(api_url, timeout=12)
            if resp.status_code == 200:
                for prod in resp.json().get("results", [])[:10]:
                    candidatos.append({
                        "nombre": prod.get("title"),
                        "link":   prod.get("permalink"),
                        "precio": _clean_price(prod.get("price")),
                    })
        except Exception as exc:
            logger.warning("ML API error: %s", exc)

        # B) Listado HTML con slug
        if not candidatos:
            try:
                slug = re.sub(r"\s+", "-", q_corta.strip().lower())
                url_l = f"https://listado.mercadolibre.com.ar/{urllib.parse.quote(slug)}"
                resp  = scraper.get(url_l, timeout=15)
                if resp.status_code == 200 and "suspicious-traffic" not in resp.url:
                    candidatos = _ml_candidatos_de_html(resp.text)
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
        result["error"] = "Sin resultados en ML (API puede requerir autenticación)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# SODIMAC — VTEX Intelligent Search API
# ════════════════════════════════════════════════════════════════════════════════
def _sodimac_json_a_candidatos(data) -> list[dict]:
    """Parsea respuesta de la Intelligent Search API de VTEX."""
    cands = []
    # Formato Intelligent Search: {"products": [...]} o {"items": [...]}
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
        link = f"https://www.sodimac.com.ar/{link_text}/p" if link_text else None
        precio = None
        # VTEX priceRange
        pr = prod.get("priceRange", {})
        if pr:
            precio = _clean_price(
                pr.get("sellingPrice", {}).get("lowPrice")
                or pr.get("listPrice", {}).get("lowPrice")
            )
        # VTEX items[0].sellers[0].commertialOffer.Price
        if precio is None:
            for item in prod.get("items", [])[:1]:
                for seller in item.get("sellers", [])[:1]:
                    offer = seller.get("commertialOffer") or {}
                    precio = _clean_price(offer.get("Price") or offer.get("ListPrice"))
        if nombre:
            cands.append({"nombre": nombre, "link": link, "precio": precio})
    return cands


def scrape_sodimac(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Sodimac Argentina — múltiples endpoints VTEX."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])
        q_enc   = urllib.parse.quote(q_corta)
        candidatos: list[dict] = []

        # Probar endpoints conocidos de VTEX para Sodimac
        endpoints = [
            # VTEX legacy catalog search (mismo que Easy, probado)
            (f"https://www.sodimac.com.ar/api/catalog_system/pub/products/search/{q_enc}?_from=0&_to=9",
             "application/json"),
            # VTEX Intelligent Search
            (f"https://www.sodimac.com.ar/_v/api/intelligent-search/product_search/trade-policy/1"
             f"?query={q_enc}&count=10&sort=score_desc",
             "application/json"),
            # Búsqueda old-style Sodimac (ATG)
            (f"https://www.sodimac.com.ar/sodimac-ar/search?Ntt={q_enc}&format=json",
             "application/json"),
        ]
        for url, accept in endpoints:
            try:
                resp = scraper.get(
                    url, timeout=20,
                    headers={**HEADERS, "Accept": accept},
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        candidatos = _sodimac_json_a_candidatos(data)
                    except Exception:
                        pass
                    if candidatos:
                        break
            except Exception as exc:
                logger.warning("Sodimac %s error: %s", url[:60], exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(
                precio=mejor["precio"], url=mejor["link"],
                nombre=mejor["nombre"], intento=i, score=mejor["score"],
            )
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = "Sin resultados en Sodimac (API puede requerir JS)"
    return result


# ════════════════════════════════════════════════════════════════════════════════
# Orquestador principal
# ════════════════════════════════════════════════════════════════════════════════
def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """Ejecuta los 5 scrapers en paralelo."""
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers = {
        "rex":       scrape_rex,
        "sagitario": scrape_sagitario,
        "ml":        scrape_ml,
        "sodimac":   scrape_sodimac,
        "easy":      scrape_easy,
    }
    resultados: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {name: ex.submit(fn, *args) for name, fn in scrapers.items()}
        for name, fut in futures.items():
            try:
                resultados[name] = fut.result(timeout=40)
            except Exception as exc:
                r = _empty_result()
                r["error"] = str(exc)
                resultados[name] = r
    return resultados
