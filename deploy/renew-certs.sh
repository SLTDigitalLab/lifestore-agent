#!/bin/sh
# Invoke with sh; run from any directory on the Linux deployment host.
set -eu
cd "$(dirname "$0")/.."
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm certbot renew --webroot -w /var/www/certbot --non-interactive
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T nginx nginx -t
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T nginx nginx -s reload
