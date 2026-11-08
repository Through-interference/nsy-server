#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
for script in "$root"/*.sh "$root"/scripts/*.sh; do bash -n "$script"; done
if command -v node >/dev/null 2>&1; then
 python3 -c "from nsy_server.viewer import LATEST_FRAME_SCRIPT as s; print(s.removeprefix('<script>').removesuffix('</script>'))" | node tests/test_browser_stream.cjs
else
 printf 'SKIP: Node.js required for the browser-parser test.\n'
fi
