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

# Fetch hashes
RUN wget2 "https://github.com/extrange/ibkr-docker/releases/download/${IB_GATEWAY_VERSION}-stable/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh.sha256" \
    -O hash

# Download IB Gateway (which contains TWS) and check hashes
RUN wget2 "https://github.com/extrange/ibkr-docker/releases/download/${IB_GATEWAY_VERSION}-stable/ibgateway-${IB_GATEWAY_VERSION}-standalone-linux-x64.sh" \
    -O "$INSTALL_FILENAME" \
    && sha256sum -c hash \
    && chmod +x "$INSTALL_FILENAME" \
    && yes '' | "./$INSTALL_FILENAME"  \
    && rm "$INSTALL_FILENAME"

# Copy scripts
COPY image-files/start.sh image-files/replace.sh /
COPY --from=python_builder /app/dist /src/dist

RUN mkdir -p ~/ibc && mv /opt/ibc/config.ini ~/ibc/config.ini && \
    chmod a+x ./*.sh /opt/ibc/*.sh /opt/ibc/scripts/*.sh && \
    python3 -m pip install --break-system-packages /src/dist/thetagang-*.whl && \
    rm -rf /src/dist

ENTRYPOINT [ "/start.sh" ]
