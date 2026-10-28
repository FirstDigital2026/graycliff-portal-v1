#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
npm install --prefix deployment/pdf-ocr --omit=dev --ignore-scripts --no-audit --no-fund
mkdir -p deployment/pdf-ocr/language
node deployment/pdf-ocr/prepare-language.cjs
