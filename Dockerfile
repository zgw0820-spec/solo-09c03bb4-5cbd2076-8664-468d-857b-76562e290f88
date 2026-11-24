FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY samples ./samples
COPY docker-entrypoint.sh /usr/local/bin/bundle-import-entrypoint.sh
RUN chmod +x /usr/local/bin/bundle-import-entrypoint.sh

ENV REPO_PATH=/srv/repo.git \
    SAMPLES_DIR=/samples/bundles \
    SAMPLES_BAD_DIR=/samples/bad

EXPOSE 8000
ENTRYPOINT ["bundle-import-entrypoint.sh"]
