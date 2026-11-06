#!/usr/bin/env bash
# Build the design Q&A into .docx and .pdf from the single Markdown source.
# Usage: ./docs/build.sh
# Needs: pandoc, Google Chrome (for HTML -> PDF).
set -euo pipefail

DOCS="$(cd "$(dirname "$0")" && pwd)"
SRC="$DOCS/design-qa.md"
OUT="$DOCS/build"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p "$OUT"

# 1. Word document
pandoc "$SRC" -o "$OUT/design-qa.docx" --toc --toc-depth=2

# 2. PDF: Markdown -> styled standalone HTML -> Chrome prints it to PDF
pandoc "$SRC" -o "$OUT/design-qa.html" --standalone --embed-resources \
  --toc --toc-depth=2 --css "$DOCS/pdf.css" --metadata lang=en
"$CHROME" --headless --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$OUT/design-qa.pdf" "file://$OUT/design-qa.html" 2>/dev/null
rm "$OUT/design-qa.html"

echo "Built: $OUT/design-qa.docx and $OUT/design-qa.pdf"
