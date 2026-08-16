from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = "change-me"
    database_url: str = ""
    default_spending_limit_cents: int = 1000  # $10.00
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    vm_provision_url: str = "https://lab-mba.rice-business.org/provision"
    provision_secret: str = ""

    # Admin panel sign-in. The password used to be a literal in
    # app/auth/routes.py, which meant anyone who could read the repo could sign
    # in to the live panel. It comes from the environment now, with no default —
    # an unset ADMIN_PASSWORD disables admin login rather than falling back to
    # something guessable.
    admin_password: str = ""
    # "username:Display Name" pairs, comma separated. Names are not secret.
    admin_users: str = (
        "kerry_back:Kerry Back,"
        "kelcie_wold:Kelcie Wold,"
        "michael_koenig:Michael Koenig,"
        "zoran_perunovic:Zoran Perunovic"
    )

    def admin_accounts(self) -> dict[str, str]:
        """Map username -> display name."""
        out = {}
        for entry in self.admin_users.split(","):
            entry = entry.strip()
            if not entry:
                continue
            username, _, name = entry.partition(":")
            out[username.strip()] = (name or username).strip()
        return out


settings = Settings()
