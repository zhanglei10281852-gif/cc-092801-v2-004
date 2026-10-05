from __future__ import annotations

from fastapi import APIRouter, Query

from app.packages.schemas import (
    DiffRequest,
    OrderCancel,
    OrderConfirm,
    OrderCreate,
    OrderRetarget,
    PackageCreate,
    PackageDraft,
    PublishRequest,
    WithdrawRequest,
)
from app.packages.service import WeddingPackageService

router = APIRouter(prefix="/api/wedding", tags=["婚庆套餐版本与订单冻结"])


def service() -> WeddingPackageService:
    return WeddingPackageService()


@router.get("/packages")
def list_packages():
    return service().list_packages()


@router.post("/packages", status_code=201)
def create_package(payload: PackageCreate, actor: str = Query(..., min_length=1)):
    return service().create_package(payload.model_dump(), actor)


@router.get("/packages/{code}")
def get_package(code: str):
    return service().get_package(code)


@router.get("/packages/{code}/timeline")
def package_timeline(code: str):
    return service().package_timeline(code)


@router.post("/packages/{code}/drafts", status_code=201)
def save_draft(code: str, payload: PackageDraft, actor: str = Query(..., min_length=1)):
    return service().save_draft(code, payload.model_dump(), actor)


@router.post("/packages/{code}/publish")
def publish_draft(code: str, payload: PublishRequest):
    return service().publish_draft(code, payload.actor, payload.reason)


@router.post("/packages/{code}/versions/{version_no}/withdraw")
def withdraw_version(code: str, version_no: int, payload: WithdrawRequest):
    return service().withdraw_version(code, version_no, payload.actor, payload.reason)


@router.post("/packages/{code}/versions/diff")
def compare_versions(code: str, payload: DiffRequest):
    return service().compare_versions(code, payload.from_version_no, payload.to_version_no)


@router.post("/orders", status_code=201)
def create_order(payload: OrderCreate):
    return service().create_order(payload.model_dump())


@router.get("/orders")
def list_orders(
    status: str | None = Query(default=None, pattern="^(pending|confirmed|cancelled)$"),
    package_code: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return service().list_orders(status=status, package_code=package_code, limit=limit)


@router.get("/orders/{order_id}")
def get_order(order_id: int):
    return service().get_order(order_id)


@router.post("/orders/{order_id}/confirm")
def confirm_order(order_id: int, payload: OrderConfirm):
    return service().confirm_order(order_id, payload.model_dump())


@router.post("/orders/{order_id}/retarget")
def retarget_order(order_id: int, payload: OrderRetarget):
    return service().retarget_order(order_id, payload.model_dump())


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, payload: OrderCancel):
    return service().cancel_order(order_id, payload.model_dump())
