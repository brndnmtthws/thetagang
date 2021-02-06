#!/bin/bash

set -e
set -x

Xvfb :1 -ac -screen 0 1024x768x24 &
export DISPLAY=:1

exec /usr/local/bin/thetagang "$@"
