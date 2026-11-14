"""Persist users, workspace membership, and revocable browser sessions."""

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator

from studio.security import hash_password, token_digest, verify_password

Role = Literal["owner", "editor", "reviewer", "viewer"]
Permission = Literal["read", "edit", "review", "manage"]
ROLE_PERMISSIONS = {
    "owner": ("read", "edit", "review", "manage"),
    "editor": ("read", "edit"),
    "reviewer": ("read", "review"),
    "viewer": ("read",),
}
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
NewPassword = Annotated[SecretStr, Field(min_length=12, max_length=128)]
CurrentPassword = Annotated[SecretStr, Field(min_length=1, max_length=128)]
EMAIL_LOCAL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", re.ASCII)
EMAIL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
INVALID_CREDENTIALS = "邮箱或密码不正确。"
DUMMY_HASH = (
    "pbkdf2-sha256$600000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)
LOGIN_WINDOW = 900

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workspaces (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workspace_members (
        workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        user_id TEXT NOT NULL REFERENCES users(id),
        role TEXT NOT NULL CHECK (role IN ('owner','editor','reviewer','viewer')),
        joined_at BIGINT NOT NULL,
        PRIMARY KEY (workspace_id,user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS membership_user ON workspace_members(user_id)",
    """CREATE TABLE IF NOT EXISTS sessions (
        digest TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        FOREIGN KEY (workspace_id,user_id) REFERENCES workspace_members(workspace_id,user_id)
            ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS sessions_expires ON sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)",
    """CREATE TABLE IF NOT EXISTS login_throttle (
        key TEXT PRIMARY KEY, window_start BIGINT NOT NULL, attempts INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS login_windows ON login_throttle(window_start)",
)


def normalize_email(value):
    local, separator, domain = value.lower().strip().partition("@")
    labels = domain.split(".")
    if (
        not separator
        or len(value) > 254
        or not EMAIL_LOCAL.fullmatch(local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or len(labels) < 2
        or any(not EMAIL_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("请输入有效的邮箱地址。")
    return f"{local}@{domain}"


class AuthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("password", "old_password", "new_password", check_fields=False)
    @classmethod
    def valid_unicode(cls, value):
        if value is not None:
            try:
                value.get_secret_value().encode("utf-8")
            except UnicodeError:
                raise ValueError("密码包含无法保存的字符。") from None
        return value


class EmailInput(AuthInput):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        return normalize_email(value)


class Login(EmailInput):
    password: CurrentPassword


class AddMember(EmailInput):
    role: Role
    display_name: Name | None = None
    password: NewPassword | None = None


class ChangeRole(AuthInput):
    role: Role


class ChangePassword(AuthInput):
    old_password: CurrentPassword
    new_password: NewPassword


class WorkspaceInput(AuthInput):
    workspace_id: UUID


class NewWorkspace(AuthInput):
    name: Name


@dataclass(frozen=True)
class Context:
    user_id: str
    workspace_id: str
    role: Role
    email: str
    display_name: str
    workspace_name: str
    session_digest: str = field(repr=False)

    @property
    def permissions(self):
        return ROLE_PERMISSIONS[self.role]


class Identity:
    def __init__(self, store, *, secure_cookie=True, session_hours=12):
        if not 1 <= session_hours <= 168:
            raise ValueError("Session lifetime must be between 1 and 168 hours")
        self.store = store
        self.secure_cookie = secure_cookie
        self.session_seconds = session_hours * 3600
        self.cookie_name = "__Host-launch_session" if secure_cookie else "launch_session"
        self.router = self._router()

    def initialize(
        self, bootstrap_email="", bootstrap_password="", bootstrap_workspace="默认工作区"
    ):
        if bool(bootstrap_email) != bool(bootstrap_password):
            raise ValueError("Bootstrap email and password must be provided together")
        with self.store.connect(write=True) as db:
            if getattr(db, "postgres", False):
                db.execute("SELECT pg_advisory_xact_lock(412970613)")
            for statement in SCHEMA:
                db.execute(statement)
            if db.execute("SELECT id FROM users LIMIT 1").fetchone():
                return
            if not bootstrap_email:
                raise ValueError("初次启动需要配置 BOOTSTRAP_EMAIL 和 BOOTSTRAP_PASSWORD。")
            try:
                bootstrap = AddMember(
                    email=bootstrap_email,
                    display_name="工作区管理员",
                    password=SecretStr(bootstrap_password),
                    role="owner",
                )
                space = NewWorkspace(name=bootstrap_workspace)
            except ValueError:
                raise ValueError(
                    "First owner needs a valid email, password and workspace"
                ) from None
            user_id = str(uuid4())
            stamp = int(time.time())
            db.execute(
                """INSERT INTO users (id,email,display_name,password_hash,created_at)
                VALUES (?,?,?,?,?)""",
                (
                    user_id,
                    bootstrap.email,
                    bootstrap.display_name,
                    hash_password(bootstrap.password.get_secret_value()),
                    stamp,
                ),
            )
            self._create_workspace(db, user_id, space.name, "workspace.bootstrap")

    def context(self, request):
        digest = self._session_digest(request)
        if not digest:
            raise HTTPException(401, "请先登录。")
        with self.store.connect() as db:
            return self._context(db, digest)

    def require(self, permission: Permission):
        def authorized(request: Request):
            context = self.context(request)
            if permission not in context.permissions:
                raise HTTPException(403, "当前角色没有这项权限。")
            expected = request.headers.get("x-workspace-id")
            if expected is not None and expected != context.workspace_id:
                raise HTTPException(409, "工作区已切换，请刷新后重试。")
            return context

        return authorized

    def _session_digest(self, request):
        token = request.cookies.get(self.cookie_name, "")
        if SESSION_TOKEN.fullmatch(token):
            return token_digest(token)
        return None

    @staticmethod
    def _context(db, digest):
        row = db.execute(
            """SELECT s.user_id,s.workspace_id,m.role,u.email,u.display_name,
            w.name AS workspace_name
            FROM sessions s JOIN users u ON u.id=s.user_id
            JOIN workspaces w ON w.id=s.workspace_id
            JOIN workspace_members m ON m.workspace_id=s.workspace_id AND m.user_id=s.user_id
            WHERE s.digest=? AND s.expires_at>?""",
            (digest, int(time.time())),
        ).fetchone()
        if not row:
            raise HTTPException(401, "登录已过期，请重新登录。")
        return Context(
            user_id=row["user_id"],
            workspace_id=row["workspace_id"],
            role=row["role"],
            email=row["email"],
            display_name=row["display_name"],
            workspace_name=row["workspace_name"],
            session_digest=digest,
        )

    @staticmethod
    def _workspaces(db, user_id):
        return [
            dict(row)
            for row in db.execute(
                """SELECT w.id,w.name,m.role FROM workspace_members m
                JOIN workspaces w ON w.id=m.workspace_id
                WHERE m.user_id=? ORDER BY m.joined_at,w.id""",
                (user_id,),
            )
        ]

    @classmethod
    def _response(cls, db, context):
        return {
            "authenticated": True,
            "demo": False,
            "user": {
                "id": context.user_id,
                "email": context.email,
                "display_name": context.display_name,
            },
            "workspace": {
                "id": context.workspace_id,
                "name": context.workspace_name,
                "role": context.role,
            },
            "workspaces": cls._workspaces(db, context.user_id),
            "permissions": context.permissions,
        }

    def _cookie(self, response, token):
        response.set_cookie(
            self.cookie_name,
            token,
            max_age=self.session_seconds,
            httponly=True,
            secure=self.secure_cookie,
            samesite="lax",
            path="/",
        )

    def _reserve_attempt(self, email, address):
        stamp = int(time.time())
        cutoff = stamp - LOGIN_WINDOW
        budgets = [
            (token_digest("address:" + address), 60),
            (token_digest("email:" + email), 8),
        ]
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM login_throttle WHERE window_start<=?", (cutoff,))
            for key, cap in budgets:
                row = db.execute(
                    """INSERT INTO login_throttle (key,window_start,attempts) VALUES (?,?,1)
                    ON CONFLICT(key) DO UPDATE SET attempts=login_throttle.attempts+1
                    WHERE login_throttle.attempts<?
                    RETURNING attempts""",
                    (key, stamp, cap),
                ).fetchone()
                if row is None:
                    raise HTTPException(
                        429,
                        "登录尝试过多，请稍后再试。",
                        headers={"Retry-After": str(LOGIN_WINDOW)},
                    )

    def _login(self, body, request, response):
        address = request.client.host if request.client else "unknown"
        self._reserve_attempt(body.email, address)
        with self.store.connect() as db:
            user = db.execute(
                "SELECT id,password_hash FROM users WHERE email=?", (body.email,)
            ).fetchone()
        candidate_hash = user["password_hash"] if user else DUMMY_HASH
        correct = verify_password(body.password.get_secret_value(), candidate_hash)
        if not correct or not user:
            raise HTTPException(401, INVALID_CREDENTIALS)

        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        stamp = int(time.time())
        with self.store.connect(write=True) as db:
            # Serialize session creation with password changes.
            changed = db.execute(
                "UPDATE users SET password_hash=password_hash WHERE id=? AND password_hash=?",
                (user["id"], candidate_hash),
            ).rowcount
            if not changed:
                raise HTTPException(401, INVALID_CREDENTIALS)
            workspaces = self._workspaces(db, user["id"])
            if not workspaces:
                raise HTTPException(401, INVALID_CREDENTIALS)
            workspace_id = workspaces[0]["id"]
            # Ensure membership still exists while inserting the session.
            retained = db.execute(
                """UPDATE workspace_members SET role=role WHERE workspace_id=? AND user_id=?""",
                (workspace_id, user["id"]),
            ).rowcount
            if not retained:
                raise HTTPException(401, INVALID_CREDENTIALS)
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (stamp,))
            previous = self._session_digest(request)
            if previous:
                db.execute("DELETE FROM sessions WHERE digest=?", (previous,))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?)",
                (digest, user["id"], workspace_id, stamp, stamp + self.session_seconds),
            )
            db.execute(
                "DELETE FROM login_throttle WHERE key=?", (token_digest("email:" + body.email),)
            )
            self.store.audit(db, workspace_id, user["id"], "auth.login", user["id"], {})
            result = self._response(db, self._context(db, digest))
        self._cookie(response, token)
        return result

    def _logout(self, request, response):
        digest = self._session_digest(request)
        if digest:
            with self.store.connect(write=True) as db:
                row = db.execute(
                    "SELECT workspace_id,user_id FROM sessions WHERE digest=?", (digest,)
                ).fetchone()
                deleted = db.execute("DELETE FROM sessions WHERE digest=?", (digest,)).rowcount
                if deleted and row:
                    self.store.audit(
                        db,
                        row["workspace_id"],
                        row["user_id"],
                        "auth.logout",
                        row["user_id"],
                        {},
                    )
        response.delete_cookie(
            self.cookie_name,
            path="/",
            secure=self.secure_cookie,
            httponly=True,
            samesite="lax",
        )
        return {"authenticated": False}

    @staticmethod
    def _switch(db, context, workspace_id):
        retained = db.execute(
            "UPDATE workspace_members SET role=role WHERE workspace_id=? AND user_id=?",
            (workspace_id, context.user_id),
        ).rowcount
        if not retained:
            raise HTTPException(404, "找不到可以访问的工作区。")
        updated = db.execute(
            "UPDATE sessions SET workspace_id=? WHERE digest=? AND expires_at>?",
            (workspace_id, context.session_digest, int(time.time())),
        ).rowcount
        if not updated:
            raise HTTPException(401, "登录已过期，请重新登录。")

    def _create_workspace(self, db, user_id, name, action):
        workspace_id, stamp = str(uuid4()), int(time.time())
        db.execute("INSERT INTO workspaces VALUES (?,?,?)", (workspace_id, name, stamp))
        db.execute(
            "INSERT INTO workspace_members VALUES (?,?,'owner',?)",
            (workspace_id, user_id, stamp),
        )
        self.store.audit(db, workspace_id, user_id, action, workspace_id, {"name": name})
        return workspace_id

    def _lock_owner(self, db, context):
        # One owner update per workspace at a time, including concurrent attempts
        # to demote or remove the final two owners.
        db.execute("UPDATE workspaces SET name=name WHERE id=?", (context.workspace_id,))
        fresh = self._context(db, context.session_digest)
        if fresh.role != "owner" or fresh.workspace_id != context.workspace_id:
            raise HTTPException(403, "当前角色没有这项权限。")

    @staticmethod
    def _keep_owner(db, workspace_id, existing_role):
        if existing_role != "owner":
            return
        remaining = db.execute(
            """SELECT COUNT(*) AS owners FROM workspace_members
            WHERE workspace_id=? AND role='owner'""",
            (workspace_id,),
        ).fetchone()["owners"]
        if remaining == 1:
            raise HTTPException(409, "工作区至少需要一位所有者。")

    @staticmethod
    def _members(db, workspace_id):
        return [
            dict(row)
            for row in db.execute(
                """SELECT u.id,u.email,u.display_name,m.role FROM workspace_members m
                JOIN users u ON u.id=m.user_id
                WHERE m.workspace_id=? ORDER BY u.email""",
                (workspace_id,),
            )
        ]

    def _add_member(self, body, context):
        new_hash = hash_password(body.password.get_secret_value()) if body.password else None
        with self.store.connect(write=True) as db:
            self._lock_owner(db, context)
            stamp = int(time.time())
            if new_hash and body.display_name:
                db.execute(
                    """INSERT INTO users (id,email,display_name,password_hash,created_at)
                    VALUES (?,?,?,?,?) ON CONFLICT(email) DO NOTHING""",
                    (str(uuid4()), body.email, body.display_name, new_hash, stamp),
                )
            user = db.execute(
                "SELECT id,email,display_name,password_hash FROM users WHERE email=?", (body.email,)
            ).fetchone()
            if not user:
                raise HTTPException(422, "新成员需要姓名和至少 12 个字符的初始密码。")
            if new_hash and user["password_hash"] != new_hash:
                raise HTTPException(409, "此邮箱已有账号，请清空初始密码再添加。")
            inserted = db.execute(
                """INSERT INTO workspace_members VALUES (?,?,?,?)
                ON CONFLICT(workspace_id,user_id) DO NOTHING""",
                (context.workspace_id, user["id"], body.role, stamp),
            ).rowcount
            if not inserted:
                raise HTTPException(409, "这个用户已经在工作区中。")
            self.store.audit(
                db,
                context.workspace_id,
                context.user_id,
                "member.add",
                user["id"],
                {"role": body.role},
            )
            return {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "role": body.role,
            }

    def _change_member(self, user_id, role, context):
        workspace_id = context.workspace_id
        with self.store.connect(write=True) as db:
            self._lock_owner(db, context)
            row = db.execute(
                "SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?",
                (workspace_id, user_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "该用户不是工作区成员。")
            existing = row["role"]
            if role == existing:
                return {"id": user_id, "role": role}
            self._keep_owner(db, workspace_id, existing)
            if role is None:
                db.execute(
                    "DELETE FROM sessions WHERE workspace_id=? AND user_id=?",
                    (workspace_id, user_id),
                )
                db.execute(
                    "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
                    (workspace_id, user_id),
                )
                action = "member.remove"
            else:
                db.execute(
                    "UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
                    (role, workspace_id, user_id),
                )
                action = "member.role_change"
            self.store.audit(
                db,
                workspace_id,
                context.user_id,
                action,
                user_id,
                {"previous_role": existing, "role": role},
            )
            return {"id": user_id, "role": role}

    def _password(self, body, context):
        with self.store.connect() as db:
            old_hash = db.execute(
                "SELECT password_hash FROM users WHERE id=?", (context.user_id,)
            ).fetchone()["password_hash"]
        old_password = body.old_password.get_secret_value()
        new_password = body.new_password.get_secret_value()
        if not verify_password(old_password, old_hash):
            raise HTTPException(403, "当前密码不正确。")
        if old_password == new_password:
            raise HTTPException(422, "请设置一个不同的新密码。")
        new_hash = hash_password(new_password)
        with self.store.connect(write=True) as db:
            fresh = self._context(db, context.session_digest)
            changed = db.execute(
                "UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",
                (new_hash, context.user_id, old_hash),
            ).rowcount
            if not changed:
                raise HTTPException(409, "密码已发生变化，请重新提交。")
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND digest<>?",
                (context.user_id, context.session_digest),
            )
            self.store.audit(
                db,
                fresh.workspace_id,
                context.user_id,
                "auth.password_change",
                context.user_id,
                {},
            )
        return {"updated": True}

    def _router(self):
        router = APIRouter(prefix="/api", tags=["账户和工作区"])
        read, manage = self.require("read"), self.require("manage")

        @router.get("/auth/me")
        def me(context: Context = Depends(read)):
            with self.store.connect() as db:
                return self._response(db, context)

        @router.post("/auth/login")
        def login(body: Login, request: Request, response: Response):
            return self._login(body, request, response)

        @router.post("/auth/logout")
        def logout(request: Request, response: Response):
            return self._logout(request, response)

        @router.post("/auth/workspace")
        def switch_workspace(body: WorkspaceInput, context: Context = Depends(read)):
            with self.store.connect(write=True) as db:
                self._switch(db, context, str(body.workspace_id))
                return self._response(db, self._context(db, context.session_digest))

        @router.post("/auth/workspaces", status_code=201)
        def create_workspace(body: NewWorkspace, context: Context = Depends(manage)):
            with self.store.connect(write=True) as db:
                self._lock_owner(db, context)
                workspace_id = self._create_workspace(
                    db,
                    context.user_id,
                    body.name,
                    "workspace.create",
                )
                self._switch(db, context, workspace_id)
                return self._response(db, self._context(db, context.session_digest))

        @router.get("/members")
        def members(context: Context = Depends(manage)):
            with self.store.connect() as db:
                return self._members(db, context.workspace_id)

        @router.post("/members", status_code=201)
        def add_member(body: AddMember, context: Context = Depends(manage)):
            return self._add_member(body, context)

        @router.patch("/members/{user_id}")
        def set_role(user_id: UUID, body: ChangeRole, context: Context = Depends(manage)):
            return self._change_member(str(user_id), body.role, context)

        @router.delete("/members/{user_id}")
        def remove_member(user_id: UUID, context: Context = Depends(manage)):
            return self._change_member(str(user_id), None, context)

        @router.post("/auth/password")
        def change_password(body: ChangePassword, context: Context = Depends(read)):
            return self._password(body, context)

        return router
