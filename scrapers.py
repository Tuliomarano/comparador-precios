"""
scrapers.py — Búsqueda de precios en Rex, Sagitario, MercadoLibre, Sodimac y Easy.

Usa `cloudscraper` para bypass de Cloudflare y protecciones anti-bot básicas.
Sin Playwright (incompatible con Streamlit Cloud).

Plataformas detectadas (jun 2026):
  - Rex        → somosrex.com        → Magento  (/catalogsearch/result/?q=)
  - Sagitario  → pintureriasagitario.com.ar → WooCommerce (/?s=&post_type=product)
  - ML         → listado.mercadolibre.com.ar → HTML (API anon. 403)
  - Sodimac    → sodimac.com.ar              → ATG/propio (/sodimac-ar/search?Ntt=)
  - Easy       → easy.com.ar                 → propio (/search?q=)
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
    """
    Devuelve un cloudscraper que bypasea Cloudflare y anti-bots básicos.
    Si cloudscraper no está instalado, usa requests.Session como fallback.
    """
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
    s = re.sub(r"(\d+)\s*(kgs?|kilos?)\b",  r"\1kg", s)
    s = re.sub(r"(\d+)\s*(lts?|litros?|l)\b", r"\1lt", s)
    s = re.sub(r"(\d+)\s*(grs?|gramos?)\b",  r"\1gr", s)
    s = re.sub(r"(\d+)\s*(ml|cc)\b",         r"\1ml", s)
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
        extra = 0
        faltantes = set_b - set_e
        for tok in faltantes:
            if any(tok in e or e in tok for e in set_e if len(tok) >= 4 and len(e) >= 4):
                extra += 1
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
    candidatos = [q_com, q_det, cod]
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


# ── Extractores genéricos ─────────────────────────────────────────────────────────
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
    """Extrae candidatos de bloques JSON-LD (funciona en cualquier sitio moderno)."""
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


# ── Rex — Magento ──────────────────────────────────────────────────────────────────
def _rex_candidatos_de_html(html: str, base_url: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)

    # Magento: tarjetas <li class="item product product-item">
    if not cands:
        for m in re.finditer(
            r'<li[^>]*class="[^"]*\bproduct[^"]*\bitem[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            blk = m.group(1)
            href = re.search(r'href="(https://[^"]+)"', blk)
            nom_m = (
                re.search(r'class="product-item-link"[^>]*>(.*?)</a>', blk, re.DOTALL)
                or re.search(r'<a[^>]+href="https://[^"]+"[^>]*>(.*?)</a>', blk, re.DOTALL)
            )
            # Magento embebe el precio como data-price-amount="1234.56"
            price_m = re.search(r'data-price-amount="([\d.]+)"', blk)
            if not price_m:
                price_m = re.search(r'<span[^>]*class="[^"]*\bprice\b[^"]*"[^>]*>[\s\$]*([\d.,]+)', blk)
            if href and nom_m:
                nombre = re.sub(r"<[^>]+>", "", nom_m.group(1)).strip()
                if nombre:
                    cands.append({
                        "nombre": _html.unescape(nombre),
                        "link":   href.group(1),
                        "precio": _clean_price(price_m.group(1)) if price_m else None,
                    })

    # Fallback: JSON embedded en la página (Magento a veces tiene window.productList o similar)
    if not cands:
        nombres = re.findall(r'"name"\s*:\s*"([^"]{5,100})"', html)
        precios = (
            re.findall(r'"finalPrice"\s*:\s*\{"amount"\s*:\s*([\d.]+)', html)
            or re.findall(r'"regularPrice"\s*:\s*([\d.]+)', html)
            or re.findall(r'"Price"\s*:\s*([\d.]+)', html)
        )
        hrefs = re.findall(r'"url"\s*:\s*"(https://www\.somosrex\.com/[^"]+)"', html)
        for idx, nom in enumerate(nombres[:8]):
            link = hrefs[idx] if idx < len(hrefs) else base_url
            cands.append({
                "nombre": _html.unescape(nom),
                "link":   link,
                "precio": _clean_price(precios[idx]) if idx < len(precios) else None,
            })

    return _dedup_candidatos(cands)


def scrape_rex(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Busca en somosrex.com (Magento) usando la URL /catalogsearch/result/?q="""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()
    scraper.headers.update({"Referer": "https://www.somosrex.com/"})

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])
        q_enc   = urllib.parse.quote(q_corta)
        url     = f"https://www.somosrex.com/catalogsearch/result/?q={q_enc}"
        try:
            resp = scraper.get(url, timeout=20)
            if resp.status_code != 200:
                logger.warning("Rex HTTP %d para %s", resp.status_code, url)
                continue
            candidatos = _rex_candidatos_de_html(resp.text, url)
            mejor = _elegir_mejor(candidatos, ref)
            if mejor:
                result.update(precio=mejor["precio"], url=mejor["link"],
                              nombre=mejor["nombre"], intento=i, score=mejor["score"])
                break
        except Exception as exc:
            logger.warning("Rex error intento %d: %s", i, exc)

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Rex tras {len(queries)} intentos"
    return result


# ── Sagitario — WooCommerce ────────────────────────────────────────────────────────
def _sagitario_candidatos_de_html(html: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)

    # WooCommerce: <li class="...product...">
    if len(cands) < 2:
        for m in re.finditer(
            r'<li[^>]*class="[^"]*\bproduct\b[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL | re.IGNORECASE,
        ):
            blk = m.group(1)
            href = re.search(r'<a[^>]+href="([^"]+)"', blk)
            nom  = (
                re.search(r'woocommerce-loop-product__title[^>]*>(.*?)<', blk, re.DOTALL)
                or re.search(r'<h2[^>]*>(.*?)</h2>', blk, re.DOTALL)
            )
            price = re.search(
                r'class="[^"]*\bprice\b[^"]*"[^>]*>.*?([\d.][\d.,]*)',
                blk, re.DOTALL,
            )
            if href and nom:
                nombre = re.sub(r"<[^>]+>", "", nom.group(1)).strip()
                if nombre:
                    cands.append({
                        "nombre": nombre,
                        "link":   _html.unescape(href.group(1)),
                        "precio": _clean_price(price.group(1)) if price else None,
                    })

    # Tiendanube fallback
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
                    link = "https://pintureriasagitario.com.ar" + link
                cands.append({"nombre": nombre, "link": _html.unescape(link), "precio": None})

    return _dedup_candidatos(cands)


def _sagitario_precio_de_pagina(scraper, url: str) -> tuple[float | None, str | None]:
    try:
        r = scraper.get(url, timeout=12)
        h = r.text
        precio = nombre = None
        cands = _candidatos_de_jsonld(h)
        for c in cands:
            if c.get("precio"):
                precio = c["precio"]
                nombre = c.get("nombre")
                break
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
            resp = scraper.get(url, timeout=15)
            resp.raise_for_status()
            candidatos = _sagitario_candidatos_de_html(resp.text)
            if not candidatos:
                continue

            for c in candidatos:
                c["score"] = score_similitud(ref, c["nombre"])
            candidatos.sort(key=lambda c: c["score"], reverse=True)
            candidatos = [c for c in candidatos if c["score"] >= SCORE_MIN]
            if not candidatos:
                continue

            elegido = None
            for c in candidatos[:3]:
                precio = c.get("precio")
                nombre = c.get("nombre")
                if precio is None and c.get("link"):
                    precio, nombre_pg = _sagitario_precio_de_pagina(scraper, c["link"])
                    nombre = nombre_pg or nombre
                if precio:
                    elegido = {"precio": precio, "link": c.get("link") or url,
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


# ── MercadoLibre ───────────────────────────────────────────────────────────────────
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

        # B) Página de búsqueda HTML
        if not candidatos:
            try:
                url_s = (
                    f"https://www.mercadolibre.com.ar/search"
                    f"?q={urllib.parse.quote(q_corta)}&sort=relevance_v2"
                )
                resp = scraper.get(url_s, timeout=15)
                if resp.status_code == 200 and "suspicious-traffic" not in resp.url:
                    candidatos = _ml_candidatos_de_html(resp.text)
            except Exception as exc:
                logger.warning("ML search page error: %s", exc)

        # C) Listado con slug
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
        result.update(precio=mediana, url=mejor.get("link"),
                      nombre=mejor.get("nombre"), intento=i, score=mejor["score"])
        break

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en ML tras {len(queries)} intentos"
    return result


# ── Sodimac ────────────────────────────────────────────────────────────────────────
def _sodimac_candidatos_de_html(html: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)

    # Sodimac embebe datos en window.__PRELOADED_STATE__ o similar JSON en script
    if not cands:
        for pat in (
            r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*(?:</script>|window\.)',
            r'window\.__PRELOADED_STATE__\s*=\s*({.+?});\s*(?:</script>|window\.)',
            r'"products"\s*:\s*(\[.+?\])\s*,?\s*"',
        ):
            for m in re.finditer(pat, html, re.DOTALL):
                try:
                    data = json.loads(m.group(1))
                    # Buscar lista de productos en diferentes paths del state
                    prods = []
                    if isinstance(data, list):
                        prods = data
                    elif isinstance(data, dict):
                        prods = (
                            data.get("search", {}).get("results", [])
                            or data.get("results", [])
                            or data.get("products", [])
                        )
                    for prod in prods[:8]:
                        if not isinstance(prod, dict):
                            continue
                        nombre = (
                            prod.get("displayName") or prod.get("name")
                            or prod.get("productName") or prod.get("title")
                        )
                        precio = _clean_price(
                            prod.get("currentPrice") or prod.get("price")
                            or prod.get("regularPrice") or prod.get("normalPrice")
                        )
                        link = prod.get("url") or prod.get("productUrl") or prod.get("slug")
                        if nombre:
                            if link and not link.startswith("http"):
                                link = "https://www.sodimac.com.ar" + link
                            cands.append({"nombre": nombre, "link": link, "precio": precio})
                except Exception:
                    continue
            if cands:
                break

    # Fallback: tarjetas HTML genéricas
    if not cands:
        for blk in re.split(
            r'<(?:div|article|li)[^>]+class="[^"]*(?:product|item|card)[^"]*"',
            html
        )[1:]:
            href = re.search(r'href="([^"]+)"', blk)
            nom  = re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            price = re.search(r'(?:finalPrice|currentPrice|price)["\s:]+[\$\s]*([\d.,]+)', blk)
            if href and nom:
                nombre = re.sub(r"<[^>]+>", "", nom.group(1)).strip()
                link = href.group(1)
                if not link.startswith("http"):
                    link = "https://www.sodimac.com.ar" + link
                if nombre:
                    cands.append({
                        "nombre": nombre,
                        "link":   link,
                        "precio": _clean_price(price.group(1)) if price else None,
                    })

    return _dedup_candidatos(cands)


def scrape_sodimac(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Busca en Sodimac Argentina."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])
        q_enc   = urllib.parse.quote(q_corta)
        candidatos: list[dict] = []

        for url in [
            f"https://www.sodimac.com.ar/sodimac-ar/search?Ntt={q_enc}",
            f"https://www.sodimac.com.ar/sodimac-ar/search?q={q_enc}",
        ]:
            try:
                resp = scraper.get(url, timeout=20)
                if resp.status_code == 200:
                    candidatos = _sodimac_candidatos_de_html(resp.text)
                    if candidatos:
                        break
            except Exception as exc:
                logger.warning("Sodimac %s error: %s", url, exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                          nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Sodimac tras {len(queries)} intentos"
    return result


# ── Easy ───────────────────────────────────────────────────────────────────────────
def _easy_candidatos_de_html(html: str) -> list[dict]:
    cands = _candidatos_de_jsonld(html)

    # Easy Argentina puede embeber datos como JSON en script o usar Vtex/propio
    if not cands:
        for pat in (
            r'window\.__INITIAL_STATE__\s*=\s*({.+?});\s*(?:</script>|window\.)',
            r'__STATE__\s*=\s*({.+?});\s*</script>',
            r'"products"\s*:\s*(\[.+?\])\s*,',
        ):
            for m in re.finditer(pat, html, re.DOTALL):
                try:
                    data = json.loads(m.group(1))
                    prods = []
                    if isinstance(data, list):
                        prods = data
                    elif isinstance(data, dict):
                        prods = (
                            data.get("search", {}).get("results", [])
                            or data.get("products", [])
                            or data.get("results", [])
                        )
                    for prod in prods[:8]:
                        if not isinstance(prod, dict):
                            continue
                        nombre = (
                            prod.get("productName") or prod.get("name")
                            or prod.get("title") or prod.get("displayName")
                        )
                        precio = _clean_price(
                            prod.get("price") or prod.get("currentPrice")
                            or prod.get("regularPrice") or prod.get("listPrice")
                        )
                        link = prod.get("url") or prod.get("linkText")
                        if nombre:
                            if link and not link.startswith("http"):
                                if "/" not in link:
                                    link = f"https://www.easy.com.ar/{link}/p"
                                else:
                                    link = "https://www.easy.com.ar" + link
                            cands.append({"nombre": nombre, "link": link, "precio": precio})
                except Exception:
                    continue
            if cands:
                break

    # Fallback: tarjetas HTML y regex de precio
    if not cands:
        for blk in re.split(
            r'<(?:div|article|li)[^>]+class="[^"]*(?:product|item|result)[^"]*"',
            html
        )[1:]:
            href = re.search(r'href="([^"]+)"', blk)
            nom  = re.search(r'<h[23][^>]*>(.*?)</h[23]>', blk, re.DOTALL)
            price = re.search(r'[\$\$]?\s*([\d.,]{4,})', blk)
            if href and nom:
                nombre = re.sub(r"<[^>]+>", "", nom.group(1)).strip()
                link = href.group(1)
                if not link.startswith("http"):
                    link = "https://www.easy.com.ar" + link
                if nombre:
                    cands.append({
                        "nombre": nombre,
                        "link":   link,
                        "precio": _clean_price(price.group(1)) if price else None,
                    })

    return _dedup_candidatos(cands)


def scrape_easy(detalle: str, marca: str, cod_proveedor, nombre_proveedor: str) -> dict:
    """Busca en Easy Argentina."""
    queries = _build_queries(detalle, marca, cod_proveedor)
    result  = _empty_result()
    scraper = _make_scraper()

    for i, item in enumerate(queries, start=1):
        query, ref = item["q"], item["ref"]
        q_corta = " ".join(query.split()[:5])
        q_enc   = urllib.parse.quote(q_corta)
        candidatos: list[dict] = []

        for url in [
            f"https://www.easy.com.ar/search?q={q_enc}",
            f"https://www.easy.com.ar/search?term={q_enc}",
        ]:
            try:
                resp = scraper.get(url, timeout=20)
                if resp.status_code == 200:
                    candidatos = _easy_candidatos_de_html(resp.text)
                    if candidatos:
                        break
            except Exception as exc:
                logger.warning("Easy %s error: %s", url, exc)

        mejor = _elegir_mejor(candidatos, ref)
        if mejor:
            result.update(precio=mejor["precio"], url=mejor["link"],
                          nombre=mejor["nombre"], intento=i, score=mejor["score"])
            break

    if result["precio"] is None and result["error"] is None:
        result["error"] = f"Sin resultados relevantes en Easy tras {len(queries)} intentos"
    return result


# ── Orquestador principal ──────────────────────────────────────────────────────────
def buscar_precios(detalle: str, marca: str,
                   cod_proveedor=None,
                   nombre_proveedor: str = "") -> dict[str, dict]:
    """Ejecuta los 5 scrapers en paralelo y devuelve resultados indexados por nombre."""
    args = (detalle, marca, cod_proveedor, nombre_proveedor)
    scrapers = {
        "rex":      scrape_rex,
        "sagitario": scrape_sagitario,
        "ml":       scrape_ml,
        "sodimac":  scrape_sodimac,
        "easy":     scrape_easy,
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
