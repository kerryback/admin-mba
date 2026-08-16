import csv
import io
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel

from app.admin.dependencies import require_admin
from app.auth.dependencies import hash_password
from app.config import settings
from app.database.db import (
    list_users, create_user, update_user, delete_user,
    get_user_by_username, get_usage_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def provision_vm_user(username: str, password: str) -> tuple[str | None, int | None]:
    """Provision a user on the AI Lab VM. Returns (error_message, code_server_port)."""
    if not settings.provision_secret:
        return "VM provisioning not configured (no PROVISION_SECRET)", None
    try:
        resp = httpx.post(
            settings.vm_provision_url,
            data={"username": username, "password": password},
            headers={"X-Provision-Secret": settings.provision_secret},
            timeout=90,
        )
        if resp.status_code == 200:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            port = data.get("code_server_port")
            return None, port
        return f"VM provision failed ({resp.status_code}): {resp.text}", None
    except Exception as e:
        return f"VM provision error: {e}", None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    name: str = ""
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str


@router.get("/users")
def admin_list_users(_admin: dict = Depends(require_admin)):
    return list_users()


@router.post("/users", status_code=status.HTTP_201_CREATED)
def admin_create_user(req: CreateUserRequest, _admin: dict = Depends(require_admin)):
    if get_user_by_username(req.username):
        raise HTTPException(status_code=400, detail="Username already exists")
    user_id = create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        name=req.name,
        is_admin=req.is_admin,
        vm_password=req.password,
    )
    vm_error, port = provision_vm_user(req.username, req.password)
    if vm_error:
        logger.warning("VM provision for %s: %s", req.username, vm_error)
    if port:
        update_user(user_id, code_server_port=port)
    return {"id": user_id, "username": req.username, "vm_provisioned": vm_error is None,
            "vm_error": vm_error, "code_server_port": port}


@router.patch("/users/{user_id}")
def admin_update_user(user_id: int, req: UpdateUserRequest,
                      _admin: dict = Depends(require_admin)):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    update_user(user_id, **updates)
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, req: ResetPasswordRequest,
                         _admin: dict = Depends(require_admin)):
    from app.database.db import get_user_by_id
    update_user(user_id, password_hash=hash_password(req.password),
                vm_password=req.password)
    user = get_user_by_id(user_id)
    if user:
        vm_error, _ = provision_vm_user(user["username"], req.password)
        if vm_error:
            logger.warning("VM password sync for %s: %s", user["username"], vm_error)
    return {"ok": True}


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: int, _admin: dict = Depends(require_admin)):
    from app.database.db import get_user_by_id
    user = get_user_by_id(user_id)
    if user and user["username"] == _admin["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    delete_user(user_id)
    return {"ok": True}


@router.post("/users/bulk")
async def admin_bulk_create(file: UploadFile = File(...),
                            _admin: dict = Depends(require_admin)):
    """Upload a CSV with columns: username, password, name (optional)."""
    content = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    created = []
    skipped = []
    for row in reader:
        username = row.get("username", "").strip()
        password = row.get("password", "").strip()
        if not username or not password:
            continue
        if get_user_by_username(username):
            skipped.append(username)
            continue
        name = row.get("name", "").strip()
        user_id = create_user(
            username=username,
            password_hash=hash_password(password),
            name=name,
            vm_password=password,
        )
        vm_error, port = provision_vm_user(username, password)
        if vm_error:
            logger.warning("VM provision for %s: %s", username, vm_error)
        if port:
            update_user(user_id, code_server_port=port)
        created.append(username)
    return {"created": created, "skipped": skipped}


@router.get("/usage")
def admin_usage(user_id: int | None = None, _admin: dict = Depends(require_admin)):
    return get_usage_summary(user_id)
