import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from studio.schemas import TEMPLATE_VERSION, Product, Review, TaskInput


class Missing(Exception):
    pass


class Conflict(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self, write=False):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS products (
                    id TEXT PRIMARY KEY, sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    data TEXT NOT NULL, sample TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    product_id TEXT REFERENCES products(id), id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (product_id, id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, retry_of TEXT,
                    status TEXT NOT NULL, input TEXT NOT NULL, snapshot TEXT NOT NULL,
                    template_version INTEGER NOT NULL, insights TEXT, mode TEXT,
                    error TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_events ON events(task_id, seq);
                CREATE TABLE IF NOT EXISTS versions (
                    task_id TEXT REFERENCES tasks(id), version INTEGER, draft TEXT NOT NULL,
                    checks TEXT NOT NULL, approved_at TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, version)
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY);
            """)
        with self.connect(write=True) as db:
            if db.execute("INSERT OR IGNORE INTO metadata VALUES ('seed-v1')").rowcount:
                samples = json.loads(Path(__file__).with_name("seed.json").read_text("utf-8"))
                for index, sample in enumerate(samples, 1):
                    product_id = f"00000000-0000-4000-8000-{index:012d}"
                    reviews, examples = sample.pop("reviews"), sample.pop("examples")
                    product = Product.model_validate(sample)
                    db.execute(
                        "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
                        (product_id, product.sku, product.model_dump_json(), dump(examples), now()),
                    )
                    self._save_reviews(db, product_id, [Review.model_validate(r) for r in reviews])
            for row in db.execute("SELECT id FROM tasks WHERE status='running'").fetchall():
                self._fail(db, row["id"], "上次运行因服务重启中断，可重新运行。")

    def products(self):
        with self.connect() as db:
            return [
                {"id": row["id"], **json.loads(row["data"])}
                for row in db.execute("SELECT id,data FROM products ORDER BY sku")
            ]

    def product(self, product_id):
        with self.connect() as db:
            return self._product(db, product_id)

    def _product(self, db, product_id):
        row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            raise Missing("找不到这件商品。")
        return {
            "id": product_id,
            **json.loads(row["data"]),
            "updated_at": row["updated_at"],
            "reviews": [
                json.loads(r["data"])
                for r in db.execute(
                    "SELECT data FROM reviews WHERE product_id=? ORDER BY id", (product_id,)
                )
            ],
        }

    def save_product(self, product, product_id=None):
        try:
            with self.connect(write=True) as db:
                if product_id is None:
                    product_id = str(uuid4())
                    db.execute(
                        "INSERT INTO products VALUES (?, ?, ?, NULL, ?)",
                        (product_id, product.sku, product.model_dump_json(), now()),
                    )
                else:
                    row = db.execute(
                        "SELECT data,sample FROM products WHERE id=?", (product_id,)
                    ).fetchone()
                    if row is None:
                        raise Missing("找不到这件商品。")
                    unchanged = Product.model_validate_json(row["data"]) == product
                    db.execute(
                        "UPDATE products SET sku=?,data=?,sample=?,updated_at=? WHERE id=?",
                        (
                            product.sku,
                            product.model_dump_json(),
                            row["sample"] if unchanged else None,
                            now(),
                            product_id,
                        ),
                    )
                return self._product(db, product_id)
        except sqlite3.IntegrityError as exc:
            raise Conflict("这个 SKU 已被另一件商品使用。") from exc

    def import_reviews(self, product_id, reviews):
        with self.connect(write=True) as db:
            product = self._product(db, product_id)
            ids = {item["id"] for item in product["reviews"]} | {r.id for r in reviews}
            if len(ids) > 500:
                raise Conflict("每件商品最多保存 500 条评论。")
            self._save_reviews(db, product_id, reviews)
            return len(ids)

    @staticmethod
    def _save_reviews(db, product_id, reviews):
        db.executemany(
            """INSERT INTO reviews VALUES (?, ?, ?) ON CONFLICT(product_id,id)
            DO UPDATE SET data=excluded.data""",
            [(product_id, item.id, item.model_dump_json()) for item in reviews],
        )

    def submit(self, inputs, retry_of=None):
        data = inputs.model_dump(mode="json", exclude={"request_id"})
        request_id = str(inputs.request_id)
        with self.connect(write=True) as db:
            return self._submit(db, data, request_id, retry_of)

    def _submit(self, db, data, request_id, retry_of, snapshot=None):
        prior = db.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        if prior is not None:
            if json.loads(prior["input"]) != data or prior["retry_of"] != retry_of:
                raise Conflict("这次提交编号已用于另一项任务。")
            return prior["id"]
        if snapshot is None:
            snapshot = self._product(db, data["product_id"])
            sample = db.execute(
                "SELECT sample FROM products WHERE id=?", (data["product_id"],)
            ).fetchone()["sample"]
            snapshot["example"] = json.loads(sample).get(data["language"]) if sample else None
            snapshot["review_count"] = len(snapshot["reviews"])
            snapshot["reviews"] = snapshot["reviews"][:40]
        task_id = str(uuid4())
        db.execute(
            """INSERT INTO tasks
            (id,request_id,retry_of,status,input,snapshot,template_version,created_at)
            VALUES (?,?,?,'queued',?,?,?,?)""",
            (task_id, request_id, retry_of, dump(data), dump(snapshot), TEMPLATE_VERSION, now()),
        )
        self._event(db, task_id, "queued", "任务已排队。")
        return task_id

    def retry(self, task_id, request_id):
        with self.connect(write=True) as db:
            row = self._task(db, task_id)
            if row["status"] != "failed":
                raise Conflict("只有失败的任务可以重试。")
            inputs = TaskInput.model_validate(
                {**json.loads(row["input"]), "request_id": request_id}
            )
            data = inputs.model_dump(mode="json", exclude={"request_id"})
            return self._submit(db, data, request_id, task_id, json.loads(row["snapshot"]))

    def claim(self):
        with self.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running' WHERE id=?", (row["id"],))
            self._event(db, row["id"], "running", "开始执行上新流程。")
            return {"id": row["id"], "input": json.loads(row["input"])}

    def snapshot(self, task_id):
        with self.connect() as db:
            return json.loads(self._task(db, task_id)["snapshot"])

    def step(self, task_id, step, status, message):
        with self.connect(write=True) as db:
            self._event(db, task_id, status, message, step)

    def complete(self, task_id, insights, draft, checks, mode):
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO versions VALUES (?,1,?,?,NULL,?)",
                (task_id, draft.model_dump_json(), dump(checks), now()),
            )
            db.execute(
                "UPDATE tasks SET status='completed',insights=?,mode=? WHERE id=?",
                (dump(insights), mode, task_id),
            )
            self._event(db, task_id, "completed", "草稿已保存，可以编辑和确认。")

    def fail(self, task_id, message):
        with self.connect(write=True) as db:
            self._fail(db, task_id, message)

    def _fail(self, db, task_id, message):
        db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?", (message, task_id))
        self._event(db, task_id, "failed", message)

    @staticmethod
    def _event(db, task_id, status, message, step=None):
        event = {"status": status, "step": step, "message": message, "at": now()}
        db.execute("INSERT INTO events (task_id,data) VALUES (?,?)", (task_id, dump(event)))

    def tasks(self):
        with self.connect() as db:
            return [
                self._summary(row)
                for row in db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")
            ]

    @staticmethod
    def _task(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise Missing("找不到这个任务。")
        return row

    @staticmethod
    def _decode_task(row):
        task = dict(row)
        task.pop("request_id")
        for key in ("input", "snapshot", "insights"):
            if task[key] is not None:
                task[key] = json.loads(task[key])
        return task

    def _summary(self, row):
        item = self._decode_task(row)
        snapshot = item.pop("snapshot")
        item.pop("insights")
        return {**item, "product_name": snapshot["name"], "sku": snapshot["sku"]}

    def task(self, task_id):
        with self.connect() as db:
            task = self._decode_task(self._task(db, task_id))
            task["snapshot"].pop("example", None)
            task["events"] = self._events(db, task_id, 0)
            task["versions"] = [
                self._decode_version(r)
                for r in db.execute(
                    "SELECT * FROM versions WHERE task_id=? ORDER BY version DESC", (task_id,)
                )
            ]
            return task

    def updates(self, task_id, since):
        with self.connect() as db:
            return self._task(db, task_id)["status"], self._events(db, task_id, since)

    @staticmethod
    def _events(db, task_id, since):
        return [
            {"seq": row["seq"], **json.loads(row["data"])}
            for row in db.execute(
                "SELECT seq,data FROM events WHERE task_id=? AND seq>? ORDER BY seq",
                (task_id, since),
            )
        ]

    @staticmethod
    def _decode_version(row):
        return {**dict(row), "draft": json.loads(row["draft"]), "checks": json.loads(row["checks"])}

    def save_version(self, task_id, base, draft, checks):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id)
            if current["version"] != base:
                raise Conflict("草稿已在其他窗口更新，请重新打开任务。")
            version = base + 1
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,NULL,?)",
                (task_id, version, draft.model_dump_json(), dump(checks), now()),
            )
            return version

    def approve(self, task_id, version):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id)
            if current["version"] != version:
                raise Conflict("请确认最新版本。")
            if any(item["level"] == "error" for item in json.loads(current["checks"])):
                raise Conflict("请补全必填文案后再确认。")
            db.execute(
                "UPDATE versions SET approved_at=COALESCE(approved_at,?)"
                " WHERE task_id=? AND version=?",
                (now(), task_id, version),
            )
            return self._decode_version(self._latest(db, task_id))

    def export(self, task_id, version):
        with self.connect() as db:
            current = self._latest(db, task_id)
            if current["version"] != version or not current["approved_at"]:
                raise Conflict("请先确认当前版本再导出。")
            task = self._decode_task(self._task(db, task_id))
            return task, self._decode_version(current)

    def _latest(self, db, task_id):
        if self._task(db, task_id)["status"] != "completed":
            raise Conflict("任务完成后才会有草稿。")
        return db.execute(
            "SELECT * FROM versions WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)
        ).fetchone()
