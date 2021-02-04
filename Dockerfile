FROM adoptopenjdk/openjdk8:jdk8u-ubuntu-nightly

ADD ./tws/Jts /root/Jts

RUN apt-get update \
  && apt-get install -qy \
  python3-pip \
  xvfb \
  libxi6 \
  libxtst6 \
  libxrender1 \
  unzip \
  curl \
  && pip3 install --no-cache-dir --upgrade pip poetry \
  && echo 'c079e0ade7e95069e464859197498f0abb4ce277b2f101d7474df4826dcac837  ibc.zip' | tee ibc.zip.sha256 \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip -o ibc.zip \
  && sha256sum -c ibc.zip.sha256 \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

ADD . /src
WORKDIR /src

RUN poetry install

ENTRYPOINT [ "/src/entrypoint.bash" ]
