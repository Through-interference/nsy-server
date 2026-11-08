#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
module=${1:?Usage: run-module.sh MODULE [ARGS...]}
shift
if [[ ! "$module" =~ ^[a-z][a-z0-9_]*$ ]]; then printf 'Invalid module name.\n' >&2; exit 2; fi
export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m "nsy_server.$module" "$@"
