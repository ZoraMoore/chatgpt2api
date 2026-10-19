from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from api import account_operations, accounts, ai, image_space, image_tasks, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, start_limited_account_watcher
from services.backup_service import backup_service
from services.config import config
from services.image_service import start_image_cleanup_scheduler
from services.image_space_service import start_image_space_scheduler


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        space_thread = start_image_space_scheduler(stop_event)
        backup_service.start()
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            space_thread.join(timeout=1)
            backup_service.stop()

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(account_operations.create_router())
    app.include_router(image_space.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(system.create_router(app_version))

    @app.get("/image-transport-settings.js", include_in_schema=False)
    async def image_transport_settings_script():
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "image-transport-settings.js",
                            media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            if asset.suffix == ".html":
                return extended_page(asset)
            return FileResponse(asset)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return extended_page(fallback)

    return app


def extended_page(asset):
    """统一加载独立页面扩展，使 Next 客户端跳转到号池时也能显示入口。"""
    html = asset.read_text(encoding="utf-8")
    html = html.replace("</body>", '<script src="/account-image-space.js?v=e1c0b3b7529f" defer></script>'
                        '<script src="/image-transport-settings.js?v=132d5ab480fa" defer></script></body>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
