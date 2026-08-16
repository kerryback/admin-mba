from fastapi import Depends

from app.auth.dependencies import get_current_admin


def require_admin(admin: dict = Depends(get_current_admin)) -> dict:
    return admin
