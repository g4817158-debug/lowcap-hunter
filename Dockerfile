FROM python:3.12-slim

# Node is needed for gmgn-cli
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g gmgn-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scanner.py /app/scanner.py

# gmgn-cli only works over IPv4: make Node prefer IPv4 DNS results
ENV NODE_OPTIONS="--dns-result-order=ipv4first"
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data

# START_MODE = probe (first run) or loop (normal). Change it in Railway variables.
CMD ["sh", "-c", "python scanner.py ${START_MODE:-probe}"]
