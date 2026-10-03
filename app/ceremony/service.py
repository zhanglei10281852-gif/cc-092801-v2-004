from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.ceremony.repository import CeremonyRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# 套餐固定包含的四类服务
CATEGORIES: dict[str, str] = {
    "venue_setup": "场地布置",
    "photography": "摄影",
    "mc": "司仪",
    "catering": "餐饮",
}
SERVICE_FIELDS: dict[str, str] = {
    "provider": "供应商",
    "assignee": "人员",
    "price_cents": "价格(分)",
    "detail": "说明",
}


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def normalize_content(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化套餐内容，生成可冻结的快照结构。"""
    services = raw.get("services")
    if not isinstance(services, dict):
        raise ValidationError("套餐内容必须包含 services 服务清单")
    missing = [CATEGORIES[key] for key in CATEGORIES if key not in services]
    if missing:
        raise ValidationError("套餐缺少必需的服务类别", context={"missing": missing})
    normalized_services: dict[str, Any] = {}
    for key, label in CATEGORIES.items():
        item = services[key]
        if not isinstance(item, dict):
            raise ValidationError(f"{label}的配置不合法")
        provider = item.get("provider")
        assignee = item.get("assignee")
        price = item.get("price_cents")
        if not isinstance(provider, str) or not provider.strip():
            raise ValidationError(f"{label}缺少供应商")
        if not isinstance(assignee, str) or not assignee.strip():
            raise ValidationError(f"{label}缺少负责人员")
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            raise ValidationError(f"{label}的价格必须是非负整数(分)")
        detail = item.get("detail", "")
        if not isinstance(detail, str):
            raise ValidationError(f"{label}的说明必须是文本")
        normalized_services[key] = {
            "provider": provider.strip(),
            "assignee": assignee.strip(),
            "price_cents": price,
            "detail": detail,
        }
    extras = set(services) - set(CATEGORIES)
    if extras:
        raise ValidationError("包含不支持的服务类别", context={"unknown": sorted(extras)})
    return {
        "services": normalized_services,
        "total_price_cents": sum(item["price_cents"] for item in normalized_services.values()),
    }


def diff_content(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """逐字段比较两个套餐快照，返回差异明细与摘要。"""
    changes: list[dict[str, Any]] = []
    old_services = old.get("services", {})
    new_services = new.get("services", {})
    for key, label in CATEGORIES.items():
        before = old_services.get(key, {})
        after = new_services.get(key, {})
        for field, field_label in SERVICE_FIELDS.items():
            old_value = before.get(field)
            new_value = after.get(field)
            if old_value != new_value:
                changes.append({
                    "category": key,
                    "category_name": label,
                    "field": field,
                    "field_name": field_label,
                    "old": old_value,
                    "new": new_value,
                })
    affected = sorted({item["category_name"] for item in changes})
    return {
        "changes": changes,
        "change_count": len(changes),
        "affected_categories": affected,
        "old_total_price_cents": old.get("total_price_cents"),
        "new_total_price_cents": new.get("total_price_cents"),
        "price_delta_cents": (new.get("total_price_cents") or 0) - (old.get("total_price_cents") or 0),
    }


class CeremonyService:
    """管理婚庆套餐不可变版本、版本比较/撤回，以及订单的版本冻结。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CeremonyRepository(self.connection)

    # ---------- 套餐 ----------
    def create_package(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            if repository.package_by_code(payload["code"]):
                raise ConflictError("套餐编码已存在")
            package = repository.create_package(
                code=payload["code"], name=payload["name"], created_by=actor, now=now,
            )
            repository.add_package_event(
                package_id=package["id"], version_id=None, action="package_created",
                actor=actor, reason=payload.get("change_reason", ""), before={}, after=package, now=now,
            )
            return package

    def list_packages(self) -> list[dict[str, Any]]:
        return self.repository.list_packages()

    def _require_package(self, repository: CeremonyRepository, code: str) -> sqlite3.Row:
        package = repository.package_by_code(code)
        if package is None:
            raise NotFoundError("套餐不存在")
        return package

    # ---------- 版本 ----------
    def create_version(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        content = normalize_content(payload["content"])
        reason = payload["change_reason"]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            package = self._require_package(repository, payload["package_code"])
            parent = self._resolve_base(repository, int(package["id"]), payload.get("base_version"))
            if parent is not None and json.loads(parent["content_json"]) == content:
                raise ConflictError("新版本内容与基线版本完全一致，无需创建")
            duplicate = connection.execute(
                "SELECT id FROM ceremony_package_versions WHERE package_id=? AND content_digest=?",
                (package["id"], digest(content)),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError("已存在内容完全相同的版本")
            number = repository.next_version_number(int(package["id"]))
            version = repository.create_version(
                package_id=int(package["id"]), number=number,
                parent_version_id=None if parent is None else int(parent["id"]),
                content=content, content_digest=digest(content),
                change_reason=reason, created_by=actor, now=now,
            )
            repository.touch_package(int(package["id"]), now)
            repository.add_package_event(
                package_id=int(package["id"]), version_id=version["id"], action="version_created",
                actor=actor, reason=reason, before={}, after=version, now=now,
            )
            return self._version_view(version)

    def publish_version(self, package_code: str, version_number: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            package = self._require_package(repository, package_code)
            version = repository.version_by_number(int(package["id"]), version_number)
            if version is None:
                raise NotFoundError("套餐版本不存在")
            if version["status"] != "draft":
                raise ConflictError(f"版本当前状态为 {version['status']}，无法发布")
            previous = repository.active_version(int(package["id"]))
            before = dict(version)
            if previous is not None:
                connection.execute(
                    "UPDATE ceremony_package_versions SET status='superseded' WHERE id=?", (previous["id"],)
                )
            repository.mark_active(int(version["id"]), now)
            repository.touch_package(int(package["id"]), now)
            after = dict(repository.version_by_id(int(version["id"])))
            repository.add_package_event(
                package_id=int(package["id"]), version_id=int(version["id"]), action="version_published",
                actor=actor, reason=reason,
                before={"version": before, "previous_active": None if previous is None else dict(previous)},
                after=after, now=now,
            )
            return self._version_view(after)

    def withdraw_version(self, package_code: str, version_number: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            package = self._require_package(repository, package_code)
            version = repository.version_by_number(int(package["id"]), version_number)
            if version is None:
                raise NotFoundError("套餐版本不存在")
            if version["status"] == "withdrawn":
                raise ConflictError("版本已经撤回")
            references = repository.version_reference_counts(int(version["id"]))
            if references["total"] > 0 or references["draft_expected"] > 0:
                raise ConflictError(
                    "版本已被订单引用或被草稿订单选用，无法撤回",
                    context={"version": version_number, "references": references},
                )
            before = dict(version)
            repository.mark_withdrawn(int(version["id"]), actor=actor, reason=reason, now=now)
            repository.add_package_event(
                package_id=int(package["id"]), version_id=int(version["id"]), action="version_withdrawn",
                actor=actor, reason=reason, before=before,
                after=dict(repository.version_by_id(int(version["id"]))), now=now,
            )
            view = self.get_version(package_code, version_number)
            view["reference_counts"] = references
            return view

    def list_versions(self, package_code: str) -> dict[str, Any]:
        with transaction() as connection:
            repository = CeremonyRepository(connection)
            package = self._require_package(repository, package_code)
            rows = repository.list_versions(int(package["id"]))
        return {
            "package": dict(package),
            "current_version": self._current_number(int(package["id"])),
            "versions": [self._version_view(row) for row in rows],
        }

    def get_version(self, package_code: str, version_number: int) -> dict[str, Any]:
        repository = self.repository
        package = self._require_package(repository, package_code)
        version = repository.version_by_number(int(package["id"]), version_number)
        if version is None:
            raise NotFoundError("套餐版本不存在")
        return self._version_view(dict(version))

    def compare_versions(self, package_code: str, from_version: int, to_version: int) -> dict[str, Any]:
        repository = self.repository
        package = self._require_package(repository, package_code)
        old = repository.version_by_number(int(package["id"]), from_version)
        new = repository.version_by_number(int(package["id"]), to_version)
        if old is None or new is None:
            raise NotFoundError("待比较的版本不存在")
        old_content = json.loads(old["content_json"])
        new_content = json.loads(new["content_json"])
        result = diff_content(old_content, new_content)
        result.update({
            "package_code": package_code,
            "from_version": from_version,
            "to_version": to_version,
            "from_status": old["status"],
            "to_status": new["status"],
            "identical": old["content_digest"] == new["content_digest"],
        })
        return result

    def package_history(self, package_code: str) -> dict[str, Any]:
        package = self._require_package(self.repository, package_code)
        return {
            "package": dict(package),
            "events": self.repository.package_events(int(package["id"])),
        }

    def _resolve_base(self, repository: CeremonyRepository, package_id: int,
                      base_number: int | None) -> sqlite3.Row | None:
        if base_number is not None:
            base = repository.version_by_number(package_id, base_number)
            if base is None:
                raise NotFoundError("基线版本不存在")
            if base["status"] == "withdrawn":
                raise ConflictError("不能基于已撤回的版本创建新版本")
            return base
        active = repository.active_version(package_id)
        if active is not None:
            return active
        rows = [row for row in repository.list_versions(package_id) if row["status"] != "withdrawn"]
        return None if not rows else repository.version_by_id(rows[-1]["id"])

    def _current_number(self, package_id: int) -> int | None:
        active = self.repository.active_version(package_id)
        return None if active is None else int(active["version"])

    @staticmethod
    def _version_view(row: dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        view["content"] = json.loads(view.pop("content_json"))
        return view

    # ---------- 订单 ----------
    def create_order(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            if repository.order_by_no(payload["order_no"]):
                raise ConflictError("订单编号已存在")
            package = self._require_package(repository, payload["package_code"])
            target = self._resolve_expected(repository, int(package["id"]), payload.get("expected_version"))
            order = repository.create_order(
                order_no=payload["order_no"], package_id=int(package["id"]),
                expected_version_id=None if target is None else int(target["id"]),
                created_by=actor, now=now,
            )
            repository.add_order_event(
                order_id=int(order["id"]), action="order_created", actor=actor, reason="",
                before={}, after=dict(order), now=now,
            )
            return self.get_order(int(order["id"]))

    def set_order_expected_version(self, order_id: int, version_number: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            order = repository.order_by_id(order_id)
            if order is None:
                raise NotFoundError("订单不存在")
            if order["status"] != "draft":
                raise ConflictError("订单已确认，引用的套餐版本已冻结，不能改挂其他版本")
            target = repository.version_by_number(int(order["package_id"]), version_number)
            if target is None:
                raise NotFoundError("套餐版本不存在")
            if target["status"] == "withdrawn":
                raise ConflictError("不能将订单指向已撤回的版本")
            before = dict(order)
            repository.set_expected_version(order_id, int(target["id"]), now)
            after = dict(repository.order_by_id(order_id))
            repository.add_order_event(
                order_id=order_id, action="expected_version_changed", actor=actor,
                reason=f"草稿改挂版本 {version_number}", before=before, after=after, now=now,
            )
            return self.get_order(order_id)

    def confirm_order(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CeremonyRepository(connection)
            order = repository.order_by_no(payload["order_no"])
            if order is None:
                raise NotFoundError("订单不存在")
            order_id = int(order["id"])
            if order["status"] == "confirmed":
                raise ConflictError("订单已经确认，版本不可重复冻结")
            if order["status"] != "draft":
                raise ConflictError(f"订单当前状态为 {order['status']}，无法确认")
            package_id = int(order["package_id"])
            current = repository.active_version(package_id)
            if current is None:
                raise ConflictError("套餐尚无已发布版本，无法确认订单")

            # 乐观并发：调用方基于读取时的版本确认；期间若发布了新版本则明确报冲突。
            expected_id = order["expected_version_id"]
            if payload.get("expected_version") is not None:
                expected_row = repository.version_by_number(package_id, int(payload["expected_version"]))
                if expected_row is None:
                    raise NotFoundError("指定的期望版本不存在")
                expected_id = expected_row["id"]
            if expected_id is None:
                expected_id = current["id"]
            if int(expected_id) != int(current["id"]):
                raise ConflictError(
                    "确认所基于的套餐版本已过期，请基于最新版本重新确认",
                    context={
                        "expected_version": self._version_number(repository, int(expected_id)),
                        "current_version": int(current["version"]),
                    },
                )

            content = json.loads(current["content_json"])
            before = dict(order)
            repository.confirm_order(
                order_id, version_id=int(current["id"]),
                total_price_cents=int(content["total_price_cents"]), actor=actor, now=now,
            )
            after = dict(repository.order_by_id(order_id))
            repository.add_order_event(
                order_id=order_id, action="order_confirmed", actor=actor,
                reason=payload.get("reason", "确认订单并冻结套餐版本"),
                before=before, after=after, now=now,
            )
            return self.get_order(order_id)

    def get_order(self, order_id: int) -> dict[str, Any]:
        row = self.repository.order_by_id(order_id)
        if row is None:
            raise NotFoundError("订单不存在")
        return self._order_view(dict(row))

    def get_order_by_no(self, order_no: str) -> dict[str, Any]:
        row = self.repository.order_by_no(order_no)
        if row is None:
            raise NotFoundError("订单不存在")
        return self._order_view(dict(row))

    def list_orders(self, *, status: str | None = None, package_code: str | None = None, limit: int = 100) -> dict[str, Any]:
        package_id: int | None = None
        if package_code:
            package = self.repository.package_by_code(package_code)
            if package is None:
                raise NotFoundError("套餐不存在")
            package_id = int(package["id"])
        rows = self.repository.list_orders(status=status, package_id=package_id, limit=max(1, min(limit, 500)))
        return {"items": [self._order_view(row, include_diff=False) for row in rows]}

    def order_history(self, order_no: str) -> dict[str, Any]:
        order = self._require_order(self.repository, order_no)
        return {"order": dict(order), "events": self.repository.order_events(int(order["id"]))}

    def _order_view(self, order: dict[str, Any], *, include_diff: bool = True) -> dict[str, Any]:
        repository = self.repository
        pinned_id = order["package_version_id"]
        expected_id = order["expected_version_id"]
        effective_id = pinned_id if pinned_id is not None else expected_id
        effective: dict[str, Any] | None = None
        parent_number: int | None = None
        if effective_id is not None:
            version_row = repository.version_by_id(int(effective_id))
            if version_row is not None:
                effective = self._version_view(dict(version_row))
                if version_row["parent_version_id"]:
                    parent = repository.version_by_id(int(version_row["parent_version_id"]))
                    parent_number = None if parent is None else int(parent["version"])
        current = repository.active_version(int(order["package_id"]))
        current_number = None if current is None else int(current["version"])

        view = dict(order)
        view["effective_version"] = effective
        view["effective_version_number"] = None if effective is None else effective["version"]
        view["parent_version_number"] = parent_number
        view["current_version_number"] = current_number
        view["frozen"] = pinned_id is not None

        if include_diff and effective is not None and current is not None and int(current["id"]) != int(effective["id"]):
            view["diff_summary"] = diff_content(
                effective["content"], json.loads(current["content_json"])
            )
        elif include_diff:
            view["diff_summary"] = None
        return view

    def _resolve_expected(self, repository: CeremonyRepository, package_id: int,
                          version_number: int | None) -> sqlite3.Row | None:
        if version_number is not None:
            target = repository.version_by_number(package_id, version_number)
            if target is None:
                raise NotFoundError("指定的套餐版本不存在")
            if target["status"] == "withdrawn":
                raise ConflictError("不能引用已撤回的版本")
            return target
        return repository.active_version(package_id)

    @staticmethod
    def _version_number(repository: CeremonyRepository, version_id: int) -> int | None:
        row = repository.version_by_id(version_id)
        return None if row is None else int(row["version"])

    def _require_order(self, repository: CeremonyRepository, order_no: str) -> sqlite3.Row:
        order = repository.order_by_no(order_no)
        if order is None:
            raise NotFoundError("订单不存在")
        return order
