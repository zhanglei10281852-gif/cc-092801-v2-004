from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PackageCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    change_reason: str = Field(default="", max_length=1000)


class VersionCreate(BaseModel):
    package_code: str | None = Field(default=None, max_length=64)
    content: dict[str, Any]
    change_reason: str = Field(min_length=2, max_length=1000)
    base_version: int | None = Field(default=None, ge=1)


class VersionAction(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class OrderCreate(BaseModel):
    order_no: str = Field(min_length=2, max_length=64)
    package_code: str = Field(min_length=2, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)


class OrderExpectedVersion(BaseModel):
    version: int = Field(ge=1)


class OrderConfirm(BaseModel):
    order_no: str = Field(min_length=2, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)
    reason: str = Field(default="确认订单并冻结套餐版本", max_length=1000)
