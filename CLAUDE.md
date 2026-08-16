# BIAI Admin Service (MBA)

Centralized user management for the BIAI MBA course. This FastAPI app runs on Koyeb and is the single place to add, remove, and manage users who access the AI Lab.

Forked from `kerryback-biai/admin-aug`. Same codebase, its own database and its own droplet.

## The two services

### 1. This admin panel (Koyeb — this repo)
- **Repo:** `kerryback/admin-mba` → `~/repos/admin-mba`
- **URL:** https://admin-mba.rice-business.org
- **Instance:** Koyeb app `mba-admin`, auto-deploys from `master` on push
- **Stack:** FastAPI + psycopg2 + Bootstrap 5 (Flatly theme)
- **Login:** a fixed list of admin accounts in `app/auth/routes.py` — *not* the
  `users` table. `is_admin` on a row in `users` does nothing for panel sign-in;
  it is only read by other parts of the app.

The account list comes from `ADMIN_USERS` ("username:Display Name", comma
separated) and they all share one password, `ADMIN_PASSWORD`. Both are
environment variables on the Koyeb service. If `ADMIN_PASSWORD` is unset nobody
can sign in — there is deliberately no fallback default.

In the August version that password was a literal in `app/auth/routes.py`,
shared by four named accounts, so anyone who could read the repo could sign in
to the live panel. That is why it moved to the environment here. The August
panel still has it hardcoded — that repo must stay private.

### 2. AI Lab (DigitalOcean Linux VM)
- **Repo:** `kerryback/lab-mba` → `~/repos/lab-mba`
- **URL:** https://lab-mba.rice-business.org
- **What it does:** Multi-user Claude Code workspace using systemd-nspawn containers. Each user gets an isolated container with ttyd terminal and filebrowser behind nginx.
- **Provisioning:** `fetch-students.py` queries the shared database for active users with a `vm_password` set. `setup-nspawn-user.sh` creates per-user containers.
- **Sizing:** created small and resized before students are added — see that repo's CLAUDE.md.

## Shared database

Both services share the **mba-db** PostgreSQL database on Koyeb (Postgres 16, `was` region). This is separate from aug-db; the two courses share no rows.

### `users` table
| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| username | TEXT UNIQUE | Login name (e.g. `kerry_back`) |
| password_hash | TEXT | bcrypt hash for web login (admin panel) |
| name | TEXT | Display name |
| is_admin | BOOLEAN | Does *not* control admin-panel access — see "Login" above |
| is_active | BOOLEAN | Disabled users can't sign in anywhere |
| spending_limit_cents | INTEGER | Per-user Anthropic API spend cap (default $10) |
| vm_password | TEXT | Plaintext password for Linux `chpasswd` on the VM |
| created_at | TIMESTAMPTZ | |

### Other tables
- **meridian_usage_log** — per-turn token/cost tracking, one row per Claude Code
  assistant turn. Written by `usage-harvester.py` on the lab droplet (a systemd
  timer that tails Claude Code transcripts and inserts rows) — see the lab
  repo's CLAUDE.md. The admin panel's usage view reads this.
- **meridian_alerts** — spending alerts (unused).

Note: this was empty in every prior course. The intended `biai-proxy` writer
never ran (it pointed at a directory the lab repo doesn't contain) and has been
dropped. The harvester replaces it: containers still call `api.anthropic.com`
directly (no proxy), and usage is logged after the fact from the transcripts.
This is **visibility only** — it does not enforce `spending_limit_cents` in real
time; a hard cap would need an actual proxy.

## First sign-in

No database bootstrap is needed — panel sign-in does not read the `users` table
at all. Set `ADMIN_PASSWORD` on the Koyeb service and sign in as one of the
`ADMIN_USERS`:

```bash
ORG=868a7fd9-9f02-4257-b314-4d78e044ba5f
koyeb services update mba-admin/mba-admin --env ADMIN_PASSWORD=<password> --organization $ORG
```

`mba-db` still starts empty, so the panel will show no course participants until
you add them. The app's `init_db` creates the tables on first boot.

## Deployment secrets

`deploy-course.py` reads its tokens from `~/.env` (or a repo-local `.env`) at
runtime — `DNSIMPLE_ACCESS_TOKEN`, `KOYEB_TOKEN`, `DIGITAL_OCEAN_TOKEN`. The
August version of the script had the Koyeb token as a literal in the source;
that is deliberately not carried over, and `.env` is gitignored.

## Koyeb organization

Unlike the August and June courses, this one runs in **kerrybackapps**
(`868a7fd9-…`), not kerryback-biai. kerryback-biai is on the starter plan, where
both limits that matter were already spent: all 5 custom domains were in use, and
its single free Postgres slot belongs to aug-db. kerrybackapps is on pro, has
domain headroom, and had no database at all — so mba-db is free there.

Two consequences worth remembering:

- **The GitHub repo had to move too.** Koyeb's GitHub integration for
  kerrybackapps reaches the personal `kerryback` account, not the
  `kerryback-biai` GitHub org. Builds from `kerryback-biai/admin-mba` failed with
  "Failed to get the SHA of the commit", so this repo was transferred to
  `kerryback/admin-mba` — matching `kerryback/lab-mba`.
- **Use the user token, not the org token.** Every `koyeb` call passes
  `--organization`, which only a *user* token can act on. `KOYEB_API_KEY` is an
  org token and fails with "your authentication token is invalid or expired";
  `KOYEB_TOKEN` is the user token and works:
  `koyeb services list --organization 868a7fd9-9f02-4257-b314-4d78e044ba5f`.

## Environment variables (Koyeb)
- `DATABASE_URL` — Postgres connection string for mba-db
- `SECRET_KEY` — JWT signing key (HS256, 24h expiry)
- `PROVISION_SECRET` — shared secret for the lab droplet's `/provision` endpoint
- `VM_PROVISION_URL` — `https://lab-mba.rice-business.org/provision`
- `ADMIN_PASSWORD` — shared password for admin-panel sign-in (no default; unset means nobody can sign in)
- `ADMIN_USERS` — optional override of the admin account list ("username:Display Name", comma separated)

The first four are set by `deploy-course.py` phase 3 and recorded in `~/.biai-deploy/mba.json`.

## Project structure
```
app/
  main.py              # FastAPI app, lifespan (init_db)
  config.py            # Settings via pydantic-settings
  auth/
    routes.py          # POST /api/auth/login (admin-only)
    dependencies.py    # JWT, bcrypt, get_current_user
  admin/
    routes.py          # CRUD: /api/admin/users, /api/admin/usage
    dependencies.py    # require_admin dependency
  database/
    db.py              # All SQL queries, connection management
  static/
    login.html         # Rice blue/gray login page
    index.html         # Bootstrap admin dashboard
    js/admin.js        # Dashboard logic
Dockerfile             # python:3.12-slim + uvicorn on port 8000
deploy-course.py       # Six-phase deploy: DB, droplet, DNS, Koyeb service, SSL
```
