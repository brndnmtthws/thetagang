FROM debian:bullseye

RUN apt-get update \
  && apt-get install -qy python3-pip xvfb libxrender1 openjfx unzip \
  && pip3 install --upgrade pip poetry \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

ADD https://download2.interactivebrokers.com/installers/tws/latest-standalone/tws-latest-standalone-linux-x64.sh /root/tws-installer.sh
ADD https://github.com/IbcAlpha/IBC/releases/download/3.8.4-beta.2/IBCLinux-3.8.4-beta.2.zip /root/ibc.zip

RUN yes "" | sh /root/tws-installer.sh \
  && unzip /root/ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm -rf /root/tws-installer.sh /root/ibc.zip

ADD . /src
WORKDIR /src

RUN poetry install

ENTRYPOINT [ "/src/entrypoint.bash" ]
