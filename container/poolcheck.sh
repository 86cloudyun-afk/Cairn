#!/bin/sh
# Print usable proxy URIs, one per line. Diagnostics go to stderr.
set -eu
script_dir=$(dirname "$(readlink -f "$0")")
helper="$script_dir/wrap.py"
if [ ! -f "$helper" ]; then
    helper=/usr/local/lib/cairn/wrap.py
fi
exec python3 "$helper" poolcheck "$@"
