from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

COMPONENT_KEYS = ("venue", "photography", "host", "catering")


class Component(BaseModel):
    """套餐中的单项服务承诺（场地布置、摄影、司仪、餐饮等）。"""

    provider: str = Field(min_length=1, max_length=120)
    staff: str = Field(default="", max_length=120)
    spec: str = Field(default="", max_length=500)
    price: float = Field(ge=0)
    note: str = Field(default="", max_length=500)


class PackageContent(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    venue: Component
    photography: Component
    host: Component
    catering: Component
    extras: dict[str, Any] = Field(default_factory=dict)


class PackageCreate(PackageContent):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    change_reason: str = Field(default="初始套餐", max_length=1000)


class PackageDraft(PackageContent):
    change_reason: str = Field(min_length=2, max_length=1000)
    parent_version_no: int | None = Field(default=None, ge=1)


class PublishRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)


class WithdrawRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class OrderCreate(BaseModel):
    order_no: str = Field(min_length=2, max_length=64)
    customer_name: str = Field(min_length=1, max_length=80)
    customer_phone: str = Field(default="", max_length=32)
    package_code: str = Field(min_length=2, max_length=64)
    expected_version_no: int = Field(ge=1)
    created_by: str = Field(min_length=1, max_length=120)


class OrderConfirm(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    expected_version_no: int = Field(ge=1)
    expected_row_version: int | None = Field(default=None, ge=1)


class OrderRetarget(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    expected_version_no: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=1000)


class OrderCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class DiffRequest(BaseModel):
    from_version_no: int = Field(ge=1)
    to_version_no: int = Field(ge=1)

    @model_validator(mode="after")
    def _distinct(self) -> "DiffRequest":
        if self.from_version_no == self.to_version_no:
            raise ValueError("比较的两个版本号必须不同")
        return self
