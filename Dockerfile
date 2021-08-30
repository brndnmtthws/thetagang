FROM adoptopenjdk/openjdk11:debian

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
  openjfx \
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

ADD ./tws/Jts /root/Jts
ADD ./dist /src/dist
ADD entrypoint.bash /src/entrypoint.bash

RUN python3 -m pip install dist/thetagang-*.whl \
  && rm -rf /root/.cache \
  && rm -rf dist \
  && echo '--module-path /usr/share/openjfx/lib' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-modules=javafx.base,javafx.controls,javafx.fxml,javafx.graphics,javafx.media,javafx.swing,javafx.web' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.desktop/javax.swing=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.desktop/java.awt=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.base/java.util=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--illegal-access=permit' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '[Logon]' | tee -a /root/Jts/jts.ini \
  && echo 'UseSSL=true' | tee -a /root/Jts/jts.ini

ENTRYPOINT [ "/src/entrypoint.bash" ]
