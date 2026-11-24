#!/bin/sh

set -e
set -x

mkdir -p tws
docker run --platform linux/amd64 --rm -v "$(pwd)/tws:/tws" debian sh -ec '
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends unzip curl ca-certificates
    curl -fsSL https://download2.interactivebrokers.com/installers/tws/latest-standalone/tws-latest-standalone-linux-x64.sh -o tws-installer.sh
    echo "1ddd0d58c59b997049f1cd6eb6115c045c01569f8ed69a764dd69ccb8a63df8d  tws-installer.sh" | sha256sum -c -
    yes "" | sh tws-installer.sh
    test -f /root/Jts/1053/tws.vmoptions
    rm -f /root/Jts/1053/uninstall
    rm -rf /root/Jts/1053/jre
    mkdir -p /tws/Jts
    cp -r /root/Jts/1053 /tws/Jts
'
