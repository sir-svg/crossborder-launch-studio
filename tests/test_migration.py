import json
import sqlite3
from contextlib import closing

from studio.store import Store


def test_first_release_sqlite_data_and_approved_version_survive_upgrade(tmp_path):
    path = tmp_path / "original.db"
    product = {
        "sku": "OLD-1",
        "name": "已有商品",
        "facts": [],
        "features": [],
        "keywords": [],
        "source": "测试",
    }
    inputs = {"product_id": "product-old", "market": "US", "language": "en-US", "notes": ""}
    snapshot = {**product, "id": "product-old", "reviews": [], "review_count": 0, "example": None}
    stamp = "2026-11-14T10:00:00+00:00"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE products (id TEXT PRIMARY KEY, sku TEXT, data TEXT, sample TEXT,
                updated_at TEXT);
            CREATE TABLE reviews (product_id TEXT REFERENCES products(id), id TEXT, data TEXT);
            CREATE TABLE tasks (id TEXT PRIMARY KEY, request_id TEXT, retry_of TEXT, status TEXT,
                input TEXT, snapshot TEXT, template_version INTEGER, insights TEXT,
                mode TEXT, error TEXT, created_at TEXT);
            CREATE TABLE events (seq INTEGER PRIMARY KEY, task_id TEXT REFERENCES tasks(id),
                data TEXT);
            CREATE TABLE versions (task_id TEXT REFERENCES tasks(id), version INTEGER,
                draft TEXT, checks TEXT, approved_at TEXT, created_at TEXT);
            CREATE TABLE metadata (key TEXT PRIMARY KEY);
            INSERT INTO metadata VALUES ('seed-v1');
        """)
        db.execute(
            "INSERT INTO products VALUES (?,?,?,?,?)",
            ("product-old", "OLD-1", json.dumps(product), None, stamp),
        )
        for task_id, state in (("finished", "completed"), ("interrupted", "running")):
            db.execute(
                "INSERT INTO tasks VALUES (?,?,NULL,?,?,?,1,NULL,'sample',NULL,?)",
                (
                    task_id,
                    task_id + "-request",
                    state,
                    json.dumps(inputs),
                    json.dumps(snapshot),
                    stamp,
                ),
            )
        db.execute(
            "INSERT INTO versions VALUES ('finished',1,?,'[]',?,?)",
            (json.dumps({"title": "Original listing"}), stamp, stamp),
        )
        db.execute(
            "INSERT INTO events VALUES (1,'finished',?)", (json.dumps({"status": "completed"}),)
        )
    store = Store(path)
    store.initialize()
    assert [item["sku"] for item in store.products()] == ["OLD-1"]
    assert store.product("product-old")["revision"] == 1
    assert store.export("finished", 1)[1]["draft"]["title"] == "Original listing"
    assert store.task("finished")["events"][0]["seq"] == 1
    assert store.claim()["id"] == "interrupted"
    store.initialize()
    assert store.task("interrupted")["status"] == "running"
    store.close()
