FROM eclipse-temurin:17.0.8_7-jdk-jammy

RUN apt-get update \
  && DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends \
  ca-certificates \
  fonts-liberation \
  libasound2 \
  libatk-bridge2.0-0 \
  libatk1.0-0 \
  libatspi2.0-0 \
  libc6 \
  libcairo2 \
  libcups2 \
  libcurl4 \
  libdbus-1-3 \
  libdrm2 \
  libexpat1 \
  libgbm1 \
  libglib2.0-0 \
  libgtk-3-0 \
  libnspr4 \
  libnss3 \
  libpango-1.0-0 \
  libu2f-udev \
  libvulkan1 \
  libx11-6 \
  libxcb1 \
  libxcomposite1 \
  libxdamage1 \
  libxext6 \
  libxfixes3 \
  libxi6 \
  libxkbcommon0 \
  libxrandr2 \
  libxrender1 \
  libxtst6 \
  openjfx \
  python3-pip \
  python3-setuptools \
  unzip \
  wget \
  xdg-utils \
  xvfb \
  && if test "$(dpkg --print-architecture)" = "armhf" ; then python3 -m pip config set global.extra-index-url https://www.piwheels.org/simple ; fi \
  && echo 'a3f9b93ea1ff6740d2880760fb73e1a6e63b454f86fe6366779ebd9cd41c1542  ibc.zip' | tee ibc.zip.sha256 \
  && wget -q https://github.com/IbcAlpha/IBC/releases/download/3.20.0/IBCLinux-3.20.0.zip -O ibc.zip \
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
ADD ./data/jxbrowser-linux64-arm-7.29.jar /root/Jts/1037/jars/

RUN python3 -m pip install dist/thetagang-*.whl \
  && rm -rf /root/.cache \
  && rm -rf dist \
  && echo '--module-path /usr/share/openjfx/lib' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-modules java.base,java.naming,java.management,javafx.base,javafx.controls,javafx.fxml,javafx.graphics,javafx.media,javafx.swing,javafx.web' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.desktop/javax.swing=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.desktop/java.awt=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens java.base/java.util=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '--add-opens javafx.graphics/com.sun.javafx.application=ALL-UNNAMED' | tee -a /root/Jts/*/tws.vmoptions \
  && echo '[Logon]' | tee -a /root/Jts/jts.ini \
  && echo 'UseSSL=true' | tee -a /root/Jts/jts.ini

ENTRYPOINT [ "/src/entrypoint.bash" ]
