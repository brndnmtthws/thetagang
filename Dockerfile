FROM ubuntu:22.04

ENV JAVA_HOME="/opt/java/openjdk"
ENV VIRTUAL_ENV="/opt/venv"
ENV PATH="/opt/venv/bin:/root/.local/bin:${JAVA_HOME}/bin:${PATH}"

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
  unzip \
  wget \
  xdg-utils \
  xvfb \
  && case "$(dpkg --print-architecture)" in \
    amd64) java_arch=x64; java_sha=61c862e4fb08135dfed7f7abfa43e2b6a0a538fc6265f5431b3f5c3aadb41f58 ;; \
    arm64) java_arch=aarch64; java_sha=ffc3bfea2633518c6c9e47821a516e6b5bcb4569ac4890af3e79c211703caa75 ;; \
    *) echo "Unsupported architecture for the bundled TWS runtime" >&2; exit 1 ;; \
  esac \
  && wget -q "https://cdn.azul.com/zulu/bin/zulu25.38.109-ca-fx-jre25.0.5.0.3-linux_${java_arch}.tar.gz" -O java.tar.gz \
  && echo "$java_sha  java.tar.gz" | sha256sum -c - \
  && mkdir -p "$JAVA_HOME" \
  && tar -xzf java.tar.gz -C "$JAVA_HOME" --strip-components=1 \
  && rm java.tar.gz \
  && java --add-modules javafx.controls,javafx.web -version \
  && wget -q https://github.com/IbcAlpha/IBC/releases/download/3.24.2/IBCLinux-3.24.2.zip -O ibc.zip \
  && echo 'cc097ca1dfa75413a5fbe02e4743050af15f3308eb6a97c769b3cff9850c80c5  ibc.zip' | sha256sum -c - \
  && unzip ibc.zip -d /opt/ibc \
  && chmod o+x /opt/ibc/*.sh /opt/ibc/*/*.sh \
  && rm ibc.zip \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY ./tws/Jts/1053 /root/Jts/1053
ADD ./dist /src/dist
ADD entrypoint.bash /src/entrypoint.bash
ADD docker/patch-ibc-java-logging.sh /src/patch-ibc-java-logging.sh
ADD ./thetagang/ibgateway-log4j2.xml /opt/thetagang/ibgateway-log4j2.xml

RUN if [ "$(dpkg --print-architecture)" = "arm64" ]; then \
    wget -q https://us-maven.pkg.dev/jxbrowser/releases/com/teamdev/jxbrowser/jxbrowser-linux64-arm/9.5.3/jxbrowser-linux64-arm-9.5.3.jar -O jxbrowser-linux64-arm-9.5.3.jar \
      && echo '0c9f37b8bfe73cd31f4b0d890ad968b8276e8921dc5ed1e206c96a2c3221bd24  jxbrowser-linux64-arm-9.5.3.jar' | sha256sum -c - \
      && mv jxbrowser-linux64-arm-9.5.3.jar /root/Jts/1053/jars/; \
  fi \
  && wget -qO- https://astral.sh/uv/install.sh | sh \
  && uv python install 3.15 \
  && uv venv /opt/venv --python 3.15 \
  && uv pip install --python /opt/venv/bin/python dist/thetagang-*.whl \
  && rm -rf /root/.cache \
  && rm -rf dist \
  && printf '%s\n' "$JAVA_HOME" > /root/Jts/1053/.install4j/pref_jre.cfg \
  && printf '%s\n' \
    '--add-modules java.base,java.naming,java.management,javafx.base,javafx.controls,javafx.fxml,javafx.graphics,javafx.media,javafx.swing,javafx.web' \
    '--add-opens java.desktop/javax.swing=ALL-UNNAMED' \
    '--add-opens java.desktop/java.awt=ALL-UNNAMED' \
    '--add-opens java.base/java.util=ALL-UNNAMED' \
    '--add-opens javafx.graphics/com.sun.javafx.application=ALL-UNNAMED' \
    >> /root/Jts/1053/tws.vmoptions \
  && sh /src/patch-ibc-java-logging.sh \
  && echo '[Logon]' | tee -a /root/Jts/jts.ini \
  && echo 'UseSSL=true' | tee -a /root/Jts/jts.ini

ENTRYPOINT [ "/src/entrypoint.bash" ]
