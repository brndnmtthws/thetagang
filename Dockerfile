#######################################################
# Python builder thetagang python package
FROM python:3.11-buster AS python_builder

RUN pip install poetry==1.4.2

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

WORKDIR /app

COPY . /app/

# generate /dist folder
RUN poetry build

#######################################################
# IBKR builder
FROM ubuntu:22.04 AS ibkr_builder

ENV IB_GATEWAY_VERSION=10.30.1r
ENV IB_GATEWAY_RELEASE_CHANNEL=stable
ENV IBC_VERSION=3.20.0

WORKDIR /tmp/setup

# Prepare system
RUN apt-get update -y && \
  DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes \
  curl \
  ca-certificates \
  unzip && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/* && \
  if [ "$(uname -m)" = "aarch64" ]; then \
    export URL="https://download.bell-sw.com/java/11.0.22+12/bellsoft-jre11.0.22+12-linux-aarch64-full.tar.gz" ; \
    export ARCHIVE_NAME="bellsoft-jre11.0.22+12-linux-aarch64-full.tar.gz" ; \
    export JVM_DIR="jre-11.0.22-full" ; \
    curl -sSOL $URL ; \
    tar -xvzf $ARCHIVE_NAME; \
    mv $JVM_DIR /opt/java ; \
    export JAVA_HOME=/opt/java ; \
    export PATH=$JAVA_HOME/bin:$PATH ; \
    fi && \
  # Install IB Gateway
  # Use this instead of "RUN curl .." to install a local file:
  curl -sSOL https://github.com/gnzsnz/ib-gateway-docker/releases/download/ibgateway-${IB_GATEWAY_RELEASE_CHANNEL}%40${IB_GATEWAY_VERSION}/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh && \
  curl -sSOL https://github.com/gnzsnz/ib-gateway-docker/releases/download/ibgateway-${IB_GATEWAY_RELEASE_CHANNEL}%40${IB_GATEWAY_VERSION}/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh.sha256 && \
  sha256sum --check ./ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh.sha256 && \
  chmod a+x ./ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh && \
  if [ "$(uname -m)" = "aarch64" ]; then \
    sed -i 's/-Djava.ext.dirs="$app_java_home\/lib\/ext:$app_java_home\/jre\/lib\/ext"/--add-modules=ALL-MODULE-PATH/g' "ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh" ; \
    app_java_home=/opt/java ./ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh -q -dir /root/Jts/ibgateway/1030 ; \
  else \
    ./ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh -q -dir /root/Jts/ibgateway/1030 ; \
  fi && \
  # Install IBC
  curl -sSOL https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip && \
  mkdir /opt/ibc && \
  unzip ./IBCLinux-${IBC_VERSION}.zip -d /opt/ibc && \
  chmod -R u+x /opt/ibc/*.sh && \
  chmod -R u+x /opt/ibc/scripts/*.sh

RUN rm -rf /root/.cache && \
  echo '--module-path /usr/share/openjfx/lib' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '--add-modules java.base,java.naming,java.management,javafx.base,javafx.controls,javafx.fxml,javafx.graphics,javafx.media,javafx.swing,javafx.web' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '--add-opens java.desktop/javax.swing=ALL-UNNAMED' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '--add-opens java.desktop/java.awt=ALL-UNNAMED' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '--add-opens java.base/java.util=ALL-UNNAMED' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '--add-opens javafx.graphics/com.sun.javafx.application=ALL-UNNAMED' | tee -a /root/Jts/ibgateway/*/ibgateway.vmoptions && \
  echo '[Logon]' | tee -a /root/Jts/jts.ini && \
  echo 'UseSSL=true' | tee -a /root/Jts/jts.ini

#######################################################
# Build final production image
FROM eclipse-temurin:17.0.8_7-jdk-jammy

RUN apt-get update && \
  DEBIAN_FRONTEND=noninteractive apt-get install -qy --no-install-recommends \
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
  xvfb && \
  if test "$(dpkg --print-architecture)" = "armhf" ; then python3 -m pip config set global.extra-index-url https://www.piwheels.org/simple ; fi && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY --from=python_builder /app/dist /src/dist
COPY --from=ibkr_builder /opt/ibc /opt/ibc
COPY --from=ibkr_builder /root/Jts /root/Jts
ADD entrypoint.bash /src/entrypoint.bash
ADD ./data/jxbrowser-linux64-arm-7.29.jar /root/Jts/ibgateway/1030/jars/

RUN python3 -m pip install dist/thetagang-*.whl && \
  rm -rf dist

ENTRYPOINT [ "/src/entrypoint.bash" ]
