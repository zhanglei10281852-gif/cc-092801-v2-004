from __future__ import annotations

from fastapi import APIRouter, Query

from app.ceremony.schemas import (
    OrderConfirm,
    OrderCreate,
    OrderExpectedVersion,
    PackageCreate,
    VersionAction,
    VersionCreate,
)
from app.ceremony.service import CeremonyService

router = APIRouter(prefix="/api/ceremony", tags=["婚庆套餐版本与订单冻结"])


def service() -> CeremonyService:
    return CeremonyService()


# ---------- 套餐 ----------
@router.post("/packages", status_code=201)
def create_package(payload: PackageCreate, actor: str = Query(..., min_length=1)):
    return service().create_package(payload.model_dump(), actor)


@router.get("/packages")
def list_packages():
    return {"items": service().list_packages()}


@router.get("/packages/{package_code}/versions")
def list_versions(package_code: str):
    return service().list_versions(package_code)


@router.post("/packages/{package_code}/versions", status_code=201)
def create_version(package_code: str, payload: VersionCreate, actor: str = Query(..., min_length=1)):
    data = payload.model_dump()
    data["package_code"] = package_code
    return service().create_version(data, actor)


@router.get("/packages/{package_code}/versions/{version_number}")
def get_version(package_code: str, version_number: int):
    return service().get_version(package_code, version_number)


@router.post("/packages/{package_code}/versions/{version_number}/publish")
def publish_version(package_code: str, version_number: int, payload: VersionAction,
                    actor: str = Query(..., min_length=1)):
    return service().publish_version(package_code, version_number, actor, payload.reason)


@router.post("/packages/{package_code}/versions/{version_number}/withdraw")
def withdraw_version(package_code: str, version_number: int, payload: VersionAction,
                     actor: str = Query(..., min_length=1)):
    return service().withdraw_version(package_code, version_number, actor, payload.reason)


@router.get("/packages/{package_code}/compare")
def compare_versions(package_code: str, from_version: int = Query(..., alias="from", ge=1),
                     to_version: int = Query(..., alias="to", ge=1)):
    return service().compare_versions(package_code, from_version, to_version)


@router.get("/packages/{package_code}/history")
def package_history(package_code: str):
    return service().package_history(package_code)


# ---------- 订单 ----------
@router.post("/orders", status_code=201)
def create_order(payload: OrderCreate, actor: str = Query(..., min_length=1)):
    return service().create_order(payload.model_dump(), actor)


@router.get("/orders")
def list_orders(status: str | None = None, package_code: str | None = None,
                limit: int = Query(default=100, ge=1, le=500)):
    return service().list_orders(status=status, package_code=package_code, limit=limit)


@router.post("/orders/confirm")
def confirm_order(payload: OrderConfirm, actor: str = Query(..., min_length=1)):
    return service().confirm_order(payload.model_dump(), actor)


@router.get("/orders/by-no/{order_no}")
def get_order_by_no(order_no: str):
    return service().get_order_by_no(order_no)


@router.get("/orders/{order_id}")
def get_order(order_id: int):
    return service().get_order(order_id)


@router.put("/orders/{order_id}/expected-version")
def set_expected_version(order_id: int, payload: OrderExpectedVersion,
                         actor: str = Query(..., min_length=1)):
    return service().set_order_expected_version(order_id, payload.version, actor)


@router.get("/orders/{order_id}/history")
def order_history(order_id: int):
    order = service().get_order(order_id)
    return service().order_history(order["order_no"])
