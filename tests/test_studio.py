import csv
import io
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from studio.main import create_app
from studio.model import Settings
from studio.schemas import Draft, Insights


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_path=str(tmp_path / "studio.db"), llm_api_key="")


def submit(client, product_index=0, market="US", language="en-US"):
    product = client.get("/api/products").json()[product_index]
    body = {
        "product_id": product["id"],
        "market": market,
        "language": language,
        "request_id": str(uuid4()),
    }
    response = client.post("/api/tasks", json=body)
    assert response.status_code == 202
    return "/api/tasks/" + response.json()["id"], body


def wait_for(client, path, status="completed"):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        task = client.get(path).json()
        if task["status"] in {"completed", "failed"}:
            assert task["status"] == status, task.get("error")
            return task
        time.sleep(0.05)
    pytest.fail("任务没有进入预期状态")


def test_version_approval_export_and_reopening(settings):
    with TestClient(create_app(settings)) as client:
        path, body = submit(client)
        assert client.post("/api/tasks", json=body).json()["id"] == path.rsplit("/", 1)[-1]
        task = wait_for(client, path)
        assert task["mode"] == "sample"
        assert any(x["field"] == "承重" for x in task["versions"][0]["checks"])
        complete = [e["step"] for e in task["events"] if e["status"] == "completed" and e["step"]]
        assert complete == ["load", "insights", "draft", "check"]
        stream = client.get(path + "/events", params={"since": task["events"][0]["seq"]}).text
        assert "event: end" in stream
        assert "开始执行上新流程" in stream
        assert client.get(path + "/export/csv?version=1").status_code == 409
        assert client.post(path + "/approve", json={"version": 1}).status_code == 200
        draft = task["versions"][0]["draft"]
        draft["title"] = "Desk Shelf, Seller Reviewed"
        assert client.put(path + "/draft", json={"base_version": 1, "draft": draft}).json() == {
            "version": 2
        }
        assert (
            client.put(path + "/draft", json={"base_version": 1, "draft": draft}).status_code == 409
        )
        assert client.get(path + "/export/csv?version=1").status_code == 409
        assert client.get(path + "/export/csv?version=2").status_code == 409
        assert client.post(path + "/approve", json={"version": 2}).status_code == 200
        download = client.get(path + "/export/csv?version=2")
        data = next(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))
        assert data["Title"] == draft["title"] and data["Version"] == "2"
        assert download.headers["content-disposition"].endswith('LS-1001-US-v2.csv"')
    with TestClient(create_app(settings)) as client:
        reopened = client.get(path).json()
        assert reopened["versions"][0]["approved_at"]
        assert reopened["versions"][0]["draft"]["title"] == draft["title"]
        assert len(reopened["versions"]) == 2


def test_import_and_snapshot_across_market(settings):
    with TestClient(create_app(settings)) as client:
        path, body = submit(client, 1, "DE", "de-DE")
        product_url = "/api/products/" + body["product_id"]
        data = "id,rating,text,source\nR300,5,Works well,CSV\nR301,9,Invalid,CSV\n"
        assert client.post(product_url + "/reviews", json={"csv_text": data}).status_code == 422
        assert len(client.get(product_url).json()["reviews"]) == 2
        data = 'id,rating,text,source\nR300,5,"Works well, even on trips",CSV\n'
        assert client.post(product_url + "/reviews", json={"csv_text": data}).json()["count"] == 3
        assert client.post(product_url + "/reviews", json={"csv_text": data}).json()["count"] == 3
        old = wait_for(client, path)
        assert len(old["snapshot"]["reviews"]) == 2
        assert "Faltbare" in old["versions"][0]["draft"]["title"]
        next_path, _ = submit(client, 1, "DE", "de-DE")
        assert len(wait_for(client, next_path)["snapshot"]["reviews"]) == 3
        response = client.post("/api/tasks", json={**body, "language": "en-US"})
        assert response.status_code == 422


class ReviewModel:
    enabled = True
    good_reference = False

    async def generate(self, _prompt, _data, schema):
        if schema is Insights:
            return Insights(
                findings=[
                    {
                        "kind": "positive",
                        "text": "便于拿取",
                        "review_ids": ["R102" if self.good_reference else "R999"],
                    }
                ]
            )
        return Draft(
            title="Desk Shelf",
            bullets=["Open shelves", "Desk storage", "Black", "Steel", "32 cm"],
            description="Open shelves for stationery.",
            search_terms="desk shelf",
        )


def test_missing_citation_fails_and_retry_keeps_original_snapshot(settings):
    model = ReviewModel()
    with TestClient(create_app(settings, model)) as client:
        path, _ = submit(client)
        failed = wait_for(client, path, "failed")
        assert "引用" in failed["error"] and failed["versions"] == []
        model.good_reference = True
        request = {"request_id": str(uuid4())}
        retry = client.post(path + "/retry", json=request).json()["id"]
        assert client.post(path + "/retry", json=request).json()["id"] == retry
        task = wait_for(client, "/api/tasks/" + retry)
        assert task["retry_of"] == failed["id"]
        assert task["snapshot"] == failed["snapshot"]
        assert task["insights"]["findings"][0]["review_ids"] == ["R102"]
        assert task["mode"] == "model"


def test_incomplete_template_cannot_be_approved(settings):
    with TestClient(create_app(settings)) as client:
        product = client.post("/api/products", json={"sku": "NEW-1", "name": "测试商品"}).json()
        response = client.post(
            "/api/tasks",
            json={
                "product_id": product["id"],
                "market": "US",
                "language": "en-US",
                "request_id": str(uuid4()),
            },
        )
        path = "/api/tasks/" + response.json()["id"]
        task = wait_for(client, path)
        assert task["mode"] == "template"
        assert any(check["level"] == "error" for check in task["versions"][0]["checks"])
        assert client.post(path + "/approve", json={"version": 1}).status_code == 409
