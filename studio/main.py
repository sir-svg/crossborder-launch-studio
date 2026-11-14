import asyncio
import csv
import io
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from studio.identity import Identity
from studio.migrations import DEMO_WORKSPACE, SCHEMA_VERSION
from studio.model import Model, Settings
from studio.schemas import (
    MARKETS,
    STEPS,
    TEMPLATE,
    TEMPLATE_VERSION,
    Approve,
    Edit,
    Input,
    Product,
    Retry,
    Review,
    ReviewImport,
    TaskInput,
)
from studio.security import OriginGuardMiddleware
from studio.store import Conflict, Missing, Store
from studio.workflow import Workflow, check_draft

STATIC = Path(__file__).with_name("static")
TERMINAL = {"completed", "failed"}
TaskStatus = Literal["queued", "running", "failed", "completed"]
ReviewStatus = Literal["draft", "pending", "approved", "rejected"]
Offset = Annotated[int, Query(ge=0, le=100_000)]
Limit = Annotated[int, Query(ge=1, le=100)]


class ReviewRequest(Input):
    version: int = Field(ge=1)
    note: str = Field(default="", max_length=1000)


class ReviewDecision(ReviewRequest):
    decision: Literal["approved", "rejected"]


def check_settings(settings, model):
    if settings.app_environment not in {"development", "production"}:
        raise RuntimeError("APP_ENVIRONMENT 只能是 development 或 production。")
    if settings.app_environment == "production":
        if not settings.auth_enabled or not settings.database_url.get_secret_value():
            raise RuntimeError("生产模式需要开启身份验证并配置 PostgreSQL。")
        parts = urlsplit(settings.public_origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise RuntimeError("生产模式需要不带路径、查询参数的 HTTPS PUBLIC_ORIGIN。")
        hosts = {item.strip() for item in settings.allowed_hosts.split(",")}
        if parts.hostname not in hosts or "*" in hosts:
            raise RuntimeError("ALLOWED_HOSTS 必须包含网站域名，且不能使用通配主机。")
        if settings.allow_demo:
            raise RuntimeError("生产模式需要设置 ALLOW_DEMO=false。")
        model.validate_configuration()


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
    check_settings(settings, model)
    store = Store(settings.database_path, settings.database_url.get_secret_value())
    identity = Identity(store, secure_cookie=urlsplit(settings.public_origin).scheme == "https")
    demo = SimpleNamespace(workspace_id=DEMO_WORKSPACE, user_id=None, role="owner")

    def access(permission, demo_allowed=True):
        if not settings.auth_enabled and demo_allowed:
            return Depends(lambda: demo)
        return Depends(identity.require(permission))

    read = access("read")
    edit_access = access("edit")
    review_access = access("review", False)
    manage = access("manage", False)

    @asynccontextmanager
    async def lifespan(_app):
        workers = []
        try:
            await asyncio.to_thread(store.initialize, not settings.auth_enabled)
            if settings.auth_enabled:
                await asyncio.to_thread(
                    identity.initialize,
                    bootstrap_email=settings.bootstrap_email,
                    bootstrap_password=settings.bootstrap_password.get_secret_value(),
                    bootstrap_workspace=settings.bootstrap_workspace,
                )
            if settings.embedded_worker:
                workflow = Workflow(store, model)
                for _ in range(settings.worker_concurrency):
                    workers.append(asyncio.create_task(workflow.worker()))
            yield
        finally:
            for worker in workers:
                worker.cancel()
            try:
                for worker in workers:
                    with suppress(asyncio.CancelledError):
                        await worker
            finally:
                await asyncio.to_thread(store.close)

    production = settings.app_environment == "production"
    app = FastAPI(
        title="跨境商品上新工作台",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.add_middleware(
        OriginGuardMiddleware,
        allowed_origins=(settings.public_origin,) if settings.public_origin else (),
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[host.strip() for host in settings.allowed_hosts.split(",")],
    )
    if settings.auth_enabled:
        app.include_router(identity.router)
    else:

        @app.get("/api/auth/me")
        def demo_user():
            return {
                "mode": "demo",
                "authenticated": True,
                "demo": True,
                "workspace": {"id": DEMO_WORKSPACE, "name": "本地演示", "role": "owner"},
                "user": {"id": None, "display_name": "体验用户"},
            }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, exc):
        return JSONResponse(
            {
                "detail": "请检查提交内容。",
                "fields": [{"loc": issue["loc"], "type": issue["type"]} for issue in exc.errors()],
            },
            status_code=422,
        )

    @app.exception_handler(Missing)
    async def missing_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC / "index.html")

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        try:
            with store.connect() as db:
                row = db.execute("SELECT version FROM schema_version").fetchone()
                if row is None or row["version"] != SCHEMA_VERSION:
                    return JSONResponse({"status": "unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.get("/api/config")
    def config(_ctx=read):
        return {
            "markets": MARKETS,
            "template": {"id": TEMPLATE, "version": TEMPLATE_VERSION, "steps": STEPS},
            "model": settings.llm_model if model.enabled else None,
        }

    @app.get("/api/products")
    def products(offset: Offset = 0, limit: Limit = 100, ctx=read):
        return store.products(ctx.workspace_id, offset, limit)

    @app.post("/api/products", status_code=201)
    def create_product(body: Product, ctx=edit_access):
        return store.save_product(body, workspace_id=ctx.workspace_id, actor_id=ctx.user_id)

    @app.get("/api/products/{product_id}")
    def product(product_id: UUID, ctx=read):
        return store.product(str(product_id), ctx.workspace_id)

    @app.get("/api/products/{product_id}/revisions")
    def revisions(product_id: UUID, offset: Offset = 0, limit: Limit = 50, ctx=read):
        return store.revisions(str(product_id), ctx.workspace_id, offset, limit)

    @app.put("/api/products/{product_id}")
    def update_product(
        product_id: UUID,
        body: Product,
        ctx=edit_access,
        if_match: Annotated[str | None, Header()] = None,
    ):
        if settings.auth_enabled and if_match is None:
            raise HTTPException(428, "请重新打开商品后再保存。")
        revision = None
        if if_match is not None:
            value = if_match.strip().strip('"')
            if not value.isascii() or not value.isdigit() or len(value) > 9 or int(value) < 1:
                raise HTTPException(422, "商品版本编号有误。")
            revision = int(value)
        return store.save_product(
            body,
            str(product_id),
            workspace_id=ctx.workspace_id,
            actor_id=ctx.user_id,
            base_revision=revision,
        )

    @app.post("/api/products/{product_id}/reviews")
    def import_reviews(product_id: UUID, body: ReviewImport, ctx=edit_access):
        try:
            reviews = parse_reviews(body.csv_text)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        count = store.import_reviews(str(product_id), reviews, ctx.workspace_id, ctx.user_id)
        return {"count": count, "imported": len(reviews)}

    @app.get("/api/tasks")
    def tasks(
        status: TaskStatus | None = None,
        review_status: ReviewStatus | None = None,
        offset: Offset = 0,
        limit: Limit = 100,
        ctx=read,
    ):
        return store.tasks(ctx.workspace_id, status, review_status, offset, limit)

    @app.post("/api/tasks", status_code=202)
    def submit(body: TaskInput, ctx=edit_access):
        if MARKETS[body.market]["language"] != body.language:
            raise HTTPException(422, "请选择该站点对应的文案语言。")
        return {"id": store.submit(body, ctx.workspace_id, ctx.user_id)}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: UUID, ctx=read):
        return store.task(str(task_id), ctx.workspace_id)

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: UUID, body: Retry, ctx=edit_access):
        return {
            "id": store.retry(str(task_id), str(body.request_id), ctx.workspace_id, ctx.user_id)
        }

    @app.get("/api/tasks/{task_id}/events")
    async def events(
        task_id: UUID, request: Request, since: int = Query(0, ge=0, le=2**63 - 1), ctx=read
    ):
        task_id = str(task_id)
        await asyncio.to_thread(store.updates, task_id, since, ctx.workspace_id)
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isascii() and last_id.isdigit() and len(last_id) < 18:
            since = max(since, int(last_id))

        async def stream():
            cursor = since
            refresh_at = time.monotonic() + 4
            while not await request.is_disconnected():
                if settings.auth_enabled and time.monotonic() >= refresh_at:
                    try:
                        fresh = await asyncio.to_thread(identity.context, request)
                    except HTTPException:
                        return
                    if fresh.workspace_id != ctx.workspace_id:
                        return
                    refresh_at = time.monotonic() + 4
                status, updates = await asyncio.to_thread(
                    store.updates, task_id, cursor, ctx.workspace_id
                )
                for event in updates:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if status in TERMINAL:
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.6)
                yield ": waiting\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.put("/api/tasks/{task_id}/draft")
    def edit(task_id: UUID, body: Edit, ctx=edit_access):
        task_id = str(task_id)
        current = store.task(task_id, ctx.workspace_id)
        checks = check_draft(body.draft, current["snapshot"], current["input"]["market"])
        return {
            "version": store.save_version(
                task_id, body.base_version, body.draft, checks, ctx.workspace_id, ctx.user_id
            )
        }

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: UUID, body: Approve):
        if settings.auth_enabled:
            raise HTTPException(404)
        return store.approve(str(task_id), body.version)

    @app.post("/api/tasks/{task_id}/submit-review")
    def submit_review(task_id: UUID, body: ReviewRequest, ctx=edit_access):
        if not settings.auth_enabled:
            raise HTTPException(404)
        return store.submit_review(
            str(task_id), body.version, ctx.workspace_id, ctx.user_id, body.note
        )

    @app.post("/api/tasks/{task_id}/review")
    def review(task_id: UUID, body: ReviewDecision, ctx=review_access):
        if body.decision == "rejected" and not body.note:
            raise HTTPException(422, "请填写退回原因。")
        return store.review(
            str(task_id), body.version, ctx.workspace_id, ctx.user_id, body.decision, body.note
        )

    @app.get("/api/tasks/{task_id}/export/{kind}")
    def export(task_id: UUID, kind: Literal["csv", "md"], ctx=read, version: int = Query(ge=1)):
        task, draft = store.export(str(task_id), version, ctx.workspace_id, ctx.user_id, kind)
        data, media = render_export(task, draft, kind)
        name = f"{task['snapshot']['sku']}-{task['input']['market']}-v{version}.{kind}"
        return Response(
            data,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/audit")
    def audit(offset: Offset = 0, limit: Limit = 60, ctx=manage):
        return store.audit_log(ctx.workspace_id, offset, limit)

    @app.get("/api/operations")
    def operations(ctx=read):
        return {**store.operations(ctx.workspace_id), "mode": settings.app_environment}

    return app


app = create_app()
