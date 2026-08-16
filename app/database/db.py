import psycopg2
import psycopg2.extras
from contextlib import contextmanager

from app.config import settings


def get_connection():
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    name TEXT DEFAULT '',
                    password_hash TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_active BOOLEAN DEFAULT TRUE,
                    spending_limit_cents INTEGER DEFAULT 1000,
                    vm_password TEXT,
                    code_server_port INTEGER,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS meridian_usage_log (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    created_at TIMESTAMPTZ DEFAULT now(),
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0,
                    model TEXT,
                    cost_cents REAL DEFAULT 0,
                    tool_calls INTEGER DEFAULT 0,
                    request_type TEXT DEFAULT 'ai-lab'
                );
                CREATE TABLE IF NOT EXISTS meridian_alerts (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    alert_type TEXT,
                    message TEXT,
                    acknowledged BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
            """)


def get_user_by_username(username: str) -> dict | None:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_users() -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.id, u.username, u.name, u.is_active,
                       u.created_at, u.code_server_port,
                       COALESCE(SUM(l.cost_cents), 0) as total_cost_cents,
                       COALESCE(SUM(l.input_tokens), 0) as total_input,
                       COALESCE(SUM(l.output_tokens), 0) as total_output
                FROM users u
                LEFT JOIN meridian_usage_log l ON u.id = l.user_id
                GROUP BY u.id
                ORDER BY u.created_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]


def create_user(username: str, password_hash: str, name: str = "",
                is_admin: bool = False, spending_limit_cents: int | None = None,
                vm_password: str | None = None) -> int:
    limit = spending_limit_cents or settings.default_spending_limit_cents
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, password_hash, name, is_admin,
                   spending_limit_cents, vm_password)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (username, password_hash, name, is_admin, limit, vm_password)
            )
            return cur.fetchone()[0]


def update_user(user_id: int, **kwargs) -> None:
    allowed = {"name", "is_active", "is_admin", "password_hash",
               "spending_limit_cents", "vm_password", "code_server_port"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [user_id]
            cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)


def delete_user(user_id: int) -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM meridian_usage_log WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM meridian_alerts WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


def get_usage_summary(user_id: int | None = None) -> list[dict]:
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if user_id:
                cur.execute(
                    """SELECT * FROM meridian_usage_log WHERE user_id = %s
                       ORDER BY created_at DESC LIMIT 200""",
                    (user_id,)
                )
            else:
                cur.execute(
                    """SELECT l.*, u.username, u.name
                       FROM meridian_usage_log l JOIN users u ON l.user_id = u.id
                       ORDER BY l.created_at DESC LIMIT 500"""
                )
            return [dict(r) for r in cur.fetchall()]
