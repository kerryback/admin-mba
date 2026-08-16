# BIAI Admin Panel & Course Deployment

## deploy-course skill

The `/deploy-course` skill automates the creation of a complete BIAI AI Lab course instance. Given a 3-letter course code (e.g., `mba`), it provisions all infrastructure across three providers and wires them together.

### What gets created

#### 1. DigitalOcean droplet (`lab-{code}-biai`)

An Ubuntu 24.04 VM running the AI Lab. Each student gets an isolated systemd-nspawn container with:
- Claude Code terminal (ttyd on port 9000)
- Plain bash terminal (ttyd on port 9001)
- File browser (FileBrowser on port 9002)
- Pre-installed skills (critique, demos, docx/pptx/xlsx tools)
- Anthropic API key injected directly into the container environment

The droplet also runs two host-level services:
- **nginx** — reverse proxy routing each student's subdirectory to their container, plus SSL via certbot
- **biai-login** (port 7900) — FastAPI login page that authenticates students via Linux PAM. Also exposes a `/provision` endpoint that the admin panel calls to create new users.

There is no longer a **biai-proxy**. The August deploy created that unit pointing
at `/opt/biai-vm/api-proxy`, a directory the lab repo does not contain, so it
never started. Per-user cost logging is therefore not wired up on this server —
`meridian_usage_log` will stay empty until a proxy is actually built.

#### 2. Koyeb PostgreSQL database (`{code}-db`)

A Postgres 16 database shared by the admin panel and the droplet services. Contains three tables:
- `users` — usernames, password hashes, VM passwords, spending limits
- `meridian_usage_log` — per-request Anthropic API token counts and costs
- `meridian_alerts` — spending threshold alerts

#### 3. Koyeb admin service (`{code}-admin`)

A FastAPI web app (this repo) deployed on Koyeb. Provides a Bootstrap dashboard for managing course participants:
- Add/delete users individually or via CSV upload
- Reset passwords
- Enable/disable accounts
- View per-user API token usage and costs

Auto-deploys from the `kerryback/admin-mba` GitHub repo on every push to master. Each course's service uses different environment variables but the same codebase.

#### 4. DNS records (DNSimple)

Two records on `rice-business.org`:
- `lab-{code}.rice-business.org` — A record pointing to the droplet IP
- `admin-{code}.rice-business.org` — CNAME pointing to the Koyeb service

### How the pieces connect

```
Student browser
    │
    ├── https://lab-{code}.rice-business.org
    │       │
    │       └── nginx (droplet)
    │            ├── / ──────────────► biai-login (port 7900)
    │            │                         │ authenticates via Linux PAM
    │            │                         │ reads is_admin from database
    │            ├── /{username}/ ───► nspawn container (port 9000, Claude Code)
    │            ├── /{username}/term/ ► nspawn container (port 9001, terminal)
    │            └── /{username}/files/ ► nspawn container (port 9002, file browser)
    │
    │   Claude Code (inside container)
    │       │
    │       └── ANTHROPIC_API_KEY ──► api.anthropic.com  (direct, unmetered)
    │
    └── https://admin-{code}.rice-business.org
            │
            └── Koyeb admin service
                    ├── reads/writes users table in database
                    └── POST /provision to droplet when creating a user
                            │
                            └── biai-login /provision endpoint
                                    ├── creates Linux user (for PAM auth)
                                    ├── runs setup-nspawn-user.sh (container + services)
                                    └── regenerates nginx config
```

### Adding a user (end-to-end flow)

1. Admin clicks "Create" in the admin panel
2. Admin panel inserts a row into the `users` table (username, bcrypt hash, plaintext VM password)
3. Admin panel POSTs username + password to `https://lab-{code}.rice-business.org/provision`
4. The login app on the droplet:
   - Creates a host Linux account with the password (for PAM login)
   - Clones the base nspawn template into a new container
   - Creates the user inside the container with workspace, Claude Code config, and skills
   - Starts ttyd, terminal, and file browser services
   - Regenerates nginx to add routes for the new user
5. Student can now log in at `https://lab-{code}.rice-business.org`

### Usage

```bash
# Full deployment (~20 minutes)
python deploy-course.py mba --anthropic-key sk-ant-...

# Dry run
python deploy-course.py mba --dry-run

# Rerun a specific phase after failure
python deploy-course.py mba --phase 4
```

### Phases

| Phase | What it does | Time |
|-------|-------------|------|
| 1 | Create Koyeb database + DO droplet + generate secrets | ~3-5 min |
| 2 | Create DNS records (A + CNAME) | seconds |
| 3 | Create Koyeb admin service with env vars + custom domain | ~1 min |
| 4 | Provision droplet (upload repo, setup nspawn, start services) | ~10-15 min |
| 5 | SSL certificate via certbot | ~1-5 min |
| 6 | Verify both URLs respond | seconds |

Progress is saved to `~/.biai-deploy/{code}.json` after each step, making the script safe to rerun after failures.
