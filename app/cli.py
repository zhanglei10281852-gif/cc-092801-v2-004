from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_ceremony_demo() -> int:
    def item(provider, assignee, price, detail=""):
        return {"provider": provider, "assignee": assignee, "price_cents": price, "detail": detail}

    def body(photo_price=200000):
        return {"services": {
            "venue_setup": item("花语布置", "王芳", 150000, "标准花艺"),
            "photography": item("光影摄影", "赵磊", photo_price, "全天跟拍"),
            "mc": item("司仪团队", "李明", 80000),
            "catering": item("喜宴餐饮A", "陈静", 300000, "10 桌"),
        }}

    with TestClient(app) as client:
        code = "ceremony-demo"
        made = client.post(f"/api/ceremony/packages?actor=cli-demo", json={"code": code, "name": "CLI 演示套餐"})
        if made.status_code not in {201, 409}:
            print(made.text)
            return 1
        v1 = client.post(f"/api/ceremony/packages/{code}/versions?actor=cli-demo",
                         json={"content": body(), "change_reason": "初版套餐"})
        if v1.status_code != 201:
            print(v1.text)
            return 1
        client.post(f"/api/ceremony/packages/{code}/versions/1/publish?actor=cli-demo", json={"reason": "首发"})
        client.post("/api/ceremony/orders?actor=cli-demo", json={"order_no": "ceremony-demo-0001", "package_code": code})
        confirmed = client.post("/api/ceremony/orders/confirm?actor=cli-demo",
                                json={"order_no": "ceremony-demo-0001"})
        client.post(f"/api/ceremony/packages/{code}/versions?actor=cli-demo",
                    json={"content": body(260000), "change_reason": "摄影临时加价"})
        client.post(f"/api/ceremony/packages/{code}/versions/2/publish?actor=cli-demo", json={"reason": "改价"})
        order = client.get("/api/ceremony/orders/by-no/ceremony-demo-0001")
    result = {
        "confirmed": confirmed.status_code,
        "frozen_version": order.json()["effective_version_number"],
        "current_version": order.json()["current_version_number"],
        "frozen_price_cents": order.json()["total_price_cents"],
        "diff": None if order.json()["diff_summary"] is None else {
            "change_count": order.json()["diff_summary"]["change_count"],
            "price_delta_cents": order.json()["diff_summary"]["price_delta_cents"],
        },
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["frozen_version"] == 1 and result["current_version"] == 2 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="ceremony-operations", description="红白喜事服务运营平台维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("ceremony-demo", help="执行套餐版本冻结演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "ceremony-demo": command_ceremony_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
