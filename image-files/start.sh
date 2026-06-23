#!/bin/bash

# fail fast
set -Eeuo pipefail

Xvfb :1 -ac -screen 0 1920x1080x24 &

export DISPLAY=:1

./replace.sh ~/ibc/config.ini

exec /usr/local/bin/thetagang "$@"
