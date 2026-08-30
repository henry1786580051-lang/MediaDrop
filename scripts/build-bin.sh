#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
BIN_DIST="$ROOT_DIR/bin-dist"
BUNDLED_BIN="$ROOT_DIR/bundled-bin"
YTDLP_VERSION="${YTDLP_VERSION:-2026.08.19}"
FFMPEG_VERSION="8.1.2"
PYTHON_BIN="${PYTHON_BIN:-python3}"

rm -rf "$BIN_DIST/mediadrop-server" "$BUNDLED_BIN"
mkdir -p "$BUNDLED_BIN"

(
  cd "$APP_DIR"
  "$PYTHON_BIN" -m PyInstaller -y mediadrop-server.spec --distpath "$BIN_DIST"
)

cp -R "$BIN_DIST/mediadrop-server/." "$BUNDLED_BIN/"

case "$(uname -s)" in
  Darwin)
    YTDLP_ASSET="yt-dlp_macos"
    ;;
  *)
    echo "Unsupported local build platform: $(uname -s)" >&2
    exit 1
    ;;
esac

curl --fail --location --retry 3 -o "$BUNDLED_BIN/$YTDLP_ASSET" "https://github.com/yt-dlp/yt-dlp/releases/download/$YTDLP_VERSION/$YTDLP_ASSET"
curl --fail --location --retry 3 -o "$BUNDLED_BIN/SHA2-256SUMS" "https://github.com/yt-dlp/yt-dlp/releases/download/$YTDLP_VERSION/SHA2-256SUMS"
(
  cd "$BUNDLED_BIN"
  grep "  ${YTDLP_ASSET}$" SHA2-256SUMS | shasum -a 256 -c -
)
mv "$BUNDLED_BIN/$YTDLP_ASSET" "$BUNDLED_BIN/yt-dlp"
rm "$BUNDLED_BIN/SHA2-256SUMS"
"$ROOT_DIR/scripts/build-ffmpeg-macos.sh" "$BUNDLED_BIN/ffmpeg"

chmod +x "$BUNDLED_BIN/mediadrop-server" "$BUNDLED_BIN/yt-dlp" "$BUNDLED_BIN/ffmpeg" "$BUNDLED_BIN/ffprobe"

actual_ytdlp_version="$("$BUNDLED_BIN/yt-dlp" --version)"
echo "yt-dlp version: $actual_ytdlp_version"
if [[ "$actual_ytdlp_version" != "$YTDLP_VERSION" ]]; then
  echo "yt-dlp version mismatch: expected $YTDLP_VERSION" >&2
  exit 1
fi
"$BUNDLED_BIN/ffmpeg" -version 2>&1 | sed -n '1p' | grep -Eq "ffmpeg version n?${FFMPEG_VERSION}([ .-]|$)"
"$BUNDLED_BIN/ffmpeg" -hide_banner -encoders 2>&1 | grep 'libmp3lame' >/dev/null
"$BUNDLED_BIN/ffmpeg" -hide_banner -encoders 2>&1 | grep -Eq '[[:space:]]png[[:space:]]'
"$BUNDLED_BIN/ffprobe" -version 2>&1 | sed -n '1p' | grep -Eq "ffprobe version n?${FFMPEG_VERSION}([ .-]|$)"
