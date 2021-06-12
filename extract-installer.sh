#!/bin/sh

set -e
set -x

mkdir -p tws
docker run -i --rm -v `pwd`/tws:/tws debian sh -c " \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends unzip curl ca-certificates \
    && echo '75f2c2071bfc342932d6bca4ec321783ecb115cb99d23290fa254a2b87200c63  tws-installer.sh' | tee tws-installer.sh.sha256 \
    && curl -qL https://download2.interactivebrokers.com/installers/tws/stable-standalone/tws-stable-standalone-linux-x64.sh -o tws-installer.sh \
    && yes '' | sh tws-installer.sh \
    && rm -f /root/Jts/*/uninstall \
    && cp -r /root/Jts /tws"

# && sha256sum -c tws-installer.sh.sha256 \
