from __future__ import annotations

import json
import sqlite3
from typing import Any


class PackageRepository:
    """婚庆套餐版本与订单的 SQLite 读写封装。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 套餐 ----

    def package_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM wedding_packages WHERE code=?", (code,)).fetchone()

    def package_by_id(self, package_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM wedding_packages WHERE id=?", (package_id,)).fetchone()

    def create_package(self, *, code: str, name: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO wedding_packages(code,name,created_at,updated_at) VALUES(?,?,?,?)",
            (code, name, now, now),
        )
        return self.package_by_id(cursor.lastrowid)

    def list_packages(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM wedding_packages ORDER BY code").fetchall()]

    def touch_package(self, package_id: int, now: str) -> None:
        self.connection.execute("UPDATE wedding_packages SET updated_at=? WHERE id=?", (now, package_id))

    # ---- 版本（不可变快照）----

    def insert_version(
        self,
        *,
        package_id: int,
        version_no: int,
        status: str,
        parent_version_id: int | None,
        snapshot: dict[str, Any],
        snapshot_digest: str,
        change_reason: str,
        created_by: str,
        now: str,
    ) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO wedding_package_versions(package_id,version_no,status,parent_version_id,snapshot_json,snapshot_digest,change_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                package_id,
                version_no,
                status,
                parent_version_id,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                snapshot_digest,
                change_reason,
                created_by,
                now,
            ),
        )
        return self.version_by_id(cursor.lastrowid)

    def next_version_no(self, package_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM wedding_package_versions WHERE package_id=?",
            (package_id,),
        ).fetchone()[0])

    def version_by_id(self, version_id: int) -> sqlite3.Row:
        return self.connection.execute("SELECT * FROM wedding_package_versions WHERE id=?", (version_id,)).fetchone()

    def version_by_no(self, package_id: int, version_no: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM wedding_package_versions WHERE package_id=? AND version_no=?",
            (package_id, version_no),
        ).fetchone()

    def versions(self, package_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM wedding_package_versions WHERE package_id=? ORDER BY version_no",
            (package_id,),
        ).fetchall()

    def open_draft(self, package_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM wedding_package_versions WHERE package_id=? AND status='draft' ORDER BY version_no DESC LIMIT 1",
            (package_id,),
        ).fetchone()

    def mark_published(self, version_id: int, *, actor: str, now: str) -> None:
        self.connection.execute(
            "UPDATE wedding_package_versions SET status='published',published_by=?,published_at=? WHERE id=? AND status='draft'",
            (actor, now, version_id),
        )

    def mark_withdrawn(self, version_id: int, *, actor: str, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE wedding_package_versions SET status='withdrawn',withdrawn_by=?,withdrawn_at=?,withdraw_reason=? WHERE id=? AND status='published'",
            (actor, now, reason, version_id),
        )

    def orders_referencing_version(self, version_id: int, *, statuses: tuple[str, ...]) -> int:
        placeholders = ",".join("?" for _ in statuses)
        return int(self.connection.execute(
            f"SELECT COUNT(*) FROM wedding_orders WHERE package_version_id=? AND status IN ({placeholders})",
            (version_id, *statuses),
        ).fetchone()[0])

    # ---- 审计 ----

    def add_package_audit(self, *, package_id: int, version_no: int | None, action: str, actor: str, reason: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO wedding_package_audit(package_id,version_no,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (package_id, version_no, action, actor, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def package_audit(self, package_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM wedding_package_audit WHERE package_id=? ORDER BY id", (package_id,)
        ).fetchall()]

    # ---- 订单 ----

    def order_by_no(self, order_no: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT o.*,p.code AS package_code FROM wedding_orders o JOIN wedding_packages p ON p.id=o.package_id WHERE o.order_no=?",
            (order_no,),
        ).fetchone()

    def order_by_id(self, order_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT o.*,p.code AS package_code FROM wedding_orders o JOIN wedding_packages p ON p.id=o.package_id WHERE o.id=?",
            (order_id,),
        ).fetchone()

    def create_order(
        self,
        *,
        order_no: str,
        customer_name: str,
        customer_phone: str,
        package_id: int,
        expected_version_no: int,
        created_by: str,
        now: str,
    ) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO wedding_orders(order_no,customer_name,customer_phone,package_id,expected_version_no,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
            (order_no, customer_name, customer_phone, package_id, expected_version_no, created_by, now, now),
        )
        return self.order_by_id(cursor.lastrowid)

    def freeze_order(self, order_id: int, *, version_id: int, version_no: int, snapshot: dict[str, Any], digest: str, actor: str, now: str, expected_row_version: int | None) -> int:
        if expected_row_version is None:
            cursor = self.connection.execute(
                "UPDATE wedding_orders SET status='confirmed',package_version_id=?,frozen_version_no=?,frozen_snapshot_json=?,frozen_digest=?,confirmed_by=?,confirmed_at=?,updated_at=?,row_version=row_version+1 WHERE id=? AND status='pending'",
                (version_id, version_no, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), digest, actor, now, now, order_id),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE wedding_orders SET status='confirmed',package_version_id=?,frozen_version_no=?,frozen_snapshot_json=?,frozen_digest=?,confirmed_by=?,confirmed_at=?,updated_at=?,row_version=row_version+1 WHERE id=? AND status='pending' AND row_version=?",
                (version_id, version_no, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), digest, actor, now, now, order_id, expected_row_version),
            )
        return cursor.rowcount

    def cancel_order(self, order_id: int, *, actor: str, reason: str, now: str) -> int:
        return self.connection.execute(
            "UPDATE wedding_orders SET status='cancelled',cancelled_by=?,cancel_reason=?,cancelled_at=?,updated_at=?,row_version=row_version+1 WHERE id=? AND status='pending'",
            (actor, reason, now, now, order_id),
        ).rowcount

    def add_order_event(self, *, order_id: int, action: str, actor: str, reason: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO wedding_order_events(order_id,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (order_id, action, actor, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def order_events(self, order_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM wedding_order_events WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()]

    def list_orders(self, *, status: str | None, package_code: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("o.status=?")
            values.append(status)
        if package_code:
            clauses.append("p.code=?")
            values.append(package_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT o.*,p.code AS package_code FROM wedding_orders o JOIN wedding_packages p ON p.id=o.package_id"
            + where
            + " ORDER BY o.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
