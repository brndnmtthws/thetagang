#!/bin/sh

set -e
set -x

mkdir -p tws
docker run -i --rm -v `pwd`/tws:/tws debian sh -c " \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends unzip curl ca-certificates \
    && echo '45657c8a48c5d477a17e37e37aee3f0b7ba041fed17e6068253bef03c6b5d772  tws-installer.sh' | tee tws-installer.sh.sha256 \
    && curl -qL https://download2.interactivebrokers.com/installers/tws/stable-standalone/tws-stable-standalone-linux-x64.sh -o tws-installer.sh \
    && yes '' | sh tws-installer.sh \
    && rm -f /root/Jts/*/uninstall \
    && cp -r /root/Jts /tws"

# && sha256sum -c tws-installer.sh.sha256 \
