from typing import Annotated
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, StrictBool, StringConstraints
from api.support import require_admin
from services.account_operations import account_operations

AccountRef = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
JobRef = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]


class Selection(BaseModel):
    account_ids: list[AccountRef] = Field(min_length=1, max_length=20)


class Preferences(Selection):
    image_cleanup_enabled: StrictBool | None = None
    auto_refresh_token: StrictBool | None = None


def create_router():
    router = APIRouter()

    @router.get("/api/accounts/operations")
    def choices(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": account_operations.list()}

    @router.post("/api/accounts/operations/preferences")
    def preferences(body: Preferences, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"items": account_operations.preferences(body.account_ids,
                image_cleanup_enabled=body.image_cleanup_enabled, auto_refresh_token=body.auto_refresh_token)}
        except ValueError as exc:
            raise HTTPException(400, detail={"error": str(exc)}) from None

    @router.post("/api/accounts/operations/refresh", status_code=202)
    def refresh(body: Selection, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"id": account_operations.start_refresh(body.account_ids)}
        except ValueError as exc:
            raise HTTPException(400, detail={"error": str(exc)}) from None

    @router.get("/api/accounts/operations/jobs/{job_id}")
    def status(job_id: JobRef, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return account_operations.get_job(job_id)
        except KeyError:
            raise HTTPException(404, detail={"error": "刷新任务不存在或已过期"}) from None

    return router
