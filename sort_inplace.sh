#!/usr/bin/env bash

# Sorts a file in place, preserving permissions and ownership.
# Usage: sort_inplace.sh <file>
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <file>" >&2
    exit 1
fi

cat "$1" | LC_ALL=C sort | sponge "$1"