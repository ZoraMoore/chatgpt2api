from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictBool, StringConstraints, model_validator

from api.support import BASE_DIR, require_admin
from services.image_space_service import ImageSpaceError, image_space_service

AccountRef = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
JobId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]


class PreviewRequest(BaseModel):
    account_ids: list[AccountRef] = Field(default_factory=list, max_length=1000)
    all_accounts: StrictBool = False

    @model_validator(mode="after")
    def selection_scope(self):
        if bool(self.account_ids) == self.all_accounts:
            raise ValueError("请选择账号或全选账号，不能同时指定两种范围")
        return self


class ExecuteRequest(BaseModel):
    confirm: Literal[True]


def create_router():
    router = APIRouter()

    @router.get("/api/accounts/image-space/accounts")
    def choices(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": image_space_service.choices()}

    @router.post("/api/accounts/image-space/preview", status_code=202)
    def preview(body: PreviewRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            job_id = image_space_service.preview(body.account_ids, all_accounts=body.all_accounts)
        except ImageSpaceError as exc:
            raise HTTPException(400, detail={"error": str(exc)}) from exc
        return {"id": job_id}

    @router.get("/api/accounts/image-space/jobs/{job_id}")
    def progress(job_id: JobId, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return image_space_service.get(job_id)
        except KeyError:
            raise HTTPException(404, detail={"error": "任务不存在或已过期（服务重启会清除任务），请重新预览"}) from None

    @router.post("/api/accounts/image-space/jobs/{job_id}/execute", status_code=202)
    def execute(job_id: JobId, body: ExecuteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            image_space_service.execute(job_id)
        except KeyError:
            raise HTTPException(404, detail={"error": "预览已过期，请重新预览"}) from None
        except ImageSpaceError as exc:
            raise HTTPException(409, detail={"error": str(exc)}) from exc
        return {"id": job_id}

    @router.get("/accounts/image-space", include_in_schema=False)
    def page(request: Request):
        # 旧书签仍能进入原账号的预览，但不再打开独立清理页面。
        refs = request.query_params.get("accounts", "").split(",")[:1000]
        refs = [ref for ref in refs if len(ref) == 64 and all(char in "0123456789abcdef" for char in ref)]
        target = "/accounts" + ("?" + urlencode({"image_space_accounts": ",".join(refs)}) if refs else "")
        return RedirectResponse(target, status_code=307, headers={"Cache-Control": "no-store"})

    @router.get("/account-image-space.js", include_in_schema=False)
    def extension():
        return FileResponse(BASE_DIR / "web" / "account-image-space.js",
                            media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @router.get("/account-image-cleanup.js", include_in_schema=False)
    def cleanup_extension():
        return FileResponse(BASE_DIR / "web" / "account-image-cleanup.js",
                            media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    return router
