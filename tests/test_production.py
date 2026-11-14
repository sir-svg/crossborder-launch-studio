import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from studio.main import create_app
from studio.model import ModelFailure, Settings
from studio.schemas import TaskInput
from studio.store import Store
from tests.test_studio import wait_for

OWNER_PASSWORD = "Test owner password 47"
REVIEW_PASSWORD = "Test reviewer password 62"


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path):
    if request.param == "sqlite":
        yield str(tmp_path / "test.db"), ""
        return
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to test PostgreSQL")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield str(tmp_path / "unused.db"), make_conninfo(url, options=f"-csearch_path={name}")
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def login(client, email, password):
    reply = client.post("/api/auth/login", json={"email": email, "password": password})
    assert reply.status_code == 200, reply.json()
    return reply.json()["workspace"]["id"]


def test_team_review_revisions_and_workspace_isolation(database):
    path, url = database
    settings = Settings(
        _env_file=None,
        database_path=path,
        database_url=url,
        auth_enabled=True,
        bootstrap_email="owner@example.test",
        bootstrap_password=OWNER_PASSWORD,
        llm_api_key="",
    )
    with TestClient(create_app(settings)) as owner:
        assert owner.get("/api/products").status_code == 401
        original_space = login(owner, "owner@example.test", OWNER_PASSWORD)
        response = owner.post(
            "/api/members",
            json={
                "email": "reviewer@example.test",
                "display_name": "审核员",
                "role": "reviewer",
                "password": REVIEW_PASSWORD,
            },
        )
        assert response.status_code == 201, response.json()
        assert owner.get("/api/products").json() == []
        product_data = {
            "name": "桌面笔架",
            "sku": "P-1",
            "keywords": ["pen rack"],
            "facts": [{"label": "尺寸", "value": "22 cm"}],
            "features": ["钢材", "哑光黑", "可整理文具", "开放式取用", "适合桌面"],
        }
        product = owner.post("/api/products", json=product_data).json()
        product_url = "/api/products/" + product["id"]
        assert owner.put(product_url, json=product_data).status_code == 428
        changes = {**product_data, "name": "桌面笔架新版"}
        assert (
            owner.put(product_url, json=changes, headers={"If-Match": '"1"'}).json()["revision"]
            == 2
        )
        assert (
            owner.put(product_url, json=product_data, headers={"If-Match": "1"}).status_code == 409
        )
        revisions = owner.get(product_url + "/revisions").json()
        assert [item["revision"] for item in revisions] == [2, 1]
        response = owner.post(
            "/api/tasks",
            json={
                "product_id": product["id"],
                "market": "US",
                "language": "en-US",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202
        task_url = "/api/tasks/" + response.json()["id"]
        task = wait_for(owner, task_url)
        assert task["snapshot"]["revision"] == 2
        assert owner.post(task_url + "/approve", json={"version": 1}).status_code == 404
        assert owner.post(task_url + "/submit-review", json={"version": 1}).status_code == 200
        approval = {"version": 1, "decision": "approved", "note": "已核对"}
        assert owner.post(task_url + "/review", json=approval).status_code == 409
        # Use a separate cookie jar through the same running application.
        reviewer = TestClient(owner.app)
        try:
            assert login(reviewer, "reviewer@example.test", REVIEW_PASSWORD) == original_space
            assert reviewer.post("/api/products", json=product_data).status_code == 403
            assert reviewer.get("/api/audit").status_code == 403
            assert (
                reviewer.post(
                    task_url + "/review",
                    json={
                        "version": 1,
                        "decision": "rejected",
                        "note": "",
                    },
                ).status_code
                == 422
            )
            reject = {"version": 1, "decision": "rejected", "note": "标题需要修改"}
            assert (
                reviewer.post(task_url + "/review", json=reject).json()["review_status"]
                == "rejected"
            )
            assert owner.get(task_url + "/export/csv?version=1").status_code == 409
            draft = task["versions"][0]["draft"]
            draft["title"] = "Reviewed Pen Rack"
            assert (
                owner.put(task_url + "/draft", json={"base_version": 1, "draft": draft}).status_code
                == 200
            )
            assert owner.post(task_url + "/submit-review", json={"version": 2}).status_code == 200
            approve = reviewer.post(task_url + "/review", json={**approval, "version": 2})
            assert approve.json()["review_status"] == "approved"
            assert "Reviewed Pen Rack" in reviewer.get(task_url + "/export/md?version=2").text
        finally:
            reviewer.close()
        events = owner.get("/api/audit").json()
        assert {"review.rejected", "draft.exported", "product.updated"} <= {
            e["action"] for e in events
        }
        assert owner.get("/api/operations").json()["reviews"]["approved"] == 1
        other = owner.post("/api/auth/workspaces", json={"name": "另一个团队"}).json()["workspace"][
            "id"
        ]
        assert other != original_space
        for route in (product_url, product_url + "/revisions", task_url, task_url + "/events"):
            assert owner.get(route).status_code == 404
        assert owner.get(task_url + "/export/md?version=2").status_code == 404
        assert (
            owner.post(
                "/api/products",
                json=product_data,
                headers={
                    "X-Workspace-ID": original_space,
                },
            ).status_code
            == 409
        )
        assert owner.post("/api/products", json=product_data).status_code == 201


def test_lease_expires_and_only_current_worker_can_commit(database):
    path, url = database
    store = Store(path, url)
    store.initialize()
    try:
        product = store.products()[0]
        request = TaskInput(
            product_id=product["id"], market="US", language="en-US", request_id=uuid4()
        )
        with ThreadPoolExecutor(max_workers=4) as pool:
            submitted = list(pool.map(lambda _: store.submit(request), range(4)))
            assert len(set(submitted)) == 1
            claimed = [
                task for task in pool.map(lambda i: store.claim(worker_id=str(i)), range(4)) if task
            ]
        (first,) = claimed
        assert first["attempt"] == 1
        assert store.heartbeat(first["id"], first["lease_token"])
        with store.connect(write=True) as db:
            db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (first["id"],))
        second = store.claim(worker_id="replacement")
        assert second["id"] == first["id"] and second["attempt"] == 2
        assert not store.heartbeat(first["id"], first["lease_token"])
        assert not store.step(
            first["id"], "load", "completed", "stale", lease_token=first["lease_token"]
        )
        assert not store.fail(first["id"], "stale", lease_token=first["lease_token"])
        assert store.fail(second["id"], "verified failure", lease_token=second["lease_token"])
        assert store.task(first["id"])["error"] == "verified failure"
        assert any(event["attempt"] == 2 for event in store.task(first["id"])["events"])
    finally:
        store.close()


def test_production_rejects_demo_configuration(tmp_path):
    settings = Settings(
        _env_file=None, app_environment="production", database_path=str(tmp_path / "failed.db")
    )
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        create_app(settings)
    configured = settings.model_copy(
        update={
            "auth_enabled": True,
            "database_url": Settings(_env_file=None, database_url="postgresql://test").database_url,
            "public_origin": "https://studio.example.test",
            "allowed_hosts": "studio.example.test,127.0.0.1",
            "allow_demo": False,
        }
    )
    with pytest.raises(ModelFailure):
        create_app(configured)
