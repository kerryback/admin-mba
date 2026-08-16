#!/usr/bin/env python3
"""Deploy a new BIAI AI Lab course instance.

Creates DNS records (DNSimple), Koyeb database + admin service,
and a DigitalOcean droplet with nspawn container setup.

Usage:
    python deploy-course.py mba --anthropic-key sk-ant-...
    python deploy-course.py mba --phase 4
    python deploy-course.py mba --dry-run
"""

import argparse
import json
import os
import re
import secrets as secrets_mod
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# kerrybackapps (pro plan). The BIAI services originally lived in
# kerryback-biai (starter), whose 5-custom-domain quota was full and whose one
# free Postgres slot was taken by aug-db.
KOYEB_ORG = "868a7fd9-9f02-4257-b314-4d78e044ba5f"
KOYEB_CNAME = f"{KOYEB_ORG}.cname.koyeb.app"

DNSIMPLE_ACCOUNT = 129999
DNSIMPLE_ZONE = "rice-business.org"

# qwen-lab-key,kerry-macbook — both, so the droplet stays reachable from either
DO_SSH_KEY = "55571350,56873206"
# Dedicated 16 vCPU / 64 GB / 400 GB, sized for ~120 containers (see the lab-mba
# repo's CLAUDE.md). The AMD gd- variant is offered in nyc1 (not nyc3) and is
# ~$90/mo cheaper than the nyc3 Intel equivalent. Requires the account's droplet
# size limit to be raised beyond the default 8 vCPU / 32 GB (done for this org).
DO_DROPLET_SIZE = "gd-16vcpu-64gb"
DO_DROPLET_REGION = "nyc1"
DO_DROPLET_IMAGE = "ubuntu-24-04-x64"

# kerrybackapps has no other database, so the free slot is available.
KOYEB_DB_INSTANCE = "free"

ADMIN_REPO = "github.com/kerryback/admin-mba"
LAB_REPO = Path(os.path.expanduser("~/repos/lab-mba"))

STATE_DIR = Path(os.path.expanduser("~/.biai-deploy"))

# Secrets are read from .env at runtime, never committed. The August version of
# this script had the Koyeb token as a literal in the source.
KOYEB_TOKEN = ""
DNSIMPLE_TOKEN = ""
DO_TOKEN = ""


def _read_env() -> dict:
    """Merge key=value pairs from ./.env and ~/.env (repo-local wins)."""
    values = {}
    for env_file in (
        Path(os.path.expanduser("~/.env")),
        Path(__file__).parent / ".env",
    ):
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def load_secrets():
    global KOYEB_TOKEN, DNSIMPLE_TOKEN, DO_TOKEN
    env = _read_env()
    missing = []

    DNSIMPLE_TOKEN = os.environ.get("DNSIMPLE_ACCESS_TOKEN") or env.get("DNSIMPLE_ACCESS_TOKEN", "")
    if not DNSIMPLE_TOKEN:
        missing.append("DNSIMPLE_ACCESS_TOKEN")

    # Every koyeb call here passes --organization, which only a *user* token can
    # act on: an org token (KOYEB_API_KEY) fails the context switch with
    # "your authentication token is invalid or expired". So prefer KOYEB_TOKEN.
    KOYEB_TOKEN = (
        os.environ.get("KOYEB_TOKEN")
        or env.get("KOYEB_TOKEN")
        or os.environ.get("KOYEB_API_KEY")
        or env.get("KOYEB_API_KEY", "")
    )
    if not KOYEB_TOKEN:
        missing.append("KOYEB_TOKEN (user token; org tokens cannot use --organization)")

    DO_TOKEN = (
        os.environ.get("DIGITALOCEAN_ACCESS_TOKEN")
        or os.environ.get("DIGITAL_OCEAN_TOKEN")
        or env.get("DIGITAL_OCEAN_TOKEN", "")
    )
    if not DO_TOKEN:
        missing.append("DIGITAL_OCEAN_TOKEN")
    else:
        # doctl reads this; setting it here means the caller need not export it.
        os.environ["DIGITALOCEAN_ACCESS_TOKEN"] = DO_TOKEN

    if missing:
        sys.exit(
            "ERROR: missing from environment or .env / ~/.env: " + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def state_path(mon: str) -> Path:
    return STATE_DIR / f"{mon}.json"


def load_state(mon: str) -> dict:
    p = state_path(mon)
    if p.exists():
        return json.loads(p.read_text())
    return {"month": mon, "phases_completed": []}


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(state["month"])
    p.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SECRET_ENV_KEYS = ("DATABASE_URL", "SECRET_KEY", "PROVISION_SECRET", "ANTHROPIC_API_KEY")


def _redact(cmd: str) -> str:
    """Keep tokens and secret env values out of printed commands and errors."""
    out = cmd
    for tok in (KOYEB_TOKEN, DNSIMPLE_TOKEN, DO_TOKEN):
        if tok:
            out = out.replace(tok, "***")
    # The service-create call passes the DB DSN (with its password) and the JWT
    # signing key on the command line; don't echo those into the terminal.
    for key in _SECRET_ENV_KEYS:
        out = re.sub(rf"({key}=)[^\"'\s]+", r"\1***", out)
    return out


def run(cmd: str, timeout: int = 120, capture: bool = True) -> str:
    print(f"  $ {_redact(cmd)}")
    result = subprocess.run(
        cmd, shell=True, capture_output=capture, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        err = result.stderr or result.stdout or ""
        raise RuntimeError(
            f"Command failed ({result.returncode}): {_redact(err.strip())}"
        )
    return (result.stdout or "").strip()


def ssh(ip: str, cmd: str, timeout: int = 120) -> str:
    full = f'ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@{ip} "{cmd}"'
    return run(full, timeout=timeout)


def scp(local: str, remote_ip: str, remote_path: str) -> str:
    return run(
        f"scp -r -o StrictHostKeyChecking=no {local} root@{remote_ip}:{remote_path}",
        timeout=300,
    )


def koyeb(args: str) -> str:
    return run(f"KOYEB_TOKEN={KOYEB_TOKEN} koyeb {args} --organization {KOYEB_ORG}")


def wait_for_ssh(ip: str, retries: int = 30, delay: int = 10):
    print(f"  Waiting for SSH on {ip}...")
    for i in range(retries):
        try:
            ssh(ip, "echo ok", timeout=15)
            print(f"  SSH ready.")
            return
        except Exception:
            if i < retries - 1:
                time.sleep(delay)
    raise RuntimeError(f"SSH not available on {ip} after {retries * delay}s")


def wait_for_dns(hostname: str, expected_ip: str, retries: int = 60, delay: int = 10):
    print(f"  Waiting for DNS: {hostname} -> {expected_ip}...")
    for i in range(retries):
        try:
            resolved = socket.gethostbyname(hostname)
            if resolved == expected_ip:
                print(f"  DNS resolved.")
                return
        except socket.gaierror:
            pass
        if i < retries - 1:
            time.sleep(delay)
    raise RuntimeError(f"DNS not resolved after {retries * delay}s")


# ---------------------------------------------------------------------------
# Phase 1: Create infrastructure
# ---------------------------------------------------------------------------
def phase1(state: dict, config: argparse.Namespace):
    mon = state["month"]
    print("\n=== Phase 1: Create infrastructure ===")

    # Generate secrets
    if not state.get("secret_key"):
        state["secret_key"] = secrets_mod.token_hex(32)
        save_state(state)
    if not state.get("provision_secret"):
        state["provision_secret"] = secrets_mod.token_urlsafe(32)
        save_state(state)

    # Create Koyeb database
    if not state.get("db_id"):
        # Idempotent: a rerun after a failure partway through this phase must not
        # trip over the database the previous attempt already created.
        try:
            koyeb(f"databases get {mon}-db/{mon}-db -o json")
            print(f"  Koyeb database {mon}-db already exists — reusing it.")
        except RuntimeError:
            print(f"  Creating Koyeb database: {mon}-db")
            koyeb(
                f"databases create {mon}-db --app {mon}-db "
                f"--instance-type {KOYEB_DB_INSTANCE} --pg-version 16 --region was"
            )
        state["db_name"] = f"{mon}-db"
        save_state(state)
        # Wait for it to become healthy.
        #
        # `koyeb databases get -o json` nests the status under "service" and
        # returns the DSN in a top-level "ConnectionStrings" list. The August
        # script looked for top-level "status" and "connection_uri", which this
        # CLI never emits — so the wait always timed out and the DATABASE_URL
        # was never captured. Read both shapes, preferring the real one.
        print("  Waiting for database to be healthy...")
        last_status = None
        for _ in range(30):
            time.sleep(10)
            try:
                data = json.loads(koyeb(f"databases get {mon}-db/{mon}-db -o json"))
            except Exception:
                continue

            svc = data.get("service") or {}
            last_status = svc.get("status") or data.get("status")
            if last_status != "HEALTHY":
                continue

            state["db_id"] = svc.get("id") or data.get("id", "unknown")

            conns = data.get("ConnectionStrings") or []
            conn = (conns[0] if conns else "") or data.get("connection_uri", "")
            if not conn:
                raise RuntimeError(
                    "Database is healthy but no connection string was returned; "
                    "fetch it with `koyeb databases get` and set database_url in "
                    f"{state_path(mon)} by hand."
                )
            state["database_url"] = conn if "?" in conn else conn + "?sslmode=require"

            save_state(state)
            print(f"  Database healthy: {state['db_id']}")
            break
        else:
            raise RuntimeError(
                f"Database did not become healthy in time (last status: {last_status})"
            )

    # Create DO droplet
    if not state.get("droplet_id"):
        print(f"  Creating DO droplet: lab-{mon}-biai")
        out = run(
            f"doctl compute droplet create lab-{mon}-biai "
            f"--size {DO_DROPLET_SIZE} "
            f"--image {DO_DROPLET_IMAGE} "
            f"--region {DO_DROPLET_REGION} "
            f"--ssh-keys {DO_SSH_KEY} "
            f"--enable-monitoring "
            f"--wait "
            f"--format ID,PublicIPv4 --no-header",
            timeout=300,
        )
        parts = out.split()
        state["droplet_id"] = parts[0]
        state["droplet_ip"] = parts[1]
        save_state(state)
        print(f"  Droplet created: {state['droplet_id']} at {state['droplet_ip']}")

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [1]))
    save_state(state)


# ---------------------------------------------------------------------------
# Phase 2: DNS records
# ---------------------------------------------------------------------------
def phase2(state: dict, config: argparse.Namespace):
    from dnsimple import Client
    from dnsimple.struct import ZoneRecordInput

    mon = state["month"]
    ip = state["droplet_ip"]
    print("\n=== Phase 2: DNS records ===")

    client = Client(access_token=DNSIMPLE_TOKEN)

    # A record for lab
    if not state.get("dns_a_record_id"):
        print(f"  Creating A record: lab-{mon}.{DNSIMPLE_ZONE} -> {ip}")
        resp = client.zones.create_record(
            DNSIMPLE_ACCOUNT, DNSIMPLE_ZONE,
            ZoneRecordInput(name=f"lab-{mon}", type="A", content=ip, ttl=300),
        )
        state["dns_a_record_id"] = resp.data.id
        save_state(state)

    # CNAME for admin
    if not state.get("dns_cname_record_id"):
        print(f"  Creating CNAME: admin-{mon}.{DNSIMPLE_ZONE} -> {KOYEB_CNAME}")
        resp = client.zones.create_record(
            DNSIMPLE_ACCOUNT, DNSIMPLE_ZONE,
            ZoneRecordInput(name=f"admin-{mon}", type="CNAME", content=KOYEB_CNAME, ttl=300),
        )
        state["dns_cname_record_id"] = resp.data.id
        save_state(state)

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [2]))
    save_state(state)


# ---------------------------------------------------------------------------
# Phase 3: Koyeb admin service
# ---------------------------------------------------------------------------
def phase3(state: dict, config: argparse.Namespace):
    mon = state["month"]
    lab_domain = f"lab-{mon}.{DNSIMPLE_ZONE}"
    print("\n=== Phase 3: Koyeb admin service ===")

    # `services create --app X` does not create app X; it fails with "Unable to
    # find the application". Create it first, idempotently.
    if not state.get("admin_app"):
        try:
            koyeb(f"apps get {mon}-admin -o json")
            print(f"  Koyeb app {mon}-admin already exists — reusing it.")
        except RuntimeError:
            print(f"  Creating Koyeb app: {mon}-admin")
            koyeb(f"apps create {mon}-admin")
        state["admin_app"] = f"{mon}-admin"
        save_state(state)

    if not state.get("admin_service"):
        print(f"  Creating Koyeb service: {mon}-admin")
        koyeb(
            f"services create {mon}-admin "
            f"--app {mon}-admin "
            f"--git {ADMIN_REPO} "
            f"--git-branch master "
            f"--git-builder docker "
            f"--instance-type eco-small "
            f"--region was "
            f"--port 8000:http "
            f"--route /:8000 "
            f'--env "DATABASE_URL={state["database_url"]}" '
            f'--env "SECRET_KEY={state["secret_key"]}" '
            f'--env "PROVISION_SECRET={state["provision_secret"]}" '
            f'--env "VM_PROVISION_URL=https://{lab_domain}/provision"'
        )
        state["admin_service"] = f"{mon}-admin"
        save_state(state)

    # Register custom domain.
    #
    # The flag is --attach-to, not --app. The August script used --app and
    # caught the resulting error as a "may already exist" warning, marking the
    # step done — so the admin panel silently stayed unreachable at its custom
    # domain. Only an already-exists error is tolerated here; anything else
    # fails the phase.
    if not state.get("admin_domain_registered"):
        admin_domain = f"admin-{mon}.{DNSIMPLE_ZONE}"
        print(f"  Registering custom domain: {admin_domain}")
        try:
            koyeb(f"domains create {admin_domain} --attach-to {mon}-admin")
        except RuntimeError as e:
            if "already exist" not in str(e).lower():
                raise
            print(f"  Domain {admin_domain} already registered — reusing it.")
        state["admin_domain_registered"] = True
        save_state(state)

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [3]))
    save_state(state)


# ---------------------------------------------------------------------------
# Phase 4: Provision droplet
# ---------------------------------------------------------------------------
def phase4(state: dict, config: argparse.Namespace):
    mon = state["month"]
    ip = state["droplet_ip"]
    lab_domain = f"lab-{mon}.{DNSIMPLE_ZONE}"
    print("\n=== Phase 4: Provision droplet ===")

    wait_for_ssh(ip)

    # Upload lab repo.
    #
    # This used to be `scp -r <repo>/ host:/opt/biai-vm/`. Unlike rsync, scp
    # ignores the trailing slash and copies the directory itself, so everything
    # landed in /opt/biai-vm/lab-mba/ and every later step that referenced
    # /opt/biai-vm/<script> failed. tar over ssh copies the *contents*, keeps
    # dotfiles like .claude, and leaves .git behind.
    if not state.get("repo_uploaded"):
        print("  Uploading lab repo to /opt/biai-vm/...")
        ssh(ip, "mkdir -p /opt/biai-vm")
        # COPYFILE_DISABLE stops macOS bsdtar from emitting an AppleDouble "._x"
        # sidecar next to every file. Those would be copied verbatim into each
        # student's ~/.claude/skills, putting a junk ._SKILL.md beside every
        # real one.
        run(
            f"COPYFILE_DISABLE=1 tar -C {LAB_REPO} --no-xattrs "
            f"--exclude=.git --exclude=__pycache__ --exclude='._*' -czf - . | "
            f"ssh -o StrictHostKeyChecking=no root@{ip} 'tar -C /opt/biai-vm -xzf -'",
            timeout=300,
        )
        ssh(ip, "find /opt/biai-vm -name '._*' -delete")
        # Fail loudly if the layout is not what later steps assume.
        ssh(ip, "test -f /opt/biai-vm/generate-nginx-nspawn.sh")
        # Parameterize the domain in generate-nginx-nspawn.sh. Matches whatever
        # lab-*.rice-business.org the repo was last pointed at — the old
        # hardcoded 'lab-june' pattern silently stopped matching once the repo
        # was forked, leaving the previous course's domain in the nginx config.
        print(f"  Setting domain to {lab_domain} in nginx generator...")
        pattern = r"lab-[a-z0-9]+\." + DNSIMPLE_ZONE.replace(".", r"\.")
        ssh(
            ip,
            f"sed -i -E 's/{pattern}/{lab_domain}/g' "
            f"/opt/biai-vm/generate-nginx-nspawn.sh",
        )
        ssh(ip, f"grep -c '{lab_domain}' /opt/biai-vm/generate-nginx-nspawn.sh")
        state["repo_uploaded"] = True
        save_state(state)

    # Create /etc/biai.env
    if not state.get("env_created"):
        print("  Creating /etc/biai.env...")
        anthropic_key = config.anthropic_key
        env_lines = [
            f"ANTHROPIC_API_KEY={anthropic_key}",
            f"DATABASE_URL={state['database_url']}",
            f"PROVISION_SECRET={state['provision_secret']}",
        ]
        env_content = "\\n".join(env_lines)
        ssh(ip, f"printf '{env_content}\\n' > /etc/biai.env && chmod 600 /etc/biai.env")
        state["env_created"] = True
        save_state(state)

    # Run server setup
    if not state.get("server_setup_done"):
        print("  Running setup-nspawn-server.sh (this takes 5-10 minutes)...")
        ssh(ip, "bash /opt/biai-vm/setup-nspawn-server.sh", timeout=900)
        state["server_setup_done"] = True
        save_state(state)

    # Install host dependencies for login app
    if not state.get("host_deps_installed"):
        print("  Installing host dependencies...")
        # login-app-nspawn.py also imports pandas, and Form uploads need
        # python-multipart; without them the service crash-looped on
        # ModuleNotFoundError and every page returned 502.
        ssh(
            ip,
            "pip3 install --break-system-packages psycopg2-binary fastapi "
            "uvicorn httpx pandas python-multipart openpyxl",
            timeout=300,
        )
        state["host_deps_installed"] = True
        save_state(state)

    # Create login app systemd service
    if not state.get("login_service_created"):
        print("  Creating biai-login systemd service...")
        unit = (
            "[Unit]\\n"
            "Description=BIAI Login App\\n"
            "After=network.target\\n"
            "[Service]\\n"
            "Type=simple\\n"
            "WorkingDirectory=/opt/biai-vm\\n"
            "EnvironmentFile=/etc/biai.env\\n"
            "ExecStart=/usr/bin/python3 -m uvicorn login-app-nspawn:app --host 127.0.0.1 --port 7900\\n"
            "Restart=on-failure\\n"
            "[Install]\\n"
            "WantedBy=multi-user.target"
        )
        ssh(ip, f"printf '{unit}\\n' > /etc/systemd/system/biai-login.service")
        ssh(ip, "systemctl daemon-reload && systemctl enable --now biai-login")
        state["login_service_created"] = True
        save_state(state)

    # NOTE: earlier versions created a biai-proxy service pointing at
    # /opt/biai-vm/api-proxy. That directory does not exist in the lab repo, so
    # the unit could never start (it is inactive on the August server too).
    # Dropped. Per-user usage is instead harvested from Claude Code transcripts
    # by biai-usage-harvester (below) — no MITM proxy.

    # Install the container watchdog (restarts containers that fall over)
    if not state.get("watchdog_installed"):
        print("  Installing container watchdog...")
        ssh(ip, "bash /opt/biai-vm/install-container-watchdog.sh", timeout=180)
        state["watchdog_installed"] = True
        save_state(state)

    # Install the usage harvester (fills meridian_usage_log for the admin panel)
    if not state.get("usage_harvester_installed"):
        print("  Installing usage harvester...")
        ssh(ip, "bash /opt/biai-vm/install-usage-harvester.sh", timeout=120)
        state["usage_harvester_installed"] = True
        save_state(state)

    # Generate initial nginx config
    if not state.get("nginx_configured"):
        print("  Generating initial nginx config...")
        ssh(ip, "touch /etc/biai-containers && bash /opt/biai-vm/generate-nginx-nspawn.sh")
        state["nginx_configured"] = True
        save_state(state)

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [4]))
    save_state(state)


# ---------------------------------------------------------------------------
# Phase 5: SSL
# ---------------------------------------------------------------------------
def phase5(state: dict, config: argparse.Namespace):
    mon = state["month"]
    ip = state["droplet_ip"]
    lab_domain = f"lab-{mon}.{DNSIMPLE_ZONE}"
    print("\n=== Phase 5: SSL ===")

    wait_for_dns(lab_domain, ip)

    if not state.get("ssl_done"):
        print(f"  Running certbot for {lab_domain}...")
        ssh(ip, "apt-get install -y -qq certbot python3-certbot-nginx", timeout=120)
        ssh(
            ip,
            f"certbot --nginx -d {lab_domain} --non-interactive --agree-tos "
            f"--email kerryback@gmail.com --redirect",
            timeout=120,
        )
        state["ssl_done"] = True
        save_state(state)

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [5]))
    save_state(state)


# ---------------------------------------------------------------------------
# Phase 6: Verification
# ---------------------------------------------------------------------------
def phase6(state: dict, config: argparse.Namespace):
    mon = state["month"]
    lab_domain = f"lab-{mon}.{DNSIMPLE_ZONE}"
    admin_domain = f"admin-{mon}.{DNSIMPLE_ZONE}"
    print("\n=== Phase 6: Verification ===")

    import urllib.request

    for url in [f"https://{lab_domain}", f"https://{admin_domain}"]:
        try:
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=15)
            print(f"  {url} -> {resp.status} OK")
        except Exception as e:
            print(f"  {url} -> FAILED: {e}")

    print(f"\n  Deployment complete!")
    print(f"  Admin panel: https://{admin_domain}")
    print(f"  AI Lab:      https://{lab_domain}")
    print(f"  SSH:         root@{state['droplet_ip']}")
    print(f"  State file:  {state_path(mon)}")

    state["phases_completed"] = list(set(state.get("phases_completed", []) + [6]))
    save_state(state)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
PHASES = {1: phase1, 2: phase2, 3: phase3, 4: phase4, 5: phase5, 6: phase6}


def main():
    parser = argparse.ArgumentParser(description="Deploy a BIAI AI Lab course instance")
    parser.add_argument("month", help="course code, 3 lowercase letters (e.g., mba)")
    parser.add_argument("--anthropic-key", help="Anthropic API key (prompted if not provided)")
    parser.add_argument("--phase", type=int, choices=range(1, 7), help="Run only this phase")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without executing")
    args = parser.parse_args()

    mon = args.month.lower()
    if len(mon) != 3:
        sys.exit("ERROR: course code must be 3 lowercase letters (e.g., mba)")

    load_secrets()

    state = load_state(mon)

    if args.dry_run:
        lab_domain = f"lab-{mon}.{DNSIMPLE_ZONE}"
        admin_domain = f"admin-{mon}.{DNSIMPLE_ZONE}"
        print(f"Dry run for month: {mon}")
        print(f"  Lab domain:   {lab_domain}")
        print(f"  Admin domain: {admin_domain}")
        print(f"  Koyeb DB:     {mon}-db")
        print(f"  Koyeb svc:    {mon}-admin (from {ADMIN_REPO})")
        print(f"  DO droplet:   lab-{mon}-biai ({DO_DROPLET_SIZE}, {DO_DROPLET_REGION})")
        print(f"  State file:   {state_path(mon)}")
        print(f"  Phases:       {', '.join(str(i) for i in range(1, 7))}")
        return

    print(f"Deploying BIAI course: {mon}")
    print(f"  Lab:   lab-{mon}.{DNSIMPLE_ZONE}")
    print(f"  Admin: admin-{mon}.{DNSIMPLE_ZONE}")

    # Prompt for Anthropic key if not provided and phase 4 is needed
    phases_to_run = [args.phase] if args.phase else [p for p in range(1, 7) if p not in state.get("phases_completed", [])]
    if not args.anthropic_key and 4 in phases_to_run:
        args.anthropic_key = input("Anthropic API key: ").strip()
        if not args.anthropic_key:
            sys.exit("ERROR: Anthropic API key is required")

    phases = phases_to_run

    if not phases:
        print("All phases already completed. Use --phase N to rerun a specific phase.")
        return

    for p in phases:
        PHASES[p](state, args)

    print("\nDone.")


if __name__ == "__main__":
    main()
