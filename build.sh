#!/usr/bin/env bash
# Build the native engine into bot/_engine*.so, plus the standalone
# engine_tests binary.
#
#   ./build.sh              incremental RelWithDebInfo build
#   ./build.sh --sanitize   build engine_tests with ASan + UBSan (leaves bot/ alone)
#   ./build.sh --clean      wipe the build directories first
set -euo pipefail

cd "$(dirname "$0")"

PY=.venv/bin/python
BUILD_DIR=build
BUILD_TYPE=RelWithDebInfo
EXTRA=()
TARGET=()

for arg in "$@"; do
  case "$arg" in
    --sanitize)
      # Sanitizers only apply to the standalone binary. The extension module is
      # never instrumented, so bot/ is not touched by this mode at all.
      BUILD_DIR=build-sanitize
      EXTRA+=(-DENGINE_SANITIZE=ON)
      TARGET+=(--target engine_tests)
      ;;
    --clean)
      rm -rf build build-sanitize
      ;;
    -h|--help)
      sed -n '2,7p' "$0"
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PY" ]]; then
  echo "No virtualenv at .venv. Create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

if ! "$PY" -c 'import pybind11' 2>/dev/null; then
  echo "Missing pybind11 in the venv. Install it with:" >&2
  echo "  .venv/bin/pip install pybind11" >&2
  exit 1
fi

cmake -S . -B "$BUILD_DIR" -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DPython3_EXECUTABLE="$PWD/$PY" \
  ${EXTRA[@]+"${EXTRA[@]}"}

cmake --build "$BUILD_DIR" ${TARGET[@]+"${TARGET[@]}"}

echo
if (( ${#TARGET[@]} )); then
  echo "built: $BUILD_DIR/engine_tests"
else
  echo "built: $(ls -1 bot/_engine*.so 2>/dev/null || echo 'NOT FOUND')"
fi
