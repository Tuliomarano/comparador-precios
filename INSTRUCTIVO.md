# Comparador de Precios — Instructivo de uso y lógica técnica

## ¿Qué hace la app?

Permite consultar, para cualquier producto del catálogo, los precios publicados en **Rex**, **Sagitario** y **MercadoLibre**, y compararlos contra el precio neto de venta propio. Los resultados muestran la diferencia en pesos y en porcentaje.

---

## Archivos necesarios en la carpeta raíz

| Archivo | Descripción |
|---|---|
| `Costos + Precios DD-MM-YYYY.xlsx` | Lista de productos con costos, precios y marca. La fecha del nombre se muestra en la app como "vigente al …". |
| `Master Prov - DD-MM.XLSX` | Tabla de proveedores. Columnas: `codigo`, `nombre`, `condicion`. Se usa para resolver el nombre del proveedor a partir del código numérico. |

**La app detecta automáticamente el archivo más reciente** que coincida con esos nombres. Para actualizar los datos, simplemente reemplazá el archivo (mismo formato, nombre con la nueva fecha).

### Columnas del archivo de costos

| Columna original | Nombre interno | Uso |
|---|---|---|
| CODIGO PROVEEDOR | `cod_proveedor` | Código que el fabricante asigna al SKU; se usa como último fallback de búsqueda |
| CODIGO INTERNO | `cod_interno` | Tu código interno; permite búsqueda exacta |
| DETALLE PRODUCTO | `detalle` | Descripción; base del autocomplete y de la query de scraping |
| COSTO DE FORMACION... | `costo` | Costo de referencia; se muestra en la ficha |
| PRECIO NETO | `precio_neto` | Precio neto de venta; base para calcular márgenes |
| MARCA | `marca` | Se concatena con el detalle para la búsqueda (ver lógica de cascada) |
| PROVEEDOR | `cod_empresa_prov` | Código numérico del proveedor; se resuelve a nombre via Master Prov |

---

## Cómo usar la app

### Búsqueda individual

1. Escribí en el campo de búsqueda **código interno**, **código de proveedor** o **parte de la descripción**.
   - Si escribís un número exacto → la app lo busca por código primero.
   - Si escribís texto → aparece un dropdown con hasta 10 coincidencias para seleccionar.
2. Una vez seleccionado el producto, se muestran: detalle, marca, proveedor, costo y precio neto.
3. Hacé clic en **"Buscar precios en Rex / Sagitario / ML"** para lanzar el scraping.
4. La tabla muestra el precio encontrado en cada sitio, la diferencia vs. tu precio neto y el link al producto.
5. Podés exportar el resultado a Excel con el botón de descarga.

### Búsqueda masiva

1. Pegá en el cuadro de texto una lista de **códigos internos**, uno por línea.
2. Hacé clic en **"Buscar todos"**.
3. La app consulta todos los productos en paralelo (hasta 5 a la vez) y arma una tabla consolidada.
4. Exportable a Excel con un clic.

---

## Lógica de cada script

### `data.py` — Carga y acceso a datos

**Responsabilidades:**
- Lee `Costos + Precios …xlsx` y `Master Prov …XLSX` desde la carpeta raíz.
- Resuelve el nombre del proveedor cruzando `cod_empresa_prov` contra la tabla de proveedores.
- Expone `buscar_productos(query)` para el autocomplete: busca primero por código exacto, luego por texto en el detalle, priorizando coincidencias en marca.
- Los datos se cachean en memoria (`@lru_cache`) para no releer el Excel en cada búsqueda.

**Funciones principales:**

| Función | Qué hace |
|---|---|
| `load_proveedores()` | Lee Master Prov → `{código: nombre}` |
| `load_productos()` | Lee Costos + Precios, resuelve proveedores, devuelve DataFrame + fecha |
| `buscar_productos(query)` | Autocomplete: código exacto → texto parcial |
| `get_producto_por_codigo(cod)` | Devuelve una fila por código interno (para búsqueda masiva) |

---

### `scrapers.py` — Búsqueda de precios externos

**Responsabilidades:**
- Lanza búsquedas en Rex, Sagitario y MercadoLibre.
- Implementa la **lógica de cascada** (ver abajo).
- Ejecuta los 3 scrapers en paralelo via `ThreadPoolExecutor`.

#### Lógica de cascada (los 3 intentos)

Para cada sitio, la búsqueda sigue este orden hasta encontrar un precio:

```
Intento 1: "POLACRIN MEM FTES Y MUROS 20L  POLACRIN"
           (DETALLE + MARCA)

Intento 2: "POLACRIN MEM FTES Y MUROS 20L"
           (solo DETALLE — si intento 1 no dio resultados)

Intento 3: "85031007"
           (COD_PROVEEDOR numérico — último recurso)
```

La columna "Sitio" en los resultados indica con *(fallback 2)* o *(fallback 3)* cuándo se usó un intento alternativo.

#### Scrapers por sitio

| Sitio | Tecnología | Estrategia |
|---|---|---|
| **Rex** | Playwright + Vtex | Navega a `/busca/?q=<query>`, toma el primer resultado de la grilla |
| **Sagitario** | Playwright + WooCommerce | Navega a `/?s=<query>&post_type=product`, toma el primer producto |
| **MercadoLibre** | API oficial (REST) | Consulta `/sites/MLA/search?q=<query>`, calcula **promedio ponderado** por unidades vendidas de los primeros 10 resultados |

> **ML usa promedio ponderado** porque un mismo producto puede estar publicado a distintos precios; ponderar por ventas da el precio que realmente compra el mercado.

---

### `app.py` — Interfaz de usuario (Streamlit)

**Responsabilidades:**
- Muestra el header con la fecha del archivo de costos.
- Gestiona el autocomplete (campo único → código o descripción).
- Muestra la ficha del producto con KPIs internos (costo, precio neto, margen bruto).
- Llama a `buscar_precios()` al presionar el botón y renderiza la tabla de comparación.
- Tab de búsqueda masiva: acepta lista de códigos, lanza todo en paralelo.
- Botón de exportación a Excel en ambas tabs.

---

## Estructura de carpetas

```
Scrapping de precios/
│
├── Costos + Precios 11-06-2026.xlsx   ← datos de productos (reemplazar para actualizar)
├── Master Prov - 10-04.XLSX           ← tabla de proveedores
│
└── comparador_precios/                ← carpeta que se sube a GitHub
    ├── app.py                         ← UI principal
    ├── data.py                        ← carga de datos y autocomplete
    ├── scrapers.py                    ← scraping Rex / Sagitario / ML
    ├── requirements.txt               ← dependencias Python
    ├── packages.txt                   ← dependencias del sistema (Chromium)
    └── INSTRUCTIVO.md                 ← este archivo
```

> **Importante:** los archivos Excel deben estar **un nivel arriba** de `comparador_precios/` (en la raíz del proyecto), no dentro de la subcarpeta. `data.py` los detecta automáticamente con `os.path.dirname(os.path.dirname(__file__))`.

---

## Deploy en Streamlit Cloud (paso a paso)

1. Creá un repositorio en GitHub (puede ser privado).
2. Subí **solo la carpeta `comparador_precios/`** como raíz del repo (o como subcarpeta y apuntás el entry point).
3. Entrá a [share.streamlit.io](https://share.streamlit.io) → **New app**.
4. Elegí el repo, la rama (`main`) y apuntá el entry point a `app.py`.
5. En **Advanced settings → Secrets**, no es necesario agregar nada (MercadoLibre usa API pública).
6. Hacé clic en **Deploy**. El primer build tarda 5–10 minutos (instala Playwright + Chromium).
7. Streamlit te da una URL pública para compartir con el equipo.

### Actualizar los datos de costos en producción

En Streamlit Cloud los archivos Excel **no se suben** — la app lee desde la carpeta del repo. Para actualizar:
- Opción A: comiteá el nuevo Excel al repo y Streamlit hace redeploy automático.
- Opción B (recomendada): usá [Streamlit Secrets + Google Sheets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management) para tener los datos en la nube y actualizarlos sin tocar el código.

---

## Preguntas frecuentes

**¿Por qué tarda la búsqueda?**
Rex y Sagitario requieren abrir un navegador real (Playwright) para renderizar el JavaScript de sus páginas. Cada búsqueda demora entre 5 y 15 segundos por sitio. ML es instantáneo porque usa API REST.

**¿Qué pasa si un producto no aparece en ningún sitio?**
La app muestra el mensaje de error de cada scraper indicando cuántos intentos realizó. Podés registrarlo para revisión manual.

**¿Cómo se detecta la fecha del archivo de costos?**
Se toma la fecha de modificación del archivo en el sistema de archivos. Si el nombre incluye una fecha (ej. `11-06-2026`), también podría extraerse del nombre — actualmente usa la fecha del archivo que es más confiable.

**¿Se puede agregar otro competidor?**
Sí: creá una función `scrape_nuevo(detalle, marca, cod_proveedor, nombre_proveedor)` en `scrapers.py` siguiendo el mismo patrón, agregala al dict `scrapers` en `buscar_precios()`, y aparecerá automáticamente en la tabla de resultados.
