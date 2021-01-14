FROM debian:bullseye

RUN apt-get update \
  && apt-get install -qy python3-pip xvfb libxrender1 openjfx unzip curl \
  && pip3 install --upgrade pip poetry \
  && echo '30ed8944d4df950e8802b0f127f6445cd23fbfd6ecc42b375fe222f6714669fa  tws-installer.sh' | tee tws-installer.sh.sha256 \
  && curl -qL https://download2.interactivebrokers.com/installers/tws/stable-standalone/tws-stable-standalone-linux-x64.sh -o tws-installer.sh \
  && sha256sum -c tws-installer.sh.sha256 \
  && echo 'c079e0ade7e95069e464859197498f0abb4ce277b2f101d7474df4826dcac837  ibc.zip' | tee ibc.zip.sha256 \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip -o ibc.zip \
  && sha256sum -c ibc.zip.sha256 \
  && yes "" | sh tws-installer.sh \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm tws-installer.sh ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

ADD . /src
WORKDIR /src

RUN poetry install

ENTRYPOINT [ "/src/entrypoint.bash" ]
