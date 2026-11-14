from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from studio.identity import Context, Identity
from studio.security import OriginGuardMiddleware, token_digest
from studio.store import Store

ROOT = {"email": "owner@example.test", "password": "temporary owner password"}
EDITOR = {"email": "editor@example.test", "password": "temporary editor password"}


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path / "identity.db")
    store.initialize(seed=False)
    identity = Identity(store, secure_cookie=True)
    identity.initialize(ROOT["email"], ROOT["password"], "北区")
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware, allowed_origins=["https://studio.example.test"])
    app.include_router(identity.router)

    @app.post("/edit")
    def edit(context: Context = Depends(identity.require("edit"))):
        return {"workspace_id": context.workspace_id}

    @app.post("/review")
    def review(context: Context = Depends(identity.require("review"))):
        return {"workspace_id": context.workspace_id}

    yield app, identity, store
    store.close()


def client_for(app):
    return TestClient(app, base_url="https://studio.example.test")


def test_first_owner_configuration_is_required_and_not_reapplied(tmp_path):
    store = Store(tmp_path / "bootstrap.db")
    store.initialize(seed=False)
    identity = Identity(store)
    with pytest.raises(ValueError, match="BOOTSTRAP_EMAIL"):
        identity.initialize()
    identity.initialize(ROOT["email"], ROOT["password"])
    identity.initialize()
    identity.initialize(EDITOR["email"], EDITOR["password"])
    with store.connect() as db:
        assert [row["email"] for row in db.execute("SELECT email FROM users")] == [ROOT["email"]]
        assert db.execute("SELECT count(*) AS total FROM workspaces").fetchone()["total"] == 1
    store.close()


def test_logins_and_member_removal_revoke_stored_sessions(service):
    app, identity, store = service
    with client_for(app) as owner, client_for(app) as editor:
        assert owner.get("/api/auth/me").status_code == 401
        response = owner.post("/api/auth/login", json=ROOT)
        session = owner.cookies.get(identity.cookie_name)
        assert session and "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        state = response.json()
        space_id = state["workspace"]["id"]
        with store.connect() as db:
            row = db.execute("SELECT digest FROM sessions").fetchone()
            assert row["digest"] == token_digest(session)
            password_hash = db.execute("SELECT password_hash FROM users").fetchone()[
                "password_hash"
            ]
            assert password_hash.startswith("pbkdf2-sha256$")
            assert ROOT["password"] not in password_hash
        assert owner.delete("/api/members/" + state["user"]["id"]).status_code == 409
        member = owner.post(
            "/api/members",
            json={**EDITOR, "display_name": "录入", "role": "editor"},
        ).json()
        response = editor.post("/api/auth/login", json=EDITOR)
        assert response.json()["permissions"] == ["read", "edit"]
        assert editor.post("/edit").status_code == 200
        assert editor.post("/review").status_code == 403
        assert editor.get("/api/members").status_code == 403
        updated = owner.patch("/api/members/" + member["id"], json={"role": "reviewer"})
        assert updated.status_code == 200
        assert editor.post("/edit").status_code == 403
        assert editor.post("/review").status_code == 200
        assert owner.delete("/api/members/" + member["id"]).status_code == 200
        assert editor.get("/api/auth/me").status_code == 401
        assert editor.post("/api/auth/login", json=EDITOR).status_code == 401
        audit = [item["action"] for item in store.audit_log(space_id, 0, 100)]
        assert {"workspace.bootstrap", "member.add", "member.role_change", "member.remove"} <= set(
            audit
        )


def test_workspace_scope_origin_and_session_password_revocation(service):
    app, _, _ = service
    with client_for(app) as first, client_for(app) as second:
        assert (
            first.post(
                "/api/auth/login", json=ROOT, headers={"origin": "https://different.example.test"}
            ).status_code
            == 403
        )
        state = first.post("/api/auth/login", json=ROOT).json()
        assert second.post("/api/auth/login", json=ROOT).status_code == 200
        old_id = state["workspace"]["id"]
        new_state = first.post("/api/auth/workspaces", json={"name": "南区"}).json()
        new_id = new_state["workspace"]["id"]
        assert old_id != new_id and len(new_state["workspaces"]) == 2
        assert first.get("/api/auth/me", headers={"x-workspace-id": old_id}).status_code == 409
        assert first.post("/edit", headers={"x-workspace-id": new_id}).json() == {
            "workspace_id": new_id
        }
        assert second.get("/api/auth/me").json()["workspace"]["id"] == old_id
        assert (
            first.post("/api/auth/workspace", json={"workspace_id": str(uuid4())}).status_code
            == 404
        )
        new_password = "another well formed password"
        password_updated = first.post(
            "/api/auth/password",
            json={"old_password": ROOT["password"], "new_password": new_password},
        )
        assert password_updated.status_code == 200
        assert first.get("/api/auth/me").status_code == 200
        assert second.get("/api/auth/me").status_code == 401
        assert second.post("/api/auth/login", json=ROOT).status_code == 401
        assert (
            second.post("/api/auth/login", json={**ROOT, "password": new_password}).status_code
            == 200
        )
        assert first.post("/api/auth/logout").json() == {"authenticated": False}
        assert first.get("/api/auth/me").status_code == 401


def test_login_attempt_budget_persists_in_database(service):
    app, identity, store = service
    email = "unknown@example.test"
    for _ in range(8):
        identity._reserve_attempt(email, "test-address")
    with client_for(app) as client:
        response = client.post("/api/auth/login", json={"email": email, "password": "bad"})
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0
    with store.connect() as db:
        row = db.execute(
            "SELECT attempts FROM login_throttle WHERE key=?", (token_digest("email:" + email),)
        ).fetchone()
        assert row["attempts"] == 8
