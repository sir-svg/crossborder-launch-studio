import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from studio.model import Model, Settings
from studio.schemas import (
    MARKETS,
    STEPS,
    TEMPLATE,
    TEMPLATE_VERSION,
    Approve,
    Edit,
    Product,
    Retry,
    Review,
    ReviewImport,
    TaskInput,
)
from studio.store import Conflict, Missing, Store
from studio.workflow import Workflow, check_draft

STATIC = Path(__file__).with_name("static")
TERMINAL = {"completed", "failed"}


def parse_reviews(text):
    reader = csv.DictReader(io.StringIO(text.removeprefix("\ufeff")), strict=True)
    reviews = []
    try:
        headers = reader.fieldnames or []
        if len(headers) != 4 or set(headers) != {"id", "rating", "text", "source"}:
            raise ValueError("CSV 表头需要 id、rating、text、source 四列。")
        for line, row in enumerate(reader, 2):
            if line > 201:
                raise ValueError("每次最多导入 200 条评论。")
            try:
                reviews.append(Review.model_validate(row))
            except ValueError:
                raise ValueError(f"第 {line} 行的评论编号、评分、正文或来源无效。") from None
    except csv.Error:
        raise ValueError("CSV 格式有误，请检查引号和分隔符。") from None
    if not reviews or len({r.id for r in reviews}) != len(reviews):
        raise ValueError("CSV 需要至少一条评论，同一文件不能重复使用编号。")
    return reviews


def csv_cell(value):
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def render_export(task, version, kind):
    draft = version["draft"]
    inputs = task["input"]
    snapshot = task["snapshot"]
    if kind == "csv":
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(
            [
                "SKU",
                "Market",
                "Language",
                "Version",
                "Title",
                *[f"Bullet {i}" for i in range(1, 6)],
                "Description",
                "Search terms",
            ]
        )
        writer.writerow(
            map(
                csv_cell,
                [
                    snapshot["sku"],
                    inputs["market"],
                    inputs["language"],
                    version["version"],
                    draft["title"],
                    *draft["bullets"],
                    draft["description"],
                    draft["search_terms"],
                ],
            )
        )
        return out.getvalue().encode("utf-8-sig"), "text/csv"
    bullets = "\n".join(f"- {item}" for item in draft["bullets"])
    body = (
        f"# {draft['title']}\n\n"
        f"{snapshot['sku']} · {inputs['market']} · {inputs['language']} · v{version['version']}\n\n"
        f"## 五点描述\n\n{bullets}\n\n## 商品描述\n\n{draft['description']}\n\n"
        f"## 搜索词\n\n{draft['search_terms']}\n"
    )
    return body.encode("utf-8"), "text/markdown"


def create_app(settings=None, model=None):
    settings = settings or Settings()
    model = model or Model(settings)
    store = Store(settings.database_path)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        worker = asyncio.create_task(Workflow(store, model).worker())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(title="跨境商品上新工作台", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(Missing)
    async def missing_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config():
        return {
            "markets": MARKETS,
            "template": {"id": TEMPLATE, "version": TEMPLATE_VERSION, "steps": STEPS},
            "model": settings.llm_model if model.enabled else None,
        }

    @app.get("/api/products")
    def products():
        return store.products()

    @app.post("/api/products", status_code=201)
    def create_product(body: Product):
        return store.save_product(body)

    @app.get("/api/products/{product_id}")
    def product(product_id: UUID):
        return store.product(str(product_id))

    @app.put("/api/products/{product_id}")
    def update_product(product_id: UUID, body: Product):
        return store.save_product(body, str(product_id))

    @app.post("/api/products/{product_id}/reviews")
    def import_reviews(product_id: UUID, body: ReviewImport):
        try:
            reviews = parse_reviews(body.csv_text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"count": store.import_reviews(str(product_id), reviews), "imported": len(reviews)}

    @app.get("/api/tasks")
    def tasks():
        return store.tasks()

    @app.post("/api/tasks", status_code=202)
    def submit(body: TaskInput):
        if MARKETS[body.market]["language"] != body.language:
            raise HTTPException(422, "请选择该站点对应的文案语言。")
        return {"id": store.submit(body)}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: UUID):
        return store.task(str(task_id))

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: UUID, body: Retry):
        return {"id": store.retry(str(task_id), str(body.request_id))}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: UUID, request: Request, since: int = Query(0, ge=0)):
        task_id = str(task_id)
        store.updates(task_id, since)
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isascii() and last_id.isdigit() and len(last_id) < 18:
            since = max(since, int(last_id))

        async def stream():
            cursor = since
            while not await request.is_disconnected():
                status, updates = store.updates(task_id, cursor)
                for event in updates:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if status in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.4)
                yield ": waiting\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.put("/api/tasks/{task_id}/draft")
    def edit(task_id: UUID, body: Edit):
        task_id = str(task_id)
        snapshot = store.task(task_id)["snapshot"]
        checks = check_draft(body.draft, snapshot)
        return {"version": store.save_version(task_id, body.base_version, body.draft, checks)}

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: UUID, body: Approve):
        return store.approve(str(task_id), body.version)

    @app.get("/api/tasks/{task_id}/export/{kind}")
    def export(task_id: UUID, kind: Literal["csv", "md"], version: int = Query(ge=1)):
        task, draft = store.export(str(task_id), version)
        data, media = render_export(task, draft, kind)
        name = f"{task['snapshot']['sku']}-{task['input']['market']}-v{version}.{kind}"
        return Response(
            data,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app


app = create_app()
