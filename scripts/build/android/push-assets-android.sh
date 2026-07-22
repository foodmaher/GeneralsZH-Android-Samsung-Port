#!/usr/bin/env bash
# Prepare and optionally push the opt-in Android RGBA8 texture overlay.
#
# Usage:
#   GX_ANDROID_TEXTURE_FALLBACK=rgba8 \
#   GX_ANDROID_TEXTURE_INPUT="/path/to/extracted GameData" \
#   GX_ANDROID_TEXTURE_OUTPUT="/path/to/generated overlay" \
#   GX_ANDROID_GAME_DATA="/sdcard/Download/GeneralsZH" \
#   ./scripts/build/android/push-assets-android.sh
#
# Pass --prepare-only to skip adb. Converted files are written below
# TextureFallback/RGBA8 and never overwrite the source tree or .big archives.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
CONVERTER="${PROJECT_ROOT}/scripts/tooling/assets/convert_dds_rgba8.py"

TEXTURE_PROFILE="${GX_ANDROID_TEXTURE_FALLBACK:-}"
INPUT_ROOT="${GX_ANDROID_TEXTURE_INPUT:-}"
OUTPUT_ROOT="${GX_ANDROID_TEXTURE_OUTPUT:-${PROJECT_ROOT}/out/android-texture-fallback}"
DEVICE_GAME_ROOT="${GX_ANDROID_GAME_DATA:-}"
ADB_SERIAL="${ANDROID_SERIAL:-}"
PREPARE_ONLY=0

usage() {
    cat <<'EOF'
Usage: push-assets-android.sh --texture-fallback rgba8 --input PATH [options]

Options:
  --texture-fallback PROFILE  Required; only "rgba8" is currently supported
  --input PATH                Directory containing legally extracted DDS files
  --output PATH               Generated overlay root
  --device-game-root PATH     Selected game folder on the Android device
  --serial SERIAL             adb device serial
  --prepare-only              Convert without invoking adb
  -h, --help                  Show this help

Environment equivalents:
  GX_ANDROID_TEXTURE_FALLBACK, GX_ANDROID_TEXTURE_INPUT,
  GX_ANDROID_TEXTURE_OUTPUT, GX_ANDROID_GAME_DATA, ANDROID_SERIAL
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --texture-fallback)
            [[ $# -ge 2 ]] || { echo "ERROR: --texture-fallback requires a value" >&2; exit 2; }
            TEXTURE_PROFILE="$2"
            shift 2
            ;;
        --input)
            [[ $# -ge 2 ]] || { echo "ERROR: --input requires a path" >&2; exit 2; }
            INPUT_ROOT="$2"
            shift 2
            ;;
        --output)
            [[ $# -ge 2 ]] || { echo "ERROR: --output requires a path" >&2; exit 2; }
            OUTPUT_ROOT="$2"
            shift 2
            ;;
        --device-game-root)
            [[ $# -ge 2 ]] || { echo "ERROR: --device-game-root requires a path" >&2; exit 2; }
            DEVICE_GAME_ROOT="$2"
            shift 2
            ;;
        --serial)
            [[ $# -ge 2 ]] || { echo "ERROR: --serial requires a value" >&2; exit 2; }
            ADB_SERIAL="$2"
            shift 2
            ;;
        --prepare-only)
            PREPARE_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument '$1'" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "${TEXTURE_PROFILE}" != "rgba8" ]]; then
    echo "ERROR: opt in with --texture-fallback rgba8 or GX_ANDROID_TEXTURE_FALLBACK=rgba8" >&2
    exit 2
fi
if [[ -z "${INPUT_ROOT}" ]]; then
    echo "ERROR: --input or GX_ANDROID_TEXTURE_INPUT is required" >&2
    exit 2
fi
if [[ ! -d "${INPUT_ROOT}" ]]; then
    echo "ERROR: input directory does not exist: ${INPUT_ROOT}" >&2
    exit 2
fi
if [[ ! -f "${CONVERTER}" ]]; then
    echo "ERROR: DDS converter not found: ${CONVERTER}" >&2
    exit 2
fi

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    else
        echo "ERROR: Python 3.10 or newer is required (python3/python not found on PATH)" >&2
        exit 2
    fi
fi

"${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "ERROR: ${PYTHON_BIN} must be Python 3.10 or newer" >&2
    exit 2
}

echo "==> Preparing Android texture profile: rgba8"
echo "    input : ${INPUT_ROOT}"
echo "    output: ${OUTPUT_ROOT}"
"${PYTHON_BIN}" "${CONVERTER}" --input "${INPUT_ROOT}" --output "${OUTPUT_ROOT}"

PROFILE_ROOT="${OUTPUT_ROOT%/}/TextureFallback/RGBA8"
[[ -f "${PROFILE_ROOT}/texture-fallback.cfg" ]] || {
    echo "ERROR: converter completed without producing the profile marker" >&2
    exit 1
}
[[ -f "${PROFILE_ROOT}/texture-fallback-manifest.json" ]] || {
    echo "ERROR: converter completed without producing the manifest" >&2
    exit 1
}

if [[ ${PREPARE_ONLY} -eq 1 ]]; then
    echo "==> Prepared overlay at ${PROFILE_ROOT} (adb push skipped)"
    exit 0
fi
if [[ -z "${DEVICE_GAME_ROOT}" ]]; then
    echo "ERROR: --device-game-root or GX_ANDROID_GAME_DATA is required unless --prepare-only is used" >&2
    exit 2
fi
command -v adb >/dev/null 2>&1 || {
    echo "ERROR: adb not found on PATH; install Android platform-tools or use --prepare-only" >&2
    exit 2
}

ADB=(adb)
if [[ -n "${ADB_SERIAL}" ]]; then
    ADB+=(-s "${ADB_SERIAL}")
fi
"${ADB[@]}" get-state >/dev/null

REMOTE_PROFILE_ROOT="${DEVICE_GAME_ROOT%/}/TextureFallback/RGBA8"
REMOTE_QUOTED="'${REMOTE_PROFILE_ROOT//\'/\'\\\'\'}'"
echo "==> Creating device overlay directory: ${REMOTE_PROFILE_ROOT}"
"${ADB[@]}" shell "mkdir -p -- ${REMOTE_QUOTED}"
echo "==> Pushing RGBA8 texture overlay"
"${ADB[@]}" push "${PROFILE_ROOT}/." "${REMOTE_PROFILE_ROOT}/"
echo "==> Installed texture profile rgba8 under ${REMOTE_PROFILE_ROOT}"
echo "    Relaunch the game and verify [GX_ANDROID_GRAPHICS] texture_profile=rgba8 in the log."
