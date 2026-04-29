FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    debian-keyring debian-archive-keyring apt-transport-https curl gnupg && \
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg && \
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends caddy supervisor && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/monarch/requirements.txt /tmp/monarch-requirements.txt
COPY services/todoist/requirements.txt /tmp/todoist-requirements.txt
COPY services/ga/requirements.txt /tmp/ga-requirements.txt
COPY services/gitlab/requirements.txt /tmp/gitlab-requirements.txt
COPY services/weather/requirements.txt /tmp/weather-requirements.txt
COPY services/trackiq/requirements.txt /tmp/trackiq-requirements.txt

RUN pip install --no-cache-dir \
    -r /tmp/monarch-requirements.txt \
    -r /tmp/todoist-requirements.txt \
    -r /tmp/ga-requirements.txt \
    -r /tmp/gitlab-requirements.txt \
    -r /tmp/weather-requirements.txt \
    -r /tmp/trackiq-requirements.txt

COPY Caddyfile .
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY services/ services/

EXPOSE 8080

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
