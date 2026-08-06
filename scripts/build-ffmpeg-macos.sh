#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-$ROOT_DIR/bundled-bin/ffmpeg}"
FFMPEG_VERSION="8.1.2"
FFMPEG_ARCHIVE="ffmpeg-n${FFMPEG_VERSION}.tar.gz"
FFMPEG_SOURCE_DIR="FFmpeg-n${FFMPEG_VERSION}"
FFMPEG_URL="https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/refs/tags/n${FFMPEG_VERSION}"
FFMPEG_SHA256="9fd092511605bbebafe095ea6d38d9e40f34d12f7386e1258372df8be0576eb7"
LAME_VERSION="3.100"
LAME_ARCHIVE="lame-${LAME_VERSION}.tar.gz"
LAME_URL="https://downloads.sourceforge.net/project/lame/lame/${LAME_VERSION}/${LAME_ARCHIVE}"
LAME_SHA256="ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e"
SOURCE_CACHE="${MEDIADROP_FFMPEG_SOURCE_CACHE:-$ROOT_DIR/build/ffmpeg-sources}"
BINARY_CACHE="${MEDIADROP_FFMPEG_BINARY_CACHE:-$ROOT_DIR/build/ffmpeg-${FFMPEG_VERSION}-macos-arm64-v1}"
MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
export MACOSX_DEPLOYMENT_TARGET

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "FFmpeg ${FFMPEG_VERSION} must be built on macOS ARM64." >&2
  exit 1
fi

JOBS="$(sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '4')"
if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
  JOBS=4
fi

verify_sha256() {
  local archive="$1"
  local expected="$2"
  printf '%s  %s\n' "$expected" "$archive" | shasum -a 256 -c - >/dev/null
}

download_verified() {
  local url="$1"
  local destination="$2"
  local expected="$3"

  if [[ -f "$destination" ]] && verify_sha256 "$destination" "$expected"; then
    return
  fi

  rm -f "$destination"
  curl --fail --location --retry 3 --proto '=https' -o "$destination" "$url"
  verify_sha256 "$destination" "$expected"
}

validate_binary() {
  local binary="$1"
  local version_output
  local encoder_output
  local binary_minos
  [[ -x "$binary" ]] || return 1
  file "$binary" | grep -q 'arm64' || return 1
  version_output="$("$binary" -version 2>&1)"
  printf '%s\n' "$version_output" | sed -n '1p' | grep -Eq "ffmpeg version n?${FFMPEG_VERSION}([ .-]|$)" || return 1
  encoder_output="$("$binary" -hide_banner -encoders 2>&1)"
  printf '%s\n' "$encoder_output" | grep 'libmp3lame' >/dev/null || return 1
  binary_minos="$(vtool -show-build "$binary" 2>/dev/null | awk '$1 == "minos" { print $2; exit }')"
  [[ "$binary_minos" == "$MACOSX_DEPLOYMENT_TARGET" ]] || return 1

  if otool -L "$binary" | tail -n +2 | grep -Ev '^[[:space:]]+(/usr/lib/|/System/Library/)' | grep -q .; then
    echo "FFmpeg contains a non-system dynamic-library dependency:" >&2
    otool -L "$binary" >&2
    return 1
  fi
}

mkdir -p "$SOURCE_CACHE" "$BINARY_CACHE" "$(dirname "$OUTPUT_PATH")"

if validate_binary "$BINARY_CACHE/ffmpeg"; then
  cp "$BINARY_CACHE/ffmpeg" "$OUTPUT_PATH"
  chmod +x "$OUTPUT_PATH"
  echo "Using cached FFmpeg ${FFMPEG_VERSION} build."
  exit 0
fi

download_verified "$FFMPEG_URL" "$SOURCE_CACHE/$FFMPEG_ARCHIVE" "$FFMPEG_SHA256"
download_verified "$LAME_URL" "$SOURCE_CACHE/$LAME_ARCHIVE" "$LAME_SHA256"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mediadrop-ffmpeg.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT
PREFIX="$WORK_DIR/prefix"
mkdir -p "$PREFIX"

tar -xf "$SOURCE_CACHE/$LAME_ARCHIVE" -C "$WORK_DIR"
(
  cd "$WORK_DIR/lame-${LAME_VERSION}"
  ./configure \
    --prefix="$PREFIX" \
    --disable-shared \
    --enable-static \
    --disable-frontend
  make -j "$JOBS"
  make install
)

tar -xf "$SOURCE_CACHE/$FFMPEG_ARCHIVE" -C "$WORK_DIR"
(
  cd "$WORK_DIR/$FFMPEG_SOURCE_DIR"
  ./configure \
    --prefix=/usr/local \
    --arch=arm64 \
    --target-os=darwin \
    --cc=clang \
    --disable-autodetect \
    --disable-debug \
    --disable-doc \
    --disable-ffplay \
    --disable-ffprobe \
    --enable-gpl \
    --enable-libmp3lame \
    --enable-securetransport \
    --extra-cflags="-I../prefix/include -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET" \
    --extra-ldflags="-L../prefix/lib -mmacosx-version-min=$MACOSX_DEPLOYMENT_TARGET"
  make -j "$JOBS" ffmpeg
  cp ffmpeg "$BINARY_CACHE/ffmpeg"
)

chmod +x "$BINARY_CACHE/ffmpeg"
strip "$BINARY_CACHE/ffmpeg"
validate_binary "$BINARY_CACHE/ffmpeg"
cp "$BINARY_CACHE/ffmpeg" "$OUTPUT_PATH"
chmod +x "$OUTPUT_PATH"

echo "Built FFmpeg ${FFMPEG_VERSION} for macOS ARM64."
