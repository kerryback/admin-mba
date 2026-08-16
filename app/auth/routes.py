import hmac

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.auth.dependencies import create_admin_token
from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    name: str
    is_admin: bool


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    # These accounts can only access the admin panel, not the AI Lab (unless
    # they also exist in the users table separately). The shared password comes
    # from ADMIN_PASSWORD; if it is unset, no one can sign in.
    accounts = settings.admin_accounts()
    name = accounts.get(req.username)
    expected = settings.admin_password

    # Compare regardless of whether the username matched, so a wrong username
    # and a wrong password take the same time to reject.
    password_ok = bool(expected) and hmac.compare_digest(req.password, expected)
    if name is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )

    token = create_admin_token(req.username)
    return LoginResponse(token=token, name=name, is_admin=True)
