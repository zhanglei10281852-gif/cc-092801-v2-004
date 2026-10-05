from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.packages.service import WeddingPackageService


def component(provider: str, *, staff: str = "默认人员", spec: str = "标准规格", price: float = 1000.0, note: str = "") -> dict:
    return {"provider": provider, "staff": staff, "spec": spec, "price": price, "note": note}


PACKAGE_V1 = {
    "code": "romance-classic",
    "name": "浪漫经典套餐",
    "venue": component("花海礼堂", staff="王布置", price=8000.0),
    "photography": component("光影工作室", staff="李摄影", price=5000.0),
    "host": component("金话筒团队", staff="张司仪", price=3000.0),
    "catering": component("喜临门餐饮", staff="周主厨", spec="每桌 2888 元", price=28880.0),
    "extras": {"婚车": "宝马五系 x4"},
}

PACKAGE_V2 = {
    "name": "浪漫经典套餐（升级）",
    "venue": component("花海礼堂", staff="王布置", spec="增加 T 台花艺", price=8800.0),
    "photography": component("光影工作室", staff="陈摄影（供应商换人）", price=5600.0),
    "host": component("金话筒团队", staff="张司仪", price=3000.0),
    "catering": component("喜临门餐饮", staff="周主厨", spec="每桌 3088 元", price=30880.0),
    "extras": {"婚车": "奔驰 E 级 x4"},
}


def create_package(client) -> dict:
    response = client.post("/api/wedding/packages?actor=顾问-小赵", json=PACKAGE_V1)
    assert response.status_code == 201, response.text
    return response.json()


def save_draft(client, body: dict, *, actor: str = "顾问-小赵"):
    return client.post(f"/api/wedding/packages/{PACKAGE_V1['code']}/drafts?actor={actor}", json=body)


def order_payload(order_no: str, version_no: int = 1) -> dict:
    return {
        "order_no": order_no,
        "customer_name": "钱家&孙家",
        "customer_phone": "13800000000",
        "package_code": PACKAGE_V1["code"],
        "expected_version_no": version_no,
        "created_by": "顾问-小赵",
    }


def test_package_created_with_immutable_v1_snapshot(client):
    package = create_package(client)
    assert package["current_published_version_no"] == 1
    version = package["versions"][0]
    assert version["status"] == "published"
    assert version["created_by"] == "顾问-小赵"
    assert version["change_reason"] == "初始套餐"
    assert version["snapshot"]["photography"]["staff"] == "李摄影"
    assert len(version["snapshot_digest"]) == 64
    # 初始版本即一条可追溯审计记录
    timeline = client.get(f"/api/wedding/packages/{PACKAGE_V1['code']}/timeline").json()
    assert [item["action"] for item in timeline["audit"]] == ["package.create"]


def test_draft_edit_does_not_change_published_service_content(client):
    package = create_package(client)
    v1_digest = package["versions"][0]["snapshot_digest"]
    order = client.post("/api/wedding/orders", json=order_payload("ORD-0001")).json()

    response = save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    assert response.status_code == 201, response.text
    updated = response.json()
    assert updated["draft_version_no"] == 2
    assert updated["current_published_version_no"] == 1

    # 草稿状态下已下单内容仍然引用 v1，且草稿不允许重复开立
    duplicate = save_draft(client, {**PACKAGE_V2, "change_reason": "再次调整"})
    assert duplicate.status_code == 409

    detail = client.get(f"/api/wedding/orders/{order['id']}").json()
    assert detail["expected_version_no"] == 1
    assert detail["effective_version_no"] is None
    assert "frozen_snapshot" not in detail

    # 发布草稿后 v1 快照仍保持不可变
    publish = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "十月档期调价生效"},
    )
    assert publish.status_code == 200
    again = client.get(f"/api/wedding/packages/{PACKAGE_V1['code']}").json()
    assert again["current_published_version_no"] == 2
    assert again["versions"][0]["snapshot_digest"] == v1_digest
    assert again["versions"][0]["snapshot"]["photography"] == PACKAGE_V1["photography"]
    assert again["versions"][1]["parent_version_id"] == again["versions"][0]["id"]


def test_order_confirm_freezes_version_and_query_shows_diff_summary(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-FREEZE-1")).json()

    confirmed = client.post(
        f"/api/wedding/orders/{order['id']}/confirm",
        json={"actor": "顾问-小赵", "expected_version_no": 1},
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "confirmed"
    assert body["effective_version_no"] == 1
    assert body["frozen_snapshot"]["catering"]["spec"] == "每桌 2888 元"
    assert len(body["frozen_digest"]) == 64
    assert [event["action"] for event in body["events"]] == ["order.create", "order.confirm"]
    assert body["events"][-1]["actor"] == "顾问-小赵"

    # 供应商改价换人并发布 v2
    save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "新档期价格"},
    )

    detail = client.get(f"/api/wedding/orders/{order['id']}").json()
    # 已确认订单仍按当时承诺执行
    assert detail["effective_version_no"] == 1
    assert detail["current_published_version_no"] == 2
    assert detail["frozen_snapshot"]["photography"]["staff"] == "李摄影"
    diff = detail["diff_to_current"]
    assert diff["from_version_no"] == 1 and diff["to_version_no"] == 2
    assert "photography.staff" in diff["changed_paths"]
    assert "photography.price" in diff["changed_paths"]
    assert "host.price" not in diff["changed_paths"]
    assert set(diff["components_changed"]) == {"venue", "photography", "catering", "extras"}
    expected_delta = round((8800 - 8000) + (5600 - 5000) + (30880 - 28880) + 0, 2)
    assert diff["price_delta"]["total"] == expected_delta

    listing = client.get("/api/wedding/orders").json()["items"]
    assert listing[0]["id"] == order["id"]
    assert listing[0]["effective_version_no"] == 1
    assert listing[0]["diff_to_current"]["price_delta"]["total"] == expected_delta


def test_confirmed_order_cannot_be_reconfirmed_or_mutated(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-LOCK-1")).json()
    client.post(f"/api/wedding/orders/{order['id']}/confirm", json={"actor": "顾问-小赵", "expected_version_no": 1})

    repeat = client.post(f"/api/wedding/orders/{order['id']}/confirm", json={"actor": "顾问-小赵", "expected_version_no": 1})
    assert repeat.status_code == 409
    assert repeat.json()["error"]["context"]["frozen_version_no"] == 1

    cancel = client.post(f"/api/wedding/orders/{order['id']}/cancel", json={"actor": "顾问-小赵", "reason": "客户改期"})
    assert cancel.status_code == 409

    retarget = client.post(
        f"/api/wedding/orders/{order['id']}/retarget",
        json={"actor": "顾问-小赵", "expected_version_no": 2, "reason": "想升级"},
    )
    assert retarget.status_code == 409


def test_concurrent_confirm_based_on_stale_version_conflicts(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-STALE-1")).json()

    # 先发布更新版本，仍按旧版本确认必须明确报冲突
    save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "新档期价格"},
    )
    stale = client.post(
        f"/api/wedding/orders/{order['id']}/confirm",
        json={"actor": "顾问-小赵", "expected_version_no": 1},
    )
    assert stale.status_code == 409
    context = stale.json()["error"]["context"]
    assert context == {"expected_version_no": 1, "latest_published_version_no": 2}

    # 显式改单到新版本（留下审计），随后确认成功
    retarget = client.post(
        f"/api/wedding/orders/{order['id']}/retarget",
        json={"actor": "顾问-小赵", "expected_version_no": 2, "reason": "客户已知晓并接受新价格"},
    )
    assert retarget.status_code == 200
    assert retarget.json()["expected_version_no"] == 2
    confirmed = client.post(
        f"/api/wedding/orders/{order['id']}/confirm",
        json={"actor": "顾问-小赵", "expected_version_no": 2},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["effective_version_no"] == 2
    actions = [event["action"] for event in confirmed.json()["events"]]
    assert actions == ["order.create", "order.retarget", "order.confirm"]


def test_parallel_confirm_only_one_wins_with_clear_conflict(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-RACE-1")).json()
    results: list[dict] = []
    errors: list[dict] = []

    def worker() -> None:
        close_connection()
        service = WeddingPackageService(get_connection(), FrozenClock(datetime(2026, 10, 5, tzinfo=UTC)))
        try:
            value = service.confirm_order(order["id"], {"actor": f"顾问-{threading.get_ident()}", "expected_version_no": 1, "expected_row_version": None})
            results.append(value)
        except Exception as exc:  # noqa: BLE001 - 并发路径需要收集冲突结果
            errors.append({"code": exc.code, "message": exc.message})  # type: ignore[attr-defined]

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0]["code"] == "conflict"
    detail = client.get(f"/api/wedding/orders/{order['id']}").json()
    assert detail["status"] == "confirmed"
    assert detail["effective_version_no"] == 1


def test_compare_versions_returns_field_level_diff(client):
    create_package(client)
    save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    diff = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/diff",
        json={"from_version_no": 1, "to_version_no": 2},
    )
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["from_status"] == "published"
    assert body["to_status"] == "draft"
    staff_change = [item for item in body["changes"] if item["path"] == "photography.staff"][0]
    assert staff_change["from"] == "李摄影"
    assert staff_change["to"] == "陈摄影（供应商换人）"
    assert body["price_delta"]["by_component"]["catering"] == 2000.0
    missing = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/diff",
        json={"from_version_no": 1, "to_version_no": 9},
    )
    assert missing.status_code == 404
    same = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/diff",
        json={"from_version_no": 1, "to_version_no": 1},
    )
    assert same.status_code == 422


def test_withdraw_unused_version_blocked_when_referenced(client):
    create_package(client)

    # 没有任何引用时可以撤回尚未使用的版本
    withdrawn = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/1/withdraw",
        json={"actor": "管理员-老周", "reason": "停止售卖该旧套餐"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["versions"][0]["status"] == "withdrawn"

    # 撤回后不能再按它下单
    rejected = client.post("/api/wedding/orders", json=order_payload("ORD-WD-1"))
    assert rejected.status_code == 409

    # 已撤回版本不能重复撤回
    again = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/1/withdraw",
        json={"actor": "管理员-老周", "reason": "再次撤回"},
    )
    assert again.status_code == 409

    # 新版本被待确认订单引用时禁止撤回
    save_draft(client, {**PACKAGE_V2, "change_reason": "重新上架升级版"})
    client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "上架"},
    )
    order = client.post("/api/wedding/orders", json=order_payload("ORD-WD-2", version_no=2)).json()
    blocked = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/2/withdraw",
        json={"actor": "管理员-老周", "reason": "想撤回"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["pending_orders"] == 1

    # 取消订单后版本恢复为“未使用”，可以撤回
    client.post(f"/api/wedding/orders/{order['id']}/cancel", json={"actor": "顾问-小赵", "reason": "客户流失"})
    freed = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/2/withdraw",
        json={"actor": "管理员-老周", "reason": "无人引用后撤回"},
    )
    assert freed.status_code == 200
    timeline = client.get(f"/api/wedding/packages/{PACKAGE_V1['code']}/timeline").json()
    assert "version.withdraw" in [item["action"] for item in timeline["audit"]]
    withdraw_events = [item for item in timeline["audit"] if item["action"] == "version.withdraw"]
    assert withdraw_events[0]["actor"] == "管理员-老周"
    assert withdraw_events[0]["reason"] == "停止售卖该旧套餐"


def test_confirmed_version_cannot_be_withdrawn(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-KEEP-1")).json()
    client.post(f"/api/wedding/orders/{order['id']}/confirm", json={"actor": "顾问-小赵", "expected_version_no": 1})
    blocked = client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/versions/1/withdraw",
        json={"actor": "管理员-老周", "reason": "想撤回已承诺版本"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["confirmed_orders"] == 1


def test_only_published_version_can_be_ordered(client):
    package = create_package(client)
    draft = save_draft(client, {**PACKAGE_V2, "change_reason": "草稿不可下单"})
    assert draft.status_code == 201
    response = client.post("/api/wedding/orders", json=order_payload("ORD-DRAFT-1", version_no=2))
    assert response.status_code == 409
    missing = client.post("/api/wedding/orders", json=order_payload("ORD-DRAFT-2", version_no=99))
    assert missing.status_code == 404
    unknown = client.post(
        "/api/wedding/orders",
        json={**order_payload("ORD-DRAFT-3"), "package_code": "does-not-exist"},
    )
    assert unknown.status_code == 404
    assert package["code"] == "romance-classic"


def test_stale_row_version_on_confirm_is_rejected(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-OPT-1")).json()
    assert order["row_version"] == 1

    save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "新档期价格"},
    )
    retarget = client.post(
        f"/api/wedding/orders/{order['id']}/retarget",
        json={"actor": "顾问-小赵", "expected_version_no": 2, "reason": "客户接受新价格"},
    )
    assert retarget.status_code == 200
    assert retarget.json()["row_version"] == 2

    stale = client.post(
        f"/api/wedding/orders/{order['id']}/confirm",
        json={"actor": "顾问-小赵", "expected_version_no": 2, "expected_row_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["context"]["current_row_version"] == 2

    fresh = client.post(
        f"/api/wedding/orders/{order['id']}/confirm",
        json={"actor": "顾问-小赵", "expected_version_no": 2, "expected_row_version": 2},
    )
    assert fresh.status_code == 200
    assert fresh.json()["effective_version_no"] == 2


def test_version_chain_audit_and_frozen_order_survive_restart(client):
    create_package(client)
    order = client.post("/api/wedding/orders", json=order_payload("ORD-RESTART-1")).json()
    client.post(f"/api/wedding/orders/{order['id']}/confirm", json={"actor": "顾问-小赵", "expected_version_no": 1})
    save_draft(client, {**PACKAGE_V2, "change_reason": "供应商临时改价并替换摄影"})
    client.post(
        f"/api/wedding/packages/{PACKAGE_V1['code']}/publish",
        json={"actor": "管理员-老周", "reason": "新档期价格"},
    )

    # 模拟服务重启：释放当前进程连接后用全新连接读取同一个 SQLite 文件
    close_connection()
    service = WeddingPackageService(get_connection())

    package = service.get_package(PACKAGE_V1["code"])
    assert [v["version_no"] for v in package["versions"]] == [1, 2]
    assert package["versions"][0]["status"] == "published"
    assert package["versions"][1]["parent_version_id"] == package["versions"][0]["id"]
    assert package["current_published_version_no"] == 2
    assert {item["action"] for item in service.package_timeline(PACKAGE_V1["code"])["audit"]} == {
        "package.create",
        "version.draft",
        "version.publish",
    }

    restored = service.get_order(order["id"])
    assert restored["status"] == "confirmed"
    assert restored["effective_version_no"] == 1
    assert restored["frozen_snapshot"]["photography"]["staff"] == "李摄影"
    assert restored["diff_to_current"]["to_version_no"] == 2
    assert [event["action"] for event in restored["events"]] == ["order.create", "order.confirm"]
