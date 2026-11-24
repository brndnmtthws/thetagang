#!/bin/sh
set -eu

image=${1:?Usage: sh docker/smoke-tws.sh IMAGE}

smoke() (
    mode=$1
    container=$(docker run -d --network none --entrypoint /bin/sh "$image" -ec '
        Xvfb :1 -ac -screen 0 1024x768x24 &
        export DISPLAY=:1 LD_LIBRARY_PATH="/usr/lib/$(arch)-linux-gnu/jni"
        until [ -S /tmp/.X11-unix/X1 ]; do sleep 1; done

        set -- /root/Jts/*/tws.vmoptions
        test "$#" -eq 1
        test -f "$1"
        version=${1%/tws.vmoptions}
        set -- "${version##*/}"
        if [ "$0" = gateway ]; then set -- "$@" --gateway; fi
        printf "TradingMode=paper\n" > /tmp/ibc-smoke.ini
        exec bash /opt/ibc/scripts/ibcstart.sh "$@" \
            --ibc-ini=/tmp/ibc-smoke.ini --java-path="$JAVA_HOME/bin" --mode=paper
    ' "$mode")
    trap 'docker rm -f "$container" > /dev/null' EXIT

    seconds=0
    while [ "$seconds" -lt 90 ]; do
        logs=$(docker logs "$container" 2>&1)
        case "$logs" in
            *'Login dialog WINDOW_OPENED'*)
                printf '%s reached its login dialog\n' "$mode"
                return 0
                ;;
        esac
        if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then
            break
        fi
        sleep 1
        seconds=$((seconds + 1))
    done
    docker logs "$container" >&2
    printf '%s did not reach its login dialog\n' "$mode" >&2
    return 1
)

smoke tws
smoke gateway
