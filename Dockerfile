FROM ubuntu:focal AS python-dependencies

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -qy \
  python3-pip \
  python3-dev \
  libffi-dev \
  libssl-dev \
  && pip3 install poetry \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src
ADD pyproject.toml .
ADD poetry.lock .

RUN poetry config cache-dir /src --local \
  && poetry install --no-dev \
  && yes | poetry cache clear . --all

FROM adoptopenjdk:8u275-b01-jdk-hotspot-focal

ADD ./tws/Jts /root/Jts

COPY --from=python-dependencies /root/.cache/pip /root/.cache/pip

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -qy \
  python3-pip \
  xvfb \
  libxi6 \
  libxtst6 \
  libxrender1 \
  unzip \
  curl \
  && pip3 install poetry \
  && echo 'c079e0ade7e95069e464859197498f0abb4ce277b2f101d7474df4826dcac837  ibc.zip' | tee ibc.zip.sha256 \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip -o ibc.zip \
  && sha256sum -c ibc.zip.sha256 \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY --from=python-dependencies /src /src

ADD . /src

ENTRYPOINT [ "/src/entrypoint.bash" ]
