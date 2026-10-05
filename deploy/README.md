# LifeStore deployment

Run these commands from the repository root on a Linux server with Docker Engine
and Compose v2. `docker-compose.prod.yml` is a **standalone** file; do not merge
it with the development Compose file (which publishes API port 8000). Only Nginx
publishes ports; PostgreSQL and the API stay on the Compose network. Volumes
persist the database, ACME webroot, and certificates. The `certbot` service is an
on-demand helper in the `tls` profile.

This deploys account sign-in, saved conversations, shopping chat, and confirmed checkout. PayHere payments support **sandbox only**. Use HTTPS for the Secure session cookie and configure `PAYHERE_PUBLIC_BASE_URL` to match the application origin.

## Configure and initialize

```sh
cp deploy/.env.example .env.prod
chmod 600 .env.prod
```

Edit `.env.prod`. Set `DOMAIN` to a bare hostname (no scheme/path), set a strong
random database password and the matching `DATABASE_URL`, and supply provider
and sandbox merchant credentials. Hex passwords avoid URL-encoding ambiguity;
otherwise percent-encode reserved characters in the URL password. Keep
`NGINX_CONFIG=bootstrap` for initial issuance. Use the real HTTPS origin for
`PAYHERE_PUBLIC_BASE_URL`, with no path. Shell environment variables override
values in the env file, so clear stale deployment variables before running.

```sh
docker compose --env-file .env.prod -f docker-compose.prod.yml config --quiet
docker compose --env-file .env.prod -f docker-compose.prod.yml build api
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d db
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m app.db.schema
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.create_test_account
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d api nginx
```

The account command creates username `tester` and prints its generated password only on first creation. Save it privately; subsequent runs retain the password. Sign in at the HTTPS application origin.

Schema creation is idempotent, including `audit_logs`. It is not an upgrade
migration engine for future column changes. Run reviewed migrations before new
application versions. Do not load sample customers/orders into a real merchant
database. For a dedicated sandbox demo only, seed with:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm api python -m scripts.seed
```

Changing `POSTGRES_PASSWORD` does not update a database already initialized in
the volume; rotate the database role password and application URL together.
Never use `down -v` on a deployment containing data.

## TLS bootstrap and renewal

Point the domain's DNS A record at the server. Publish an AAAA record only if
IPv6 actually reaches it. Allow inbound TCP 80 and 443 through host/cloud
firewalls. The HTTP bootstrap serves ACME challenges and returns 503 for other
paths except `/health`; it intentionally serves no customer API traffic before TLS exists.

Replace the example hostname/email below with your values. The certificate name
must equal `DOMAIN`; Nginx uses that directory in the shared certificate volume.

```sh
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm certbot certonly --webroot -w /var/www/certbot --cert-name shop.example.com -d shop.example.com --email ops@example.com --agree-tos --non-interactive
```

Change `NGINX_CONFIG=https` in `.env.prod`, then:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --force-recreate nginx
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T nginx nginx -t
curl --fail https://shop.example.com/health
docker compose --env-file .env.prod -f docker-compose.prod.yml run --rm certbot renew --dry-run
```

Expect HTTP 200 and `{"status":"ok"}`. Do not use `curl -k` for launch checks.
Port 80 stays open for renewal and redirects other requests to HTTPS. Certbot's
webroot method and renewal behavior are described in the [official Certbot
guide](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot).

Install a host cron entry under an account with Docker access, replacing the
absolute project path. The script runs renewal, validates Nginx, and reloads it
to pick up renewed certificates; it stops on errors. Monitor the job's exit
status/output and certificate expiry. Keep the deployment directory path fixed.

```cron
17 3,15 * * * /bin/sh /opt/lifestore-agent/deploy/renew-certs.sh >> /var/log/lifestore-cert-renew.log 2>&1
```

Apply host log rotation to that log. Preserve the `letsencrypt` volume across
releases and protect its private keys. Re-run `renew --dry-run` after changes to
DNS, firewalls, or the proxy. Pin tested image digests for release reproducibility;
the supplied Nginx/Certbot tags track upstream updates when pulled.

## PayHere sandbox callback through ngrok

Use the development stack and a separate sandbox database for this exercise.
Configure ngrok authentication using your account's setup instructions, then run
`ngrok http 8000` against the running LifeStore API on localhost:8000. If another
application owns that port, run LifeStore on a free port and tunnel that port.
For example, after starting the development DB:

```sh
docker compose up -d db
docker compose run --rm -p 127.0.0.1:8001:8000 api
# In a second terminal:
ngrok http 8001
```

Set these values in the development `.env` using the actual ngrok HTTPS URL:

```dotenv
PAYHERE_MODE=sandbox
PAYHERE_MERCHANT_ID=<sandbox merchant ID>
PAYHERE_MERCHANT_SECRET=<sandbox app/domain secret>
PAYHERE_PUBLIC_BASE_URL=https://your-assigned-domain.ngrok.app
```

Recreate the API so it receives the updated environment (restart the `compose
run` command above, or use `docker compose up -d --force-recreate api` for the
standard stack). Register/authorize the HTTPS domain in the appropriate PayHere
merchant application settings and use its corresponding merchant secret.
The checkout response uses `Referrer-Policy: strict-origin`: PayHere receives
the origin needed for domain validation, while the signed URL's query is omitted.

The generated checkout form automatically sets:

```text
notify_url = https://your-assigned-domain.ngrok.app/webhooks/payhere
return_url = https://your-assigned-domain.ngrok.app/payments/payhere/return
cancel_url = https://your-assigned-domain.ngrok.app/payments/payhere/cancel
```

There is no separate `PAYHERE_NOTIFY_URL` variable. Keep the tunnel online, avoid
interactive authentication on the callback, and regenerate checkout links when
the tunnel origin changes. Use sandbox test cards only in the PayHere-hosted
form, never in chat. Verify the callback returns 200, changes the order to PAID,
and reduces stock once. A browser return alone must not change the order.
Inspect tunnel requests for delivery failures without publishing customer data
or signed payment links. See [ngrok setup](https://ngrok.com/docs/start) and
[PayHere Checkout API](https://support.payhere.lk/api-%26-mobile-sdk/checkout-api).

## Sandbox to live

| Setting | Sandbox | Live |
| --- | --- | --- |
| `PAYHERE_MODE` | `sandbox` | `live` after implementing and testing live-mode support |
| `PAYHERE_MERCHANT_ID` | Sandbox merchant ID | Approved live merchant ID |
| `PAYHERE_MERCHANT_SECRET` | Sandbox application secret | Live secret for the approved production domain |
| `PAYHERE_PUBLIC_BASE_URL` | Tunnel or sandbox HTTPS origin | Stable production HTTPS origin |
| `DOMAIN` | Sandbox hostname for TLS deployments | Production DNS hostname and matching certificate |
| `DATABASE_URL` / `POSTGRES_PASSWORD` | Isolated sandbox database | Production database and separate credentials |

**Current launch blocker:** `app/api/payhere.py:settings()` rejects live mode and
the form action is hardcoded to `https://sandbox.payhere.lk/pay/checkout`.
Before switching credentials, implement an explicit sandbox/live endpoint map
with rejection of unknown modes and tests preventing mode/endpoint mismatch.
The live endpoint is `https://www.payhere.lk/pay/checkout`, per the
[PayHere API reference](https://support.payhere.lk/api-%26-mobile-sdk/checkout-api).
Do not route live credentials to the sandbox endpoint. Keep signature verification
and server-side notification handling in both modes. Quiesce pending sandbox
payments before switching environments; use separate databases/deployments.

Provider keys do not change just because PayHere changes mode. OpenAI selection
is a separate application change gated by Phase 9; adding its key does not
activate it in the runtime Gemini → Groq chain.

## Backup and restore drill

Run backups on the Linux host; use PostgreSQL's custom archive format. This
backs up all application, audit, and checkpoint schemas in the database.

```sh
mkdir -p backups
chmod 700 backups
umask 077
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db pg_dump -U lifestore -d lifestore -Fc > backups/lifestore.dump
```

Check the exit code, use timestamped names in scheduled jobs, encrypt and copy
backups off-host, and define retention and recovery objectives. A Docker volume
alone is not a backup. Test restoration into a **new disposable database**:

```sh
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db createdb -U lifestore lifestore_restore_check
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db pg_restore -U lifestore -d lifestore_restore_check --exit-on-error --no-owner < backups/lifestore.dump
docker compose --env-file .env.prod -f docker-compose.prod.yml exec -T db psql -U lifestore -d lifestore_restore_check -c 'SELECT count(*) FROM audit_logs;'
```

Also inspect products/orders and checkpoint schemas in the restored database.
Remove only the disposable restore database after the drill. Back up before
upgrades; preserve a previous application image and a tested database rollback
plan. Rolling back an image does not reverse a schema migration.

## Pre-launch checklist

- [ ] Public DNS and trusted TLS work; `/health` returns 200 through Nginx.
- [ ] Renewal dry-run passes; twice-daily renewal/reload and expiry monitoring run.
- [ ] Only Nginx publishes ports; no public 5432 or 8000. API/DB health checks pass.
- [ ] `.env`/`.env.prod` are untracked, mode 600, absent from image build context;
  confirm `git check-ignore .env .env.prod` and `git ls-files '.env*'`. Commit only
  examples. Never paste rendered Compose configuration containing secrets.
- [ ] Scheduled encrypted off-host DB backups exist and restoration was tested.
- [ ] AuditLog's composite index exists in the deployed DB, not just in code:
  `SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='audit_logs' AND indexname='ix_audit_session_timestamp';`
  Expect an index on `(session_id, "timestamp")`. `python -m app.db.schema`
  creates it for a new table; for a pre-existing table missing the index, apply
  a reviewed migration (e.g. `CREATE INDEX CONCURRENTLY` outside a transaction).
- [ ] Audit/checkpoint retention, access controls, disk alerts, and log rotation
  are configured for customer contact/address data. Nginx omits query strings;
  production Uvicorn access logging is disabled to avoid payment-token logging.
- [ ] All 14 Phase 9 cases pass for the intended provider, with no skips; actual
  PayHere sandbox-card callback, duplicate callback, and forged-signature checks pass.
- [ ] Live-mode code blocker above is resolved and tested before live credentials
  are installed. Approved merchant/domain settings match the deployment.
- [ ] Verify sign-in/logout, conversation ownership, saved history, cart updates, order confirmation, and sandbox payment status on the deployed HTTPS origin.
- [ ] Monitoring covers failed callbacks/stock reconciliation, chargeback reviews,
  provider failures, database capacity, and backup/renewal jobs.
