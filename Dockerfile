FROM debian:bullseye

RUN apt-get update \
  && apt-get install -qy python3-pip xvfb libxrender1 openjfx unzip curl \
  && pip3 install --upgrade pip poetry \
  && curl -qL https://download2.interactivebrokers.com/installers/tws/latest-standalone/tws-latest-standalone-linux-x64.sh -o /root/tws-installer.sh \
  && curl -qL https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip -o /root/ibc.zip \
  && yes "" | sh /root/tws-installer.sh \
  && unzip /root/ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm -rf /root/tws-installer.sh /root/ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

ADD . /src
WORKDIR /src

RUN poetry install

ENTRYPOINT [ "/src/entrypoint.bash" ]
