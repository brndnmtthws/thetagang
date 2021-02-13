FROM adoptopenjdk/openjdk8:jdk8u262-b10-debian

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends \
  python3-pip \
  python3-setuptools \
  xvfb \
  libxi6 \
  libxtst6 \
  libxrender1 \
  unzip \
  curl \
  && python3 -m pip install --upgrade pip \
  && if test "$(dpkg --print-architecture)" = "armhf" ; then python3 -m pip config set global.extra-index-url https://www.piwheels.org/simple ; fi \
  && echo 'c079e0ade7e95069e464859197498f0abb4ce277b2f101d7474df4826dcac837  ibc.zip' | tee ibc.zip.sha256 \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip -o ibc.zip \
  && sha256sum -c ibc.zip.sha256 \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

ADD ./tws/Jts /root/Jts
ADD ./dist /src/dist
ADD entrypoint.bash /src/entrypoint.bash

RUN python3 -m pip install dist/thetagang-*.whl \
  && rm -rf /root/.cache \
  && rm -rf dist

ENTRYPOINT [ "/src/entrypoint.bash" ]
