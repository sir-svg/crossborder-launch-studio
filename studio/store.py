import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from studio.database import Database
from studio.migrations import DEMO_WORKSPACE, migrate
from studio.schemas import TEMPLATE_VERSION, Product, Review


class Missing(Exception):
    pass


class Conflict(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def locked(db, query, skip=False):
    if db.postgres:
        return query + (" FOR UPDATE SKIP LOCKED" if skip else " FOR UPDATE")
    return query


class Store:
    def __init__(self, path, url=""):
        self.database = Database(path, url)

    def connect(self, write=False):
        return self.database.connect(write)

    def initialize(self, seed=True):
        self.database.open()
        with self.connect(write=True) as db:
            migrate(db)
            if (
                seed
                and db.execute(
                    "INSERT INTO metadata VALUES ('seed-v1') ON CONFLICT DO NOTHING"
                ).rowcount
            ):
                items = json.loads(Path(__file__).with_name("seed.json").read_text("utf-8"))
                for index, item in enumerate(items, 1):
                    pid = f"00000000-0000-4000-8000-{index:012d}"
                    reviews, examples = item.pop("reviews"), item.pop("examples")
                    self._insert_product(
                        db, DEMO_WORKSPACE, pid, Product.model_validate(item), None, dump(examples)
                    )
                    self._save_reviews(db, pid, [Review.model_validate(r) for r in reviews])

    def close(self):
        self.database.close()

    @staticmethod
    def audit(db, workspace_id, actor_id, action, resource_id, details):
        db.execute(
            """INSERT INTO audit_log
            (workspace_id,actor_id,action,resource_id,details,created_at) VALUES (?,?,?,?,?,?)""",
            (workspace_id, actor_id, action, resource_id, dump(details), now()),
        )

    def audit_log(self, workspace_id, offset, limit):
        with self.connect() as db:
            rows = db.execute(
                """SELECT a.id,a.actor_id,u.display_name AS actor_name,a.action,a.resource_id,
                a.details,a.created_at FROM audit_log a LEFT JOIN users u ON a.actor_id=u.id
                WHERE a.workspace_id=? ORDER BY a.id DESC LIMIT ? OFFSET ?""",
                (workspace_id, limit, offset),
            )
            return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def products(self, workspace_id=DEMO_WORKSPACE, offset=0, limit=100):
        with self.connect() as db:
            return [
                {"id": row["id"], "revision": row["revision"], **json.loads(row["data"])}
                for row in db.execute(
                    """SELECT id,revision,data FROM products WHERE workspace_id=?
                    ORDER BY sku,id LIMIT ? OFFSET ?""",
                    (workspace_id, limit, offset),
                )
            ]

    @staticmethod
    def _product_row(db, workspace_id, product_id, lock=False):
        query = "SELECT * FROM products WHERE id=? AND workspace_id=?"
        row = db.execute(
            locked(db, query) if lock else query, (product_id, workspace_id)
        ).fetchone()
        if row is None:
            raise Missing("找不到这件商品。")
        return row

    @staticmethod
    def _product(db, row):
        return {
            "id": row["id"],
            **json.loads(row["data"]),
            "updated_at": row["updated_at"],
            "revision": row["revision"],
            "reviews": [
                json.loads(r["data"])
                for r in db.execute(
                    "SELECT data FROM reviews WHERE product_id=? ORDER BY id", (row["id"],)
                )
            ],
        }

    def product(self, product_id, workspace_id=DEMO_WORKSPACE):
        with self.connect() as db:
            return self._product(db, self._product_row(db, workspace_id, product_id))

    @staticmethod
    def _insert_product(db, workspace_id, product_id, product, actor_id, sample=None):
        data = product.model_dump_json()
        stamp = now()
        db.execute(
            """INSERT INTO products (id,workspace_id,sku,data,sample,updated_at)
            VALUES (?,?,?,?,?,?)""",
            (product_id, workspace_id, product.sku, data, sample, stamp),
        )
        db.execute(
            "INSERT INTO product_revisions VALUES (?,1,?,?,?)",
            (product_id, data, actor_id, stamp),
        )

    def save_product(
        self,
        product,
        product_id=None,
        *,
        workspace_id=DEMO_WORKSPACE,
        actor_id=None,
        base_revision=None,
    ):
        try:
            with self.connect(write=True) as db:
                if product_id is None:
                    product_id = str(uuid4())
                    self._insert_product(db, workspace_id, product_id, product, actor_id)
                    action, revision = "product.created", 1
                else:
                    row = self._product_row(db, workspace_id, product_id, lock=True)
                    if base_revision is not None and row["revision"] != base_revision:
                        raise Conflict("这件商品已被更新，请重新打开后再编辑。")
                    if Product.model_validate_json(row["data"]) == product:
                        return self._product(db, row)
                    data, stamp = product.model_dump_json(), now()
                    revision = row["revision"] + 1
                    db.execute(
                        """UPDATE products SET sku=?,data=?,sample=NULL,updated_at=?,revision=?
                        WHERE id=?""",
                        (product.sku, data, stamp, revision, product_id),
                    )
                    db.execute(
                        "INSERT INTO product_revisions VALUES (?,?,?,?,?)",
                        (product_id, revision, data, actor_id, stamp),
                    )
                    action = "product.updated"
                self.audit(db, workspace_id, actor_id, action, product_id, {"revision": revision})
                return self._product(db, self._product_row(db, workspace_id, product_id))
        except self.database.integrity_errors as exc:
            raise Conflict("这个 SKU 已被另一件商品使用。") from exc

    def revisions(self, product_id, workspace_id, offset, limit):
        with self.connect() as db:
            self._product_row(db, workspace_id, product_id)
            rows = db.execute(
                """SELECT revision,data,actor_id,created_at FROM product_revisions
                WHERE product_id=? ORDER BY revision DESC LIMIT ? OFFSET ?""",
                (product_id, limit, offset),
            )
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def import_reviews(self, product_id, reviews, workspace_id=DEMO_WORKSPACE, actor_id=None):
        with self.connect(write=True) as db:
            row = self._product_row(db, workspace_id, product_id, lock=True)
            product = self._product(db, row)
            ids = {item["id"] for item in product["reviews"]} | {r.id for r in reviews}
            if len(ids) > 500:
                raise Conflict("每件商品最多保存 500 条评论。")
            self._save_reviews(db, product_id, reviews)
            self.audit(
                db,
                workspace_id,
                actor_id,
                "reviews.imported",
                product_id,
                {"imported": len(reviews), "total": len(ids)},
            )
            return len(ids)

    @staticmethod
    def _save_reviews(db, product_id, reviews):
        db.executemany(
            """INSERT INTO reviews VALUES (?, ?, ?) ON CONFLICT(product_id,id)
            DO UPDATE SET data=excluded.data""",
            [(product_id, item.id, item.model_dump_json()) for item in reviews],
        )

    @staticmethod
    def _workspace_lock(db, workspace_id):
        if db.postgres:
            db.execute("SELECT pg_advisory_xact_lock(4302, hashtext(?))", (workspace_id,))

    def submit(self, inputs, workspace_id=DEMO_WORKSPACE, actor_id=None):
        data = inputs.model_dump(mode="json", exclude={"request_id"})
        with self.connect(write=True) as db:
            self._workspace_lock(db, workspace_id)
            return self._submit(db, data, str(inputs.request_id), workspace_id, actor_id)

    def _submit(self, db, data, request_id, space, actor, retry_of=None, snapshot=None):
        prior = db.execute(
            "SELECT * FROM tasks WHERE workspace_id=? AND request_id=?",
            (space, request_id),
        ).fetchone()
        if prior is not None:
            if (
                json.loads(prior["input"]) != data
                or prior["retry_of"] != retry_of
                or prior["submitted_by"] != actor
            ):
                raise Conflict("这次提交编号已用于另一项任务。")
            return prior["id"]
        load = db.execute(
            """SELECT count(*) AS total FROM tasks WHERE workspace_id=?
            AND status IN ('queued','running')""",
            (space,),
        ).fetchone()["total"]
        if load >= 50:
            raise Conflict("当前工作区有 50 项待处理任务，请等部分任务完成。")
        if snapshot is None:
            row = self._product_row(db, space, data["product_id"], lock=True)
            snapshot = self._product(db, row)
            snapshot["example"] = (
                json.loads(row["sample"]).get(data["language"]) if row["sample"] else None
            )
            snapshot["review_count"] = len(snapshot["reviews"])
            snapshot["reviews"] = snapshot["reviews"][:40]
        task_id, stamp = str(uuid4()), now()
        db.execute(
            """INSERT INTO tasks
            (id,workspace_id,request_id,submitted_by,retry_of,status,input,snapshot,
             template_version,created_at,updated_at)
            VALUES (?,?,?,?,?,'queued',?,?,?,?,?)""",
            (
                task_id,
                space,
                request_id,
                actor,
                retry_of,
                dump(data),
                dump(snapshot),
                TEMPLATE_VERSION,
                stamp,
                stamp,
            ),
        )
        self._event(db, task_id, "queued", "任务已排队。")
        action = "task.retried" if retry_of else "task.submitted"
        self.audit(
            db, space, actor, action, task_id, {"retry_of": retry_of, "sku": snapshot["sku"]}
        )
        return task_id

    def retry(self, task_id, request_id, workspace_id=DEMO_WORKSPACE, actor_id=None):
        with self.connect(write=True) as db:
            self._workspace_lock(db, workspace_id)
            row = self._task(db, task_id, workspace_id)
            if row["status"] != "failed":
                raise Conflict("只有失败的任务可以重试。")
            return self._submit(
                db,
                json.loads(row["input"]),
                request_id,
                workspace_id,
                actor_id,
                task_id,
                json.loads(row["snapshot"]),
            )

    def worker_seen(self, worker_id):
        with self.connect(write=True) as db:
            db.execute(
                """INSERT INTO workers VALUES (?,?) ON CONFLICT(id) DO UPDATE
                SET seen=excluded.seen""",
                (worker_id, time.time()),
            )
            db.execute("DELETE FROM workers WHERE seen<?", (time.time() - 3600,))

    def claim(self, lease_seconds=45, worker_id=None):
        with self.connect(write=True) as db:
            row = db.execute(
                locked(
                    db,
                    """SELECT * FROM tasks
                WHERE status='queued' OR (status='running' AND lease_until<=?)
                ORDER BY created_at,id LIMIT 1""",
                    skip=True,
                ),
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            if row["attempt"] >= 3:
                self._fail(db, row, "任务在多次中断后停止，请检查服务再重试。")
                return None
            token = str(uuid4())
            attempt = row["attempt"] + 1
            db.execute(
                """UPDATE tasks SET status='running',attempt=?,lease_token=?,lease_until=?,
                worker_id=?,updated_at=? WHERE id=?""",
                (attempt, token, time.time() + lease_seconds, worker_id, now(), row["id"]),
            )
            message = "开始执行上新流程。" if attempt == 1 else "上次执行中断，已恢复任务。"
            self._event(db, row["id"], "running", message, attempt=attempt)
            return {
                "id": row["id"],
                "input": json.loads(row["input"]),
                "lease_token": token,
                "attempt": attempt,
            }

    def heartbeat(self, task_id, lease_token, lease_seconds=45):
        with self.connect(write=True) as db:
            stamp = time.time()
            return bool(
                db.execute(
                    """UPDATE tasks SET lease_until=? WHERE id=? AND status='running'
                AND lease_token=? AND lease_until>?""",
                    (stamp + lease_seconds, task_id, lease_token, stamp),
                ).rowcount
            )

    def snapshot(self, task_id):
        with self.connect() as db:
            return json.loads(self._task(db, task_id)["snapshot"])

    @staticmethod
    def _leased(db, task_id, token):
        return db.execute(
            locked(
                db,
                """SELECT * FROM tasks WHERE id=? AND status='running'
            AND lease_token=? AND lease_until>?""",
            ),
            (task_id, token, time.time()),
        ).fetchone()

    def step(self, task_id, step, status, message, *, lease_token):
        with self.connect(write=True) as db:
            row = self._leased(db, task_id, lease_token)
            if row is None:
                return False
            self._event(db, task_id, status, message, step, row["attempt"])
            return True

    def complete(self, task_id, insights, draft, checks, mode, *, lease_token, run_metadata=None):
        with self.connect(write=True) as db:
            row = self._leased(db, task_id, lease_token)
            if row is None:
                return False
            db.execute(
                """INSERT INTO versions (task_id,version,draft,checks,created_at,author_id)
                VALUES (?,1,?,?,?,?)""",
                (task_id, draft.model_dump_json(), dump(checks), now(), row["submitted_by"]),
            )
            db.execute(
                """UPDATE tasks SET status='completed',insights=?,mode=?,run_metadata=?,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                (dump(insights), mode, dump(run_metadata or {}), now(), task_id),
            )
            self._event(db, task_id, "completed", "草稿已保存，可以编辑。", attempt=row["attempt"])
            self.audit(db, row["workspace_id"], None, "task.completed", task_id, {"mode": mode})
            return True

    def fail(self, task_id, message, *, lease_token, run_metadata=None):
        with self.connect(write=True) as db:
            row = self._leased(db, task_id, lease_token)
            if row is None:
                return False
            self._fail(db, row, message, run_metadata)
            return True

    def _fail(self, db, row, message, run_metadata=None):
        db.execute(
            """UPDATE tasks SET status='failed',error=?,lease_token=NULL,lease_until=NULL,
            run_metadata=?,updated_at=? WHERE id=?""",
            (message, dump(run_metadata or {}), now(), row["id"]),
        )
        self._event(db, row["id"], "failed", message, attempt=row["attempt"])
        self.audit(
            db, row["workspace_id"], None, "task.failed", row["id"], {"attempt": row["attempt"]}
        )

    @staticmethod
    def _event(db, task_id, status, message, step=None, attempt=None):
        event = {
            "status": status,
            "step": step,
            "message": message,
            "at": now(),
            "attempt": attempt,
        }
        db.execute("INSERT INTO events (task_id,data) VALUES (?,?)", (task_id, dump(event)))

    def tasks(
        self, workspace_id=DEMO_WORKSPACE, status=None, review_status=None, offset=0, limit=100
    ):
        name, sku = (
            ("CAST(t.snapshot AS JSON)->>'name'", "CAST(t.snapshot AS JSON)->>'sku'")
            if self.database.postgres
            else ("json_extract(t.snapshot,'$.name')", "json_extract(t.snapshot,'$.sku')")
        )
        query = f"""SELECT t.id,t.retry_of,t.status,t.input,t.created_at,t.updated_at,
            t.attempt,t.mode,v.review_status,{name} AS product_name,{sku} AS sku
            FROM tasks t LEFT JOIN versions v ON v.task_id=t.id
            AND v.version=(SELECT max(x.version) FROM versions x WHERE x.task_id=t.id)
            WHERE t.workspace_id=?"""
        args = [workspace_id]
        if status:
            query += " AND t.status=?"
            args.append(status)
        if review_status:
            query += " AND v.review_status=?"
            args.append(review_status)
        with self.connect() as db:
            return [
                {**dict(row), "input": json.loads(row["input"])}
                for row in db.execute(
                    query + " ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]

    @staticmethod
    def _task(db, task_id, workspace_id=None, lock=False):
        query = "SELECT * FROM tasks WHERE id=?"
        args = (task_id,)
        if workspace_id is not None:
            query += " AND workspace_id=?"
            args += (workspace_id,)
        row = db.execute(locked(db, query) if lock else query, args).fetchone()
        if row is None:
            raise Missing("找不到这个任务。")
        return row

    @staticmethod
    def _decode_task(row):
        internal = {"request_id", "workspace_id", "lease_token", "lease_until", "worker_id"}
        task = {key: value for key, value in dict(row).items() if key not in internal}
        for key in ("input", "snapshot", "insights", "run_metadata"):
            if task[key] is not None:
                task[key] = json.loads(task[key])
        task["snapshot"].pop("example", None)
        return task

    def task(self, task_id, workspace_id=DEMO_WORKSPACE):
        with self.connect() as db:
            task = self._decode_task(self._task(db, task_id, workspace_id))
            task["events"] = self._events(db, task_id, 0)
            task["versions"] = [
                self._decode_version(row)
                for row in db.execute(
                    "SELECT * FROM versions WHERE task_id=? ORDER BY version DESC", (task_id,)
                )
            ]
            return task

    def updates(self, task_id, since, workspace_id=DEMO_WORKSPACE):
        with self.connect() as db:
            return self._task(db, task_id, workspace_id)["status"], self._events(db, task_id, since)

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

    def save_version(
        self, task_id, base, draft, checks, workspace_id=DEMO_WORKSPACE, actor_id=None
    ):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id, workspace_id, lock=True)
            if current["version"] != base:
                raise Conflict("草稿已在其他窗口更新，请重新打开任务。")
            version = base + 1
            db.execute(
                """INSERT INTO versions (task_id,version,draft,checks,created_at,author_id)
                VALUES (?,?,?,?,?,?)""",
                (task_id, version, draft.model_dump_json(), dump(checks), now(), actor_id),
            )
            self.audit(db, workspace_id, actor_id, "draft.updated", task_id, {"version": version})
            return version

    @staticmethod
    def _check_version(row, version, validate=True):
        if row["version"] != version:
            raise Conflict("请重新打开任务并操作最新版本。")
        if validate and any(item["level"] == "error" for item in json.loads(row["checks"])):
            raise Conflict("请完成必填文案及错误修正后再继续。")

    def submit_review(self, task_id, version, workspace_id, actor_id, note):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id, workspace_id, lock=True)
            self._check_version(current, version)
            if current["review_status"] == "pending" and current["submitted_by"] == actor_id:
                return self._decode_version(current)
            if current["review_status"] != "draft":
                raise Conflict("请提交当前待审核的草稿版本，退回后需保存新版本。")
            db.execute(
                """UPDATE versions SET review_status='pending',submitted_at=?,submitted_by=?,
                submit_note=? WHERE task_id=? AND version=?""",
                (now(), actor_id, note, task_id, version),
            )
            self.audit(
                db, workspace_id, actor_id, "review.submitted", task_id, {"version": version}
            )
            return self._decode_version(self._latest(db, task_id, workspace_id))

    def review(self, task_id, version, workspace_id, actor_id, decision, note):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id, workspace_id, lock=True)
            self._check_version(current, version, validate=decision == "approved")
            if actor_id in (current["author_id"], current["submitted_by"]):
                raise Conflict("请其他审核人处理你撰写或提交的版本。")
            if current["review_status"] != "pending":
                raise Conflict("此版本已处理或还未提交审核。")
            stamp = now()
            db.execute(
                """UPDATE versions SET review_status=?,decided_at=?,decided_by=?,
                review_note=?,approved_at=? WHERE task_id=? AND version=?""",
                (
                    decision,
                    stamp,
                    actor_id,
                    note,
                    stamp if decision == "approved" else None,
                    task_id,
                    version,
                ),
            )
            self.audit(
                db, workspace_id, actor_id, "review." + decision, task_id, {"version": version}
            )
            return self._decode_version(self._latest(db, task_id, workspace_id))

    def approve(self, task_id, version):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id, DEMO_WORKSPACE, lock=True)
            self._check_version(current, version)
            db.execute(
                "UPDATE versions SET approved_at=COALESCE(approved_at,?),review_status='approved'"
                " WHERE task_id=? AND version=?",
                (now(), task_id, version),
            )
            return self._decode_version(self._latest(db, task_id, DEMO_WORKSPACE))

    def export(self, task_id, version, workspace_id=DEMO_WORKSPACE, actor_id=None, kind=None):
        with self.connect(write=True) as db:
            current = self._latest(db, task_id, workspace_id, lock=True)
            if current["version"] != version or current["review_status"] != "approved":
                raise Conflict("请先确认当前版本再导出。")
            self.audit(
                db,
                workspace_id,
                actor_id,
                "draft.exported",
                task_id,
                {"version": version, "format": kind},
            )
            task = self._decode_task(self._task(db, task_id, workspace_id))
            return task, self._decode_version(current)

    def _latest(self, db, task_id, workspace_id, lock=False):
        if self._task(db, task_id, workspace_id, lock)["status"] != "completed":
            raise Conflict("任务完成后才会有草稿。")
        return db.execute(
            "SELECT * FROM versions WHERE task_id=? ORDER BY version DESC LIMIT 1", (task_id,)
        ).fetchone()

    def operations(self, workspace_id):
        with self.connect() as db:
            tasks = {key: 0 for key in ("queued", "running", "failed", "completed")}
            rows = db.execute(
                "SELECT status,count(*) AS total FROM tasks WHERE workspace_id=? GROUP BY status",
                (workspace_id,),
            )
            tasks.update({row["status"]: row["total"] for row in rows})
            reviews = {key: 0 for key in ("pending", "approved", "rejected", "draft")}
            rows = db.execute(
                """SELECT v.review_status,count(*) AS total FROM versions v JOIN tasks t
                ON t.id=v.task_id WHERE t.workspace_id=? AND v.version=(
                    SELECT max(x.version) FROM versions x WHERE x.task_id=v.task_id)
                GROUP BY v.review_status""",
                (workspace_id,),
            )
            reviews.update({row["review_status"]: row["total"] for row in rows})
            queued = db.execute(
                "SELECT min(created_at) AS oldest FROM tasks"
                " WHERE workspace_id=? AND status='queued'",
                (workspace_id,),
            ).fetchone()["oldest"]
            expired = db.execute(
                """SELECT count(*) AS total FROM tasks WHERE workspace_id=? AND status='running'
                AND lease_until<=?""",
                (workspace_id, time.time()),
            ).fetchone()["total"]
            seen = db.execute("SELECT max(seen) AS latest FROM workers").fetchone()["latest"]
        return {
            "tasks": tasks,
            "reviews": reviews,
            "worker": {
                "oldest_queued_at": queued,
                "expired_leases": expired,
                "healthy": seen is not None and seen >= time.time() - 45,
                "last_seen": datetime.fromtimestamp(seen, timezone.utc).isoformat()
                if seen is not None
                else None,
            },
        }
