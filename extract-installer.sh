#!/bin/sh

set -e
set -x

mkdir -p tws
docker run -i --rm -v `pwd`/tws:/tws debian sh -c " \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends unzip curl ca-certificates \
    && echo '09a2d39a1f5727346f43959b6c52b7becd1e4dd42b5ab907dd7d29cd51a023df  tws-installer.sh' | tee tws-installer.sh.sha256 \
    && curl -qL https://download2.interactivebrokers.com/installers/tws/stable-standalone/tws-stable-standalone-linux-x64.sh -o tws-installer.sh \
    && sha256sum -c tws-installer.sh.sha256 \
    && yes "" | sh tws-installer.sh \
    && chmod 644 /root/Jts/978/uninstall \
    && cp -r /root/Jts /tws"
