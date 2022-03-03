#!/bin/bash

set -e
set -x

Xvfb :1 -ac -screen 0 1024x768x24 &
export DISPLAY=:1

# make sure jni path is set
export LD_LIBRARY_PATH="/usr/lib/$(arch)-linux-gnu/jni"

exec /usr/local/bin/thetagang "$@"
