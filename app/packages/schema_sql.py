from __future__ import annotations

from app.database import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS wedding_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wedding_package_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES wedding_packages(id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','withdrawn')),
    parent_version_id INTEGER REFERENCES wedding_package_versions(id),
    snapshot_json TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_by TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    withdrawn_by TEXT NOT NULL DEFAULT '',
    withdrawn_at TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(package_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_wedding_versions_status ON wedding_package_versions(package_id,status,version_no);
CREATE TABLE IF NOT EXISTS wedding_package_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL,
    version_no INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wedding_package_audit_pkg ON wedding_package_audit(package_id,id);
CREATE TABLE IF NOT EXISTS wedding_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL DEFAULT '',
    package_id INTEGER NOT NULL REFERENCES wedding_packages(id) ON DELETE RESTRICT,
    expected_version_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','cancelled')),
    package_version_id INTEGER REFERENCES wedding_package_versions(id),
    frozen_version_no INTEGER,
    frozen_snapshot_json TEXT,
    frozen_digest TEXT,
    confirmed_by TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    cancelled_by TEXT NOT NULL DEFAULT '',
    cancelled_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wedding_orders_status ON wedding_orders(status,id);
CREATE INDEX IF NOT EXISTS idx_wedding_orders_version ON wedding_orders(package_version_id);
CREATE TABLE IF NOT EXISTS wedding_order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES wedding_orders(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wedding_order_events_order ON wedding_order_events(order_id,id);
"""


def ensure_schema() -> None:
    """幂等建表：服务重启后版本关系与审计记录仍可从 SQLite 完整恢复。"""
    get_connection().executescript(SCHEMA)
