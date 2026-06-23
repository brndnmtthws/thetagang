#######################################################
# Python builder thetagang python package
FROM python:3.14-bookworm AS python_builder

RUN pip install uv

WORKDIR /app

COPY . /app/

# generate /dist folder
RUN uv build

FROM debian:12 AS setup

ENV IBC_VERSION=3.24.0
ENV IB_GATEWAY_VERSION=10.45.1g

RUN apt-get update && \
    apt-get install --no-install-recommends -y \
    ca-certificates \
    git \
    libxtst6 \
    libgtk-3-0 \
    xvfb \
    procps \
    python3 \
    python3-pip \
    socat \
    unzip \
    wget2 \
    xterm \
    libasound2 \
    libnss3 \
    libgbm1 \
    libnspr4

# Download and setup IBC
RUN wget2 https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip -O ibc.zip \
    && unzip ibc.zip -d /opt/ibc \
    && rm ibc.zip

ENV INSTALL_FILENAME="ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh"

# IB Gateway ships only an x64 JRE, so the bundled JRE can't run on arm64. On
# aarch64 we install Azul Zulu's JavaFX-enabled JRE and point the installer at
# it via app_java_home (the JRE stays in the image for the gateway to use at
# runtime). On amd64 the installer's bundled JRE is used as-is.
ARG ZULU_NAME=zulu17.60.17-ca-fx-jre17.0.16-linux_aarch64
ARG ZULU_FILE=${ZULU_NAME}.tar.gz
ARG ZULU_URL=https://cdn.azul.com/zulu/bin/${ZULU_FILE}

RUN if [ "$(uname -m)" = "aarch64" ]; then \
        wget2 "${ZULU_URL}" -O "${ZULU_FILE}" \
        && tar -xzf "${ZULU_FILE}" -C /usr/local/ \
        && ln -s "/usr/local/${ZULU_NAME}" /usr/local/zulu17 \
        && rm "${ZULU_FILE}"; \
    fi

# Fetch hashes
RUN wget2 "https://github.com/extrange/ibkr-docker/releases/download/${IB_GATEWAY_VERSION}-stable/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh.sha256" \
    -O hash

# Download IB Gateway (which contains TWS) and check hashes
RUN wget2 "https://github.com/extrange/ibkr-docker/releases/download/${IB_GATEWAY_VERSION}-stable/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh" \
    -O "$INSTALL_FILENAME" \
    && sha256sum -c hash \
    && chmod +x "$INSTALL_FILENAME" \
    && if [ "$(uname -m)" = "aarch64" ]; then \
        yes '' | app_java_home=/usr/local/zulu17 "./$INSTALL_FILENAME"; \
    else \
        yes '' | "./$INSTALL_FILENAME"; \
    fi \
    && rm "$INSTALL_FILENAME"

# Copy scripts
COPY image-files/start.sh image-files/replace.sh /
COPY --from=python_builder /app/dist /src/dist

RUN mkdir -p ~/ibc && mv /opt/ibc/config.ini ~/ibc/config.ini && \
    chmod a+x ./*.sh /opt/ibc/*.sh /opt/ibc/scripts/*.sh && \
    python3 -m pip install --break-system-packages /src/dist/thetagang-*.whl && \
    rm -rf /src/dist

ENTRYPOINT [ "/start.sh" ]
