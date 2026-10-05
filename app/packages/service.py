from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.packages.repository import PackageRepository
from app.packages.schema_sql import ensure_schema
from app.packages.schemas import COMPONENT_KEYS

COMPONENT_FIELDS = ("provider", "staff", "spec", "price", "note")


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def build_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """提取套餐承诺内容（场地布置、摄影、司仪、餐饮）作为快照主体。"""

    snapshot: dict[str, Any] = {"name": payload["name"]}
    for key in COMPONENT_KEYS:
        snapshot[key] = {field: payload[key][field] for field in COMPONENT_FIELDS}
    snapshot["extras"] = dict(payload.get("extras") or {})
    return snapshot


def diff_snapshots(from_snapshot: dict[str, Any], to_snapshot: dict[str, Any]) -> dict[str, Any]:
    """比较两个不可变快照，输出字段级变更与价格差异摘要。"""

    changes: list[dict[str, Any]] = []
    components_changed: set[str] = set()

    def compare(path: str, left: Any, right: Any, component: str | None = None) -> None:
        if left != right:
            changes.append({"path": path, "from": left, "to": right})
            if component:
                components_changed.add(component)

    compare("name", from_snapshot.get("name"), to_snapshot.get("name"))
    price_delta: dict[str, float] = {}
    for key in COMPONENT_KEYS:
        old_component = from_snapshot.get(key) or {}
        new_component = to_snapshot.get(key) or {}
        for field in COMPONENT_FIELDS:
            compare(f"{key}.{field}", old_component.get(field), new_component.get(field), component=key)
        delta = round(float(new_component.get("price") or 0) - float(old_component.get("price") or 0), 2)
        price_delta[key] = delta
    old_extras = from_snapshot.get("extras") or {}
    new_extras = to_snapshot.get("extras") or {}
    for path in sorted(set(old_extras) | set(new_extras)):
        if old_extras.get(path) != new_extras.get(path):
            compare(f"extras.{path}", old_extras.get(path), new_extras.get(path), component="extras")
    return {
        "changed_paths": [item["path"] for item in changes],
        "components_changed": sorted(components_changed),
        "changes": changes,
        "price_delta": {"by_component": price_delta, "total": round(sum(price_delta.values()), 2)},
    }


class WeddingPackageService:
    """管理婚庆套餐的不可变版本、发布/撤回与订单确认时的版本冻结。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()
        self.repository = PackageRepository(self.connection)

    # ---- 套餐与版本 ----

    def create_package(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        snapshot = build_snapshot(payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            if repository.package_by_code(payload["code"]):
                raise ConflictError("套餐编码已存在")
            package = repository.create_package(code=payload["code"], name=snapshot["name"], now=now)
            version = repository.insert_version(
                package_id=package["id"], version_no=1, status="published", parent_version_id=None,
                snapshot=snapshot, snapshot_digest=snapshot_digest(snapshot),
                change_reason=payload.get("change_reason") or "初始套餐", created_by=actor, now=now,
            )
            repository.touch_package(package["id"], now)
            repository.add_package_audit(
                package_id=package["id"], version_no=1, action="package.create", actor=actor,
                reason=payload.get("change_reason") or "初始套餐", before={}, after=self._version_view(version), now=now,
            )
            return self._package_view(connection, package["id"])

    def save_draft(self, code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        snapshot = build_snapshot(payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            package = self._require_package(repository, code)
            if repository.open_draft(package["id"]) is not None:
                raise ConflictError("该套餐已存在未发布草稿，请先发布或撤回后再调整")
            if payload.get("parent_version_no") is None:
                parent = self._latest_version(repository, package["id"])
            else:
                parent = repository.version_by_no(package["id"], int(payload["parent_version_no"]))
                if parent is None:
                    raise NotFoundError("所基于的套餐版本不存在")
            version_no = repository.next_version_no(package["id"])
            version = repository.insert_version(
                package_id=package["id"], version_no=version_no, status="draft",
                parent_version_id=parent["id"], snapshot=snapshot, snapshot_digest=snapshot_digest(snapshot),
                change_reason=payload["change_reason"], created_by=actor, now=now,
            )
            repository.touch_package(package["id"], now)
            repository.add_package_audit(
                package_id=package["id"], version_no=version_no, action="version.draft", actor=actor,
                reason=payload["change_reason"], before=self._version_view(parent), after=self._version_view(version), now=now,
            )
            return self._package_view(connection, package["id"])

    def publish_draft(self, code: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            package = self._require_package(repository, code)
            draft = repository.open_draft(package["id"])
            if draft is None:
                raise NotFoundError("该套餐没有待发布的草稿版本")
            before = self._version_view(draft)
            repository.mark_published(draft["id"], actor=actor, now=now)
            after = self._version_view(repository.version_by_id(draft["id"]))
            repository.touch_package(package["id"], now)
            repository.add_package_audit(
                package_id=package["id"], version_no=draft["version_no"], action="version.publish",
                actor=actor, reason=reason, before=before, after=after, now=now,
            )
            return self._package_view(connection, package["id"])

    def withdraw_version(self, code: str, version_no: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            package = self._require_package(repository, code)
            version = repository.version_by_no(package["id"], version_no)
            if version is None:
                raise NotFoundError("套餐版本不存在")
            if version["status"] != "published":
                raise ConflictError(f"版本当前状态为 {version['status']}，只有已发布版本可以撤回")
            frozen_orders = repository.orders_referencing_version(
                version["id"], statuses=("confirmed",)
            )
            pending_orders = int(connection.execute(
                "SELECT COUNT(*) FROM wedding_orders WHERE package_id=? AND expected_version_no=? AND status='pending'",
                (package["id"], version_no),
            ).fetchone()[0])
            if frozen_orders or pending_orders:
                raise ConflictError(
                    "版本已被订单引用，不能撤回",
                    context={"confirmed_orders": frozen_orders, "pending_orders": pending_orders},
                )
            before = self._version_view(version)
            repository.mark_withdrawn(version["id"], actor=actor, reason=reason, now=now)
            after = self._version_view(repository.version_by_id(version["id"]))
            repository.add_package_audit(
                package_id=package["id"], version_no=version_no, action="version.withdraw",
                actor=actor, reason=reason, before=before, after=after, now=now,
            )
            return self._package_view(connection, package["id"])

    def get_package(self, code: str) -> dict[str, Any]:
        return self._package_view(self.repository, self._require_package(self.repository, code)["id"])

    def list_packages(self) -> dict[str, Any]:
        items = [self._package_view(self.repository, row["id"]) for row in self.repository.list_packages()]
        return {"items": items}

    def compare_versions(self, code: str, from_version_no: int, to_version_no: int) -> dict[str, Any]:
        package = self._require_package(self.repository, code)
        from_version = self.repository.version_by_no(package["id"], from_version_no)
        to_version = self.repository.version_by_no(package["id"], to_version_no)
        if from_version is None or to_version is None:
            raise NotFoundError("待比较的套餐版本不存在")
        result = diff_snapshots(json.loads(from_version["snapshot_json"]), json.loads(to_version["snapshot_json"]))
        result.update(
            {
                "package_code": code,
                "from_version_no": from_version_no,
                "to_version_no": to_version_no,
                "from_status": from_version["status"],
                "to_status": to_version["status"],
            }
        )
        return result

    def package_timeline(self, code: str) -> dict[str, Any]:
        package = self._require_package(self.repository, code)
        view = self._package_view(self.repository, package["id"])
        view["audit"] = self.repository.package_audit(package["id"])
        return view

    # ---- 订单 ----

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            package = self._require_package(repository, payload["package_code"])
            version = repository.version_by_no(package["id"], payload["expected_version_no"])
            if version is None:
                raise NotFoundError("套餐版本不存在")
            if version["status"] != "published":
                raise ConflictError(f"只能按已发布版本下单，版本 {payload['expected_version_no']} 当前为 {version['status']}")
            if repository.order_by_no(payload["order_no"]):
                raise ConflictError("订单号已存在")
            order = repository.create_order(
                order_no=payload["order_no"], customer_name=payload["customer_name"],
                customer_phone=payload.get("customer_phone", ""), package_id=package["id"],
                expected_version_no=payload["expected_version_no"], created_by=payload["created_by"], now=now,
            )
            repository.add_order_event(
                order_id=order["id"], action="order.create", actor=payload["created_by"], reason="",
                before={}, after=dict(order), now=now,
            )
            return self._order_view(connection, order["id"], include_events=True)

    def confirm_order(self, order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            order = repository.order_by_id(order_id)
            if order is None:
                raise NotFoundError("订单不存在")
            if order["status"] == "confirmed":
                raise ConflictError(
                    f"订单已经确认，生效版本为 v{order['frozen_version_no']}，不能重复确认或改写",
                    context={"frozen_version_no": order["frozen_version_no"], "frozen_digest": order["frozen_digest"]},
                )
            if order["status"] == "cancelled":
                raise ConflictError("订单已取消，不能确认")
            expected_no = int(payload["expected_version_no"])
            if expected_no != int(order["expected_version_no"]):
                raise ConflictError(
                    f"确认请求基于版本 v{expected_no}，订单登记的引用版本为 v{order['expected_version_no']}",
                    context={"order_expected_version_no": order["expected_version_no"], "confirm_version_no": expected_no},
                )
            version = repository.version_by_no(order["package_id"], expected_no)
            if version is None or version["status"] != "published":
                raise ConflictError(f"引用版本 v{expected_no} 不可用（未发布或已撤回）")
            latest_published = self._latest_published(repository, order["package_id"])
            if latest_published is not None and int(latest_published["version_no"]) > expected_no:
                raise ConflictError(
                    f"套餐已发布更新版本 v{latest_published['version_no']}，基于旧版本 v{expected_no} 的确认存在冲突；"
                    "请先核对差异并显式改单到新版本后再确认",
                    context={
                        "expected_version_no": expected_no,
                        "latest_published_version_no": latest_published["version_no"],
                    },
                )
            expected_row_version = payload.get("expected_row_version")
            if expected_row_version is not None and int(expected_row_version) != int(order["row_version"]):
                raise ConflictError(
                    "订单已被其他操作修改，请刷新后重新确认",
                    context={"current_row_version": order["row_version"], "expected_row_version": expected_row_version},
                )
            snapshot = json.loads(version["snapshot_json"])
            digest = version["snapshot_digest"]
            updated = repository.freeze_order(
                order["id"], version_id=version["id"], version_no=expected_no, snapshot=snapshot,
                digest=digest, actor=payload["actor"], now=now, expected_row_version=expected_row_version,
            )
            if updated != 1:
                # 并发事务抢先确认：立即给出明确冲突。
                current = repository.order_by_id(order_id)
                raise ConflictError(
                    "订单已被并发流程确认",
                    context={"frozen_version_no": current["frozen_version_no"] if current else None},
                )
            after = repository.order_by_id(order_id)
            repository.add_order_event(
                order_id=order_id, action="order.confirm", actor=payload["actor"], reason="",
                before=dict(order), after=dict(after), now=now,
            )
            return self._order_view(connection, order_id, include_events=True)

    def retarget_order(self, order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """待确认订单显式改单到另一已发布版本（保留审计，不允许静默改内容）。"""

        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            order = repository.order_by_id(order_id)
            if order is None:
                raise NotFoundError("订单不存在")
            if order["status"] != "pending":
                raise ConflictError("只有待确认订单可以改单版本")
            target_no = int(payload["expected_version_no"])
            if target_no == int(order["expected_version_no"]):
                raise ValidationError("改单目标版本与当前引用版本相同")
            version = repository.version_by_no(order["package_id"], target_no)
            if version is None or version["status"] != "published":
                raise ConflictError(f"目标版本 v{target_no} 不可用（未发布或已撤回）")
            before = dict(order)
            connection.execute(
                "UPDATE wedding_orders SET expected_version_no=?,updated_at=?,row_version=row_version+1 WHERE id=?",
                (target_no, now, order_id),
            )
            after = repository.order_by_id(order_id)
            repository.add_order_event(
                order_id=order_id, action="order.retarget", actor=payload["actor"],
                reason=payload["reason"], before=before, after=dict(after), now=now,
            )
            return self._order_view(connection, order_id, include_events=True)

    def cancel_order(self, order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PackageRepository(connection)
            order = repository.order_by_id(order_id)
            if order is None:
                raise NotFoundError("订单不存在")
            if order["status"] == "confirmed":
                raise ConflictError("订单已确认并冻结版本，不能取消")
            if order["status"] == "cancelled":
                raise ConflictError("订单已经处于取消状态")
            updated = repository.cancel_order(
                order_id, actor=payload["actor"], reason=payload["reason"], now=now
            )
            if updated != 1:
                raise ConflictError("订单状态已被并发修改，取消失败")
            after = repository.order_by_id(order_id)
            repository.add_order_event(
                order_id=order_id, action="order.cancel", actor=payload["actor"],
                reason=payload["reason"], before=dict(order), after=dict(after), now=now,
            )
            return self._order_view(connection, order_id, include_events=True)

    def get_order(self, order_id: int) -> dict[str, Any]:
        if self.repository.order_by_id(order_id) is None:
            raise NotFoundError("订单不存在")
        return self._order_view(self.repository, order_id, include_events=True)

    def list_orders(self, *, status: str | None = None, package_code: str | None = None, limit: int = 100) -> dict[str, Any]:
        items = self.repository.list_orders(status=status, package_code=package_code, limit=max(1, min(limit, 500)))
        return {"items": [self._order_summary(item) for item in items]}

    # ---- 视图组装 ----

    @staticmethod
    def _version_view(row: sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        view["snapshot"] = json.loads(row["snapshot_json"])
        return view

    def _package_view(self, repository: PackageRepository | sqlite3.Connection, package_id: int) -> dict[str, Any]:
        repo = repository if isinstance(repository, PackageRepository) else PackageRepository(repository)
        package = dict(repo.package_by_id(package_id))
        versions = [self._version_view(row) for row in repo.versions(package_id)]
        package["versions"] = versions
        published = [v for v in versions if v["status"] == "published"]
        package["current_published_version_no"] = published[-1]["version_no"] if published else None
        drafts = [v for v in versions if v["status"] == "draft"]
        package["draft_version_no"] = drafts[-1]["version_no"] if drafts else None
        chain = [{"version_no": v["version_no"], "parent_version_id": v["parent_version_id"]} for v in versions]
        package["version_chain"] = chain
        return package

    def _order_summary(self, order: dict[str, Any]) -> dict[str, Any]:
        package_id = order["package_id"]
        latest_published = self._latest_published(self.repository, package_id)
        latest_no = latest_published["version_no"] if latest_published is not None else None
        summary: dict[str, Any] = {
            "id": order["id"],
            "order_no": order["order_no"],
            "customer_name": order["customer_name"],
            "package_code": order["package_code"],
            "status": order["status"],
            "expected_version_no": order["expected_version_no"],
            "effective_version_no": order["frozen_version_no"],
            "current_published_version_no": latest_no,
            "row_version": order["row_version"],
            "created_at": order["created_at"],
            "confirmed_at": order["confirmed_at"],
        }
        if order["status"] == "confirmed":
            frozen = json.loads(order["frozen_snapshot_json"])
            if latest_no is not None and int(latest_no) != int(order["frozen_version_no"]):
                current_snapshot = json.loads(latest_published["snapshot_json"])
                diff = diff_snapshots(frozen, current_snapshot)
                summary["diff_to_current"] = {
                    "from_version_no": order["frozen_version_no"],
                    "to_version_no": latest_no,
                    "changed_paths": diff["changed_paths"],
                    "components_changed": diff["components_changed"],
                    "price_delta": diff["price_delta"],
                }
            else:
                summary["diff_to_current"] = None
        elif order["status"] == "pending":
            summary["stale"] = latest_no is not None and int(latest_no) > int(order["expected_version_no"])
        return summary

    def _order_view(self, repository: PackageRepository | sqlite3.Connection, order_id: int, *, include_events: bool) -> dict[str, Any]:
        repo = repository if isinstance(repository, PackageRepository) else PackageRepository(repository)
        order = dict(repo.order_by_id(order_id))
        view = self._order_summary(order)
        if order["frozen_snapshot_json"]:
            view["frozen_snapshot"] = json.loads(order["frozen_snapshot_json"])
            view["frozen_digest"] = order["frozen_digest"]
        package = self._package_view(repo, order["package_id"])
        view["package"] = {"code": package["code"], "name": package["name"]}
        if include_events:
            view["events"] = repo.order_events(order_id)
        return view

    # ---- 辅助 ----

    @staticmethod
    def _require_package(repository: PackageRepository, code: str) -> sqlite3.Row:
        package = repository.package_by_code(code)
        if package is None:
            raise NotFoundError("套餐不存在")
        return package

    @staticmethod
    def _latest_version(repository: PackageRepository, package_id: int) -> sqlite3.Row:
        rows = repository.versions(package_id)
        if not rows:
            raise NotFoundError("套餐还没有任何版本")
        return rows[-1]

    @staticmethod
    def _latest_published(repository: PackageRepository, package_id: int) -> sqlite3.Row | None:
        published = [
            row for row in repository.versions(package_id) if row["status"] == "published"
        ]
        return published[-1] if published else None
