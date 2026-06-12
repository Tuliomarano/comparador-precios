#!/bin/bash
# Descarga el binario de Chromium que usa Playwright.
# Este script corre una sola vez durante el deploy en Streamlit Cloud.
playwright install chromium
playwright install-deps chromium
