FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    debian-keyring debian-archive-keyring apt-transport-https curl gnupg && \
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && \
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends caddy supervisor && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Node.js 22 (for the framer sidecar) ---
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/monarch/requirements.txt /tmp/monarch-requirements.txt
COPY services/todoist/requirements.txt /tmp/todoist-requirements.txt
COPY services/ga/requirements.txt /tmp/ga-requirements.txt
COPY services/gitlab/requirements.txt /tmp/gitlab-requirements.txt
COPY services/weather/requirements.txt /tmp/weather-requirements.txt
COPY services/trackiq/requirements.txt /tmp/trackiq-requirements.txt
COPY services/framer/requirements.txt /tmp/framer-requirements.txt
COPY services/pacvue/requirements.txt /tmp/pacvue-requirements.txt
COPY services/alpaca/requirements.txt /tmp/alpaca-requirements.txt
COPY services/ta/requirements.txt /tmp/ta-requirements.txt
COPY services/uw/requirements.txt /tmp/uw-requirements.txt
COPY services/datarova/requirements.txt /tmp/datarova-requirements.txt
COPY services/keepa/requirements.txt /tmp/keepa-requirements.txt
COPY services/scrapingbee/requirements.txt /tmp/scrapingbee-requirements.txt
COPY services/oxylabs/requirements.txt /tmp/oxylabs-requirements.txt

RUN pip install --no-cache-dir \
    -r /tmp/monarch-requirements.txt \
    -r /tmp/todoist-requirements.txt \
    -r /tmp/ga-requirements.txt \
    -r /tmp/gitlab-requirements.txt \
    -r /tmp/weather-requirements.txt \
    -r /tmp/trackiq-requirements.txt \
    -r /tmp/framer-requirements.txt \
    -r /tmp/pacvue-requirements.txt \
    -r /tmp/alpaca-requirements.txt \
    -r /tmp/ta-requirements.txt \
    -r /tmp/uw-requirements.txt \
    -r /tmp/datarova-requirements.txt \
    -r /tmp/keepa-requirements.txt \
    -r /tmp/scrapingbee-requirements.txt \
    -r /tmp/oxylabs-requirements.txt

COPY Caddyfile .
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY services/ services/

# --- Build the framer sidecar (TypeScript -> dist/) ---
RUN cd /app/services/framer && npm ci && npm run build && npm prune --omit=dev

EXPOSE 8080

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
