#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install Chrome for Testing + its matching chromedriver into ./.chrome so the
# bot can run on hosts that have no system Chrome (e.g. Render's *native*
# Python runtime). The app auto-detects ./.chrome/chrome-linux64/chrome.
#
# Render: set the Build Command to
#     pip install -r requirements.txt && bash build.sh
# (or simply deploy with the provided Dockerfile, which apt-installs Chrome.)
#
# Set CHROME_INSTALL_DIR to install somewhere else.
# ---------------------------------------------------------------------------
set -euo pipefail

DEST="${CHROME_INSTALL_DIR:-$PWD/.chrome}"
mkdir -p "$DEST"

echo "[build.sh] Resolving Chrome for Testing stable version..."
VER=$(python - <<'PY'
import json, urllib.request
u = ("https://googlechromelabs.github.io/chrome-for-testing/"
     "last-known-good-versions-with-downloads.json")
print(json.load(urllib.request.urlopen(u, timeout=60))["channels"]["Stable"]["version"])
PY
)
echo "[build.sh] Chrome for Testing version: $VER"

BASE="https://storage.googleapis.com/chrome-for-testing-public/$VER/linux64"
TMP="${TMPDIR:-/tmp}"
curl -fsSL -o "$TMP/chrome-linux64.zip" "$BASE/chrome-linux64.zip"
curl -fsSL -o "$TMP/chromedriver-linux64.zip" "$BASE/chromedriver-linux64.zip"

python - "$TMP/chrome-linux64.zip" "$TMP/chromedriver-linux64.zip" "$DEST" <<'PY'
import sys, zipfile
for z in sys.argv[1:3]:
    with zipfile.ZipFile(z) as zf:
        zf.extractall(sys.argv[3])
PY

chmod +x "$DEST/chrome-linux64/chrome" \
         "$DEST/chromedriver-linux64/chromedriver" 2>/dev/null || true
echo "[build.sh] Done. Chrome at $DEST/chrome-linux64/chrome"

# ---------------------------------------------------------------------------
# Chrome also needs a handful of *system* libraries that minimal images often
# lack (libnss3, libgbm, libasound2, ...). `apt-get download` + `dpkg-deb -x`
# work without root, so unpack them next to Chrome; the app adds
# $DEST/libs/** to LD_LIBRARY_PATH at startup. Skip with CHROME_INSTALL_LIBS=0.
# ---------------------------------------------------------------------------
if [ "${CHROME_INSTALL_LIBS:-1}" != "0" ] && command -v apt-get >/dev/null 2>&1 \
        && command -v dpkg-deb >/dev/null 2>&1; then
  LIBS="$DEST/libs"
  DEBS="$DEST/debs"
  mkdir -p "$LIBS" "$DEBS"
  APT_LIBS="libnss3 libnspr4 libxss1 libasound2 libdbus-1-3 libatk1.0-0 \
libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libxkbcommon0 libxcomposite1 \
libxdamage1 libxext6 libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 \
libatspi2.0-0 libexpat1 libglib2.0-0 libuuid1"
  echo "[build.sh] Fetching Chrome shared libraries..."
  cd "$DEBS"
  for pkg in $APT_LIBS; do
    DEBIAN_FRONTEND=noninteractive apt-get download "$pkg" >/dev/null 2>&1 \
      || DEBIAN_FRONTEND=noninteractive apt-get download "${pkg}t64" >/dev/null 2>&1 \
      || echo "[build.sh]   (skipped $pkg; the app will retry at runtime)"
  done
  cd - >/dev/null
  ok=0
  for deb in "$DEBS"/*.deb; do
    [ -e "$deb" ] || continue
    if dpkg-deb -x "$deb" "$LIBS" 2>/dev/null; then ok=$((ok + 1)); fi
  done
  echo "[build.sh] Unpacked $ok library package(s) into $LIBS"
fi
echo "[build.sh] Complete."
