from __future__ import annotations

import json
import sqlite3
from typing import Any

PACKAGE_VERSION_COLUMNS = (
    "id,package_id,version,parent_version_id,content_json,content_digest,status,"
    "change_reason,created_by,created_at,published_at,withdrawn_at,withdrawn_by,withdraw_reason"
)


class CeremonyRepository:
    """封装婚庆套餐版本与订单冻结领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 套餐 ----
    def package_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ceremony_packages WHERE code=?", (code,)).fetchone()

    def package_by_id(self, package_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM ceremony_packages WHERE id=?", (package_id,)).fetchone()

    def list_packages(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM ceremony_packages ORDER BY id").fetchall()]

    def create_package(self, *, code: str, name: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_packages(code,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
            (code, name, created_by, now, now),
        )
        return dict(self.package_by_id(cursor.lastrowid))

    def touch_package(self, package_id: int, now: str) -> None:
        self.connection.execute("UPDATE ceremony_packages SET updated_at=? WHERE id=?", (now, package_id))

    # ---- 套餐版本 ----
    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT {PACKAGE_VERSION_COLUMNS} FROM ceremony_package_versions WHERE id=?", (version_id,)
        ).fetchone()

    def version_by_number(self, package_id: int, number: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT {PACKAGE_VERSION_COLUMNS} FROM ceremony_package_versions WHERE package_id=? AND version=?",
            (package_id, number),
        ).fetchone()

    def list_versions(self, package_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {PACKAGE_VERSION_COLUMNS} FROM ceremony_package_versions WHERE package_id=? ORDER BY version",
            (package_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def next_version_number(self, package_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM ceremony_package_versions WHERE package_id=?", (package_id,)
        ).fetchone()[0])

    def active_version(self, package_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT {PACKAGE_VERSION_COLUMNS} FROM ceremony_package_versions "
            "WHERE package_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (package_id,),
        ).fetchone()

    def open_draft(self, package_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            f"SELECT {PACKAGE_VERSION_COLUMNS} FROM ceremony_package_versions "
            "WHERE package_id=? AND status='draft' ORDER BY version DESC LIMIT 1",
            (package_id,),
        ).fetchone()

    def create_version(self, *, package_id: int, number: int, parent_version_id: int | None, content: dict[str, Any],
                       content_digest: str, change_reason: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_package_versions(package_id,version,parent_version_id,content_json,content_digest,"
            "status,change_reason,created_by,created_at) VALUES(?,?,?,?,?, 'draft',?,?,?)",
            (package_id, number, parent_version_id, json.dumps(content, ensure_ascii=False, sort_keys=True),
             content_digest, change_reason, created_by, now),
        )
        return dict(self.version_by_id(cursor.lastrowid))

    def mark_active(self, version_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_package_versions SET status='active',published_at=? WHERE id=?", (now, version_id)
        )

    def mark_withdrawn(self, version_id: int, *, actor: str, reason: str, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_package_versions SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=? WHERE id=?",
            (now, actor, reason, version_id),
        )

    def add_package_event(self, *, package_id: int, version_id: int | None, action: str, actor: str, reason: str,
                          before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO ceremony_package_events(package_id,version_id,action,actor,reason,before_json,after_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (package_id, version_id, action, actor, reason,
             json.dumps(before, ensure_ascii=False, sort_keys=True),
             json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def package_events(self, package_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ceremony_package_events WHERE package_id=? ORDER BY id", (package_id,)
        ).fetchall()]

    def version_reference_counts(self, version_id: int) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS amount FROM ceremony_orders WHERE package_version_id=? GROUP BY status",
            (version_id,),
        ).fetchall()
        counts = {str(row["status"]): int(row["amount"]) for row in rows}
        counts["total"] = sum(counts.values())
        counts["draft_expected"] = int(self.connection.execute(
            "SELECT COUNT(*) FROM ceremony_orders WHERE expected_version_id=? AND package_version_id IS NULL",
            (version_id,),
        ).fetchone()[0])
        return counts

    # ---- 订单 ----
    def create_order(self, *, order_no: str, package_id: int, expected_version_id: int | None, created_by: str,
                     now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_orders(order_no,package_id,package_version_id,status,expected_version_id,"
            "created_by,created_at,updated_at) VALUES(?,?,NULL,'draft',?,?,?,?)",
            (order_no, package_id, expected_version_id, created_by, now, now),
        )
        return dict(self.order_by_id(cursor.lastrowid))

    def order_by_id(self, order_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT o.*,p.code AS package_code,p.name AS package_name,v.version AS pinned_version "
            "FROM ceremony_orders o JOIN ceremony_packages p ON p.id=o.package_id "
            "LEFT JOIN ceremony_package_versions v ON v.id=o.package_version_id WHERE o.id=?",
            (order_id,),
        ).fetchone()

    def order_by_no(self, order_no: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT o.*,p.code AS package_code,p.name AS package_name,v.version AS pinned_version "
            "FROM ceremony_orders o JOIN ceremony_packages p ON p.id=o.package_id "
            "LEFT JOIN ceremony_package_versions v ON v.id=o.package_version_id WHERE o.order_no=?",
            (order_no,),
        ).fetchone()

    def list_orders(self, *, status: str | None = None, package_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("o.status=?")
            values.append(status)
        if package_id is not None:
            clauses.append("o.package_id=?")
            values.append(package_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT o.*,p.code AS package_code,p.name AS package_name,v.version AS pinned_version "
            "FROM ceremony_orders o JOIN ceremony_packages p ON p.id=o.package_id "
            "LEFT JOIN ceremony_package_versions v ON v.id=o.package_version_id"
            + where + " ORDER BY o.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    def set_expected_version(self, order_id: int, version_id: int | None, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_orders SET expected_version_id=?,updated_at=? WHERE id=?", (version_id, now, order_id)
        )

    def confirm_order(self, order_id: int, version_id: int, total_price_cents: int, actor: str, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_orders SET status='confirmed',package_version_id=?,expected_version_id=?,"
            "total_price_cents=?,confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
            (version_id, version_id, total_price_cents, actor, now, now, order_id),
        )

    def add_order_event(self, *, order_id: int, action: str, actor: str, reason: str,
                        before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO ceremony_order_events(order_id,action,actor,reason,before_json,after_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (order_id, action, actor, reason,
             json.dumps(before, ensure_ascii=False, sort_keys=True),
             json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def order_events(self, order_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM ceremony_order_events WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()]
