FROM adoptopenjdk/openjdk8:jdk8u292-b10-debian

RUN apt update \
  && DEBIAN_FRONTEND=noninteractive apt install -qy --no-install-recommends \
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
  && echo 'ffccc98102df7750b86a6b77308dcdc3965c4aff1ee7216ba7142cad67a292a0  ibc.zip' | tee ibc.zip.sha256 \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.7/IBCLinux-3.8.7.zip -o ibc.zip \
  && sha256sum -c ibc.zip.sha256 \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

ADD ./jfx/*.jar /opt/java/openjdk/lib
ADD ./tws/Jts /root/Jts
ADD ./dist /src/dist
ADD entrypoint.bash /src/entrypoint.bash

RUN python3 -m pip install dist/thetagang-*.whl \
  && rm -rf /root/.cache \
  && rm -rf dist

ENTRYPOINT [ "/src/entrypoint.bash" ]
