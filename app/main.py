from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database.db import init_db
from app.auth.routes import router as auth_router
from app.admin.routes import router as admin_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BIAI Admin", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(admin_router)

app.mount("/js", StaticFiles(directory="app/static/js"), name="js")


@app.get("/")
@app.get("/login.html")
def login_page():
    return FileResponse("app/static/login.html")


@app.get("/index.html")
def admin_page():
    return FileResponse("app/static/index.html")
