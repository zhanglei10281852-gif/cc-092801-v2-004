from __future__ import annotations

from datetime import UTC, datetime

from app.ceremony.service import CeremonyService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection


def service_item(provider: str, assignee: str, price: int, detail: str = "") -> dict:
    return {"provider": provider, "assignee": assignee, "price_cents": price, "detail": detail}


def content(*, photo_price=200000, mc="司仪-李明", caterer="喜宴餐饮A") -> dict:
    return {
        "services": {
            "venue_setup": service_item("花语布置", "布置组-王芳", 150000, "标准花艺"),
            "photography": service_item("光影摄影", "摄影师-赵磊", photo_price, "全天跟拍"),
            "mc": service_item("司仪团队", mc, 80000),
            "catering": service_item(caterer, "餐饮主管-陈静", 300000, "10 桌"),
        }
    }


def make_package(client) -> None:
    resp = client.post("/api/ceremony/packages?actor=顾问-周婷", json={"code": "sweet-2026", "name": "甜蜜 2026 套餐"})
    assert resp.status_code == 201, resp.text


def add_version(client, body_content, reason="供应商调整", *, base=None, actor="顾问-周婷"):
    payload = {"content": body_content, "change_reason": reason}
    if base is not None:
        payload["base_version"] = base
    resp = client.post("/api/ceremony/packages/sweet-2026/versions?actor=" + actor, json=payload)
    return resp


def publish(client, number, *, actor="管理员-郑强", reason="发布新版本"):
    return client.post(
        f"/api/ceremony/packages/sweet-2026/versions/{number}/publish?actor={actor}",
        json={"reason": reason},
    )


def test_version_snapshots_are_immutable_and_record_reason_and_operator(client):
    make_package(client)
    created = add_version(client, content(), "初版套餐拟定")
    assert created.status_code == 201, created.text
    version = created.json()
    assert version["version"] == 1
    assert version["status"] == "draft"
    assert version["created_by"] == "顾问-周婷"
    assert version["change_reason"] == "初版套餐拟定"
    assert version["content_digest"]
    assert version["content"]["total_price_cents"] == 730000
    # 快照行一旦写入不可变：内容相同的重复版本被拒绝
    duplicate = add_version(client, content(), "再次提交相同内容")
    assert duplicate.status_code == 409

    published = publish(client, 1)
    assert published.status_code == 200
    assert published.json()["status"] == "active"

    history = client.get("/api/ceremony/packages/sweet-2026/history").json()
    actions = [(e["action"], e["actor"]) for e in history["events"]]
    assert ("package_created", "顾问-周婷") in actions
    assert ("version_created", "顾问-周婷") in actions
    assert ("version_published", "管理员-郑强") in actions


def test_missing_required_category_is_rejected(client):
    make_package(client)
    bad = content()
    del bad["services"]["catering"]
    resp = add_version(client, bad, "缺少餐饮")
    assert resp.status_code == 422
    assert "餐饮" in resp.json()["error"]["context"]["missing"]


def test_compare_two_versions_reports_field_level_diff(client):
    make_package(client)
    add_version(client, content(), "初版")
    publish(client, 1)
    add_version(client, content(photo_price=260000, mc="司仪-周杰", caterer="喜宴餐饮B"), "摄影加价并更换司仪与餐饮")
    publish(client, 2, reason="供应商临时改价")

    comparison = client.get("/api/ceremony/packages/sweet-2026/compare?from=1&to=2").json()
    assert comparison["identical"] is False
    assert comparison["change_count"] == 3  # 摄影价格、司仪人员、餐饮供应商
    fields = {(c["category"], c["field"]) for c in comparison["changes"]}
    assert ("photography", "price_cents") in fields
    assert ("mc", "assignee") in fields
    assert ("catering", "provider") in fields
    assert comparison["price_delta_cents"] == 60000
    assert "摄影" in comparison["affected_categories"]


def test_confirmed_order_is_frozen_and_draft_changes_do_not_mutate_it(client):
    make_package(client)
    add_version(client, content(), "初版")
    publish(client, 1)

    order = client.post(
        "/api/ceremony/orders?actor=顾问-周婷",
        json={"order_no": "WED-0001", "package_code": "sweet-2026"},
    )
    assert order.status_code == 201, order.text
    confirmed = client.post(
        "/api/ceremony/orders/confirm?actor=新人-林悦",
        json={"order_no": "WED-0001"},
    )
    assert confirmed.status_code == 200, confirmed.text
    frozen = confirmed.json()
    assert frozen["status"] == "confirmed"
    assert frozen["frozen"] is True
    assert frozen["effective_version_number"] == 1
    assert frozen["total_price_cents"] == 730000
    assert frozen["confirmed_by"] == "新人-林悦"

    # 供应商改价/换人后发布新版本
    add_version(client, content(photo_price=260000, mc="司仪-周杰"), "供应商改价换人")
    publish(client, 2, reason="供应商临时改价")

    details = client.get("/api/ceremony/orders/by-no/WED-0001").json()
    # 已确认订单仍固定引用版本 1 的承诺内容
    assert details["effective_version_number"] == 1
    assert details["current_version_number"] == 2
    assert details["total_price_cents"] == 730000
    assert details["effective_version"]["content"]["services"]["mc"]["assignee"] == "司仪-李明"
    # 查询订单时可见生效版本与差异摘要
    diff = details["diff_summary"]
    assert diff is not None
    assert diff["new_total_price_cents"] == 790000
    assert diff["price_delta_cents"] == 60000
    assert ("mc", "assignee") in {(c["category"], c["field"]) for c in diff["changes"]}

    # 草稿修改不能改变已确认订单：禁止再改挂版本
    re_attach = client.put("/api/ceremony/orders/1/expected-version?actor=顾问-周婷", json={"version": 2})
    assert re_attach.status_code == 409


def test_concurrent_confirm_on_stale_version_raises_clear_conflict(client):
    make_package(client)
    add_version(client, content(), "初版")
    publish(client, 1)
    order = client.post(
        "/api/ceremony/orders?actor=顾问-周婷",
        json={"order_no": "WED-0002", "package_code": "sweet-2026", "expected_version": 1},
    ).json()
    assert order["expected_version_id"] is not None

    # 在确认之前，另一名顾问发布了版本 2
    add_version(client, content(photo_price=260000), "摄影加价")
    publish(client, 2, reason="改价")

    stale = client.post(
        "/api/ceremony/orders/confirm?actor=新人-林悦",
        json={"order_no": "WED-0002", "expected_version": 1},
    )
    assert stale.status_code == 409
    ctx = stale.json()["error"]["context"]
    assert ctx["expected_version"] == 1
    assert ctx["current_version"] == 2

    # 基于最新版本重新确认即可成功，且冻结的是版本 2
    fresh = client.post(
        "/api/ceremony/orders/confirm?actor=新人-林悦",
        json={"order_no": "WED-0002", "expected_version": 2},
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["effective_version_number"] == 2
    assert fresh.json()["total_price_cents"] == 790000


def test_withdraw_unused_version_allowed_but_referenced_blocked(client):
    make_package(client)
    add_version(client, content(), "初版")
    publish(client, 1)
    add_version(client, content(photo_price=220000), "小幅调价")
    publish(client, 2, reason="调价")
    # 版本 3 尚未被任何订单使用
    add_version(client, content(photo_price=240000), "再次调价")

    withdrawn = client.post(
        "/api/ceremony/packages/sweet-2026/versions/3/withdraw?actor=管理员-郑强",
        json={"reason": "录入错误，撤回未使用草稿"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["reference_counts"]["total"] == 0

    # 版本 1 已被确认订单引用，不能撤回
    client.post("/api/ceremony/orders?actor=顾问-周婷", json={"order_no": "WED-0003", "package_code": "sweet-2026"})
    client.post("/api/ceremony/orders/confirm?actor=新人-林悦", json={"order_no": "WED-0003"})
    blocked = client.post(
        "/api/ceremony/packages/sweet-2026/versions/2/withdraw?actor=管理员-郑强",
        json={"reason": "尝试撤回当前版本"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["references"]["confirmed"] >= 1


def test_lineage_and_audit_survive_service_restart(client):
    clock = FrozenClock(datetime(2026, 10, 1, 3, 0, tzinfo=UTC))
    service = CeremonyService(get_connection(), clock)
    service.create_package({"code": "forever", "name": "恒久套餐", "change_reason": ""}, "顾问-周婷")
    service.create_version(
        {"package_code": "forever", "content": content(), "change_reason": "初版", "base_version": None},
        "顾问-周婷",
    )
    service.publish_version("forever", 1, "管理员-郑强", "首发")
    service.create_version(
        {"package_code": "forever", "content": content(photo_price=260000), "change_reason": "改价", "base_version": 1},
        "顾问-周婷",
    )
    service.publish_version("forever", 2, "管理员-郑强", "改价发布")
    service.create_order({"order_no": "WED-0009", "package_code": "forever"}, "新人-林悦")
    service.confirm_order({"order_no": "WED-0009", "reason": "确认冻结"}, "新人-林悦")

    # 模拟服务重启：释放连接后用全新服务实例重新打开同一数据库文件
    close_connection()
    restarted = CeremonyService()
    versions = restarted.list_versions("forever")
    assert [v["version"] for v in versions["versions"]] == [1, 2]
    by_number = {v["version"]: v for v in versions["versions"]}
    assert by_number[2]["parent_version_id"] == by_number[1]["id"]
    assert by_number[1]["status"] == "superseded"
    assert by_number[2]["status"] == "active"
    # 快照内容与摘要完整恢复
    assert by_number[1]["content_digest"] != by_number[2]["content_digest"]

    order = restarted.get_order_by_no("WED-0009")
    assert order["effective_version_number"] == 2
    assert order["frozen"] is True

    package_events = restarted.package_history("forever")["events"]
    order_events = restarted.order_history("WED-0009")["events"]
    assert [e["action"] for e in order_events] == ["order_created", "order_confirmed"]
    assert any(e["action"] == "version_published" for e in package_events)
