#!/bin/sh
# Invoke explicitly as wrap.sh curl|wget ..., or through a curl/wget symlink.
set -eu
client=${0##*/}
script_dir=$(dirname "$(readlink -f "$0")")
helper="$script_dir/wrap.py"
if [ ! -f "$helper" ]; then
    helper=/usr/local/lib/cairn/wrap.py
fi
case "$client" in
    curl|wget) exec python3 "$helper" "$client" "$@" ;;
    *) exec python3 "$helper" "$@" ;;
esac
