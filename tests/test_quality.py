import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from studio.main import create_app
from studio.model import Model, ModelFailure, Settings, end_trace, start_trace
from studio.rules import RULESET_VERSION, check_draft, claims, source_catalog
from studio.schemas import Draft, Insights
from studio.workflow import GroundedInsights, ListingResponse, listing_sources, review_citations


def draft(title="Schreibtischregal", detail="Für Büroartikel.", search="regal büro"):
    return Draft(
        title=title,
        bullets=["Schwarz", "Für Aufbewahrung", "Mit Anleitung", "Offene Fächer", "Für den Tisch"],
        description=detail,
        search_terms=search,
    )


def test_whole_dimensions_and_ranges_have_to_match_the_same_fact():
    snapshot = {
        "source": "商品原始规格",
        "facts": [
            {"label": "尺寸", "value": "32 × 21 × 30 cm"},
            {"label": "工作温度", "value": "-5–10 °C"},
            {"label": "另一尺寸", "value": "30 × 40 × 45 cm"},
        ],
        "features": [],
        "reviews": [{"id": "review-1", "text": "感觉是 98 × 21 × 30 cm."}],
    }
    good = check_draft(draft(detail="32cm × 21cm × 30cm, -5 bis 10°C"), snapshot, "DE")
    evidence = [entry["evidence"][0] for entry in good if entry["code"] == "numeric_reference"]
    assert [entry["id"] for entry in evidence] == ["spec-1", "spec-2"]
    assert all(entry["source"] == "商品原始规格" for entry in evidence)
    wrong = check_draft(draft(detail="98 × 21 × 30 cm, 1 bis 10°C"), snapshot, "DE")
    assert len([entry for entry in wrong if entry["code"] == "unsupported_numeric"]) == 2
    assert "review-1" not in str(wrong)
    assert all(entry["rule_version"] == RULESET_VERSION for entry in wrong)


def test_decimal_aliases_binding_and_ambiguous_thousands():
    assert next(claims("2,5 公斤")).signature == next(claims("2.5 kg")).signature
    assert next(claims("3-Tier")).signature == next(claims("3 层")).signature
    assert next(claims("2.500 mAh")).signature != next(claims("2500 mAh")).signature
    assert next(claims("2,500.50 mAh")).signature == next(claims("2500,5 mAh")).signature
    assert next(claims("10–20h")).signature != next(claims("20h")).signature
    assert next(claims("Up to 10kg")).signature != next(claims("At least 10kg")).signature


def test_checks_use_market_rules_and_show_blockers_first():
    snapshot = {"facts": [], "features": []}
    sample = draft(title="A" * 161, detail="TODO: Bestseller bei www.example.net", search="ö" * 121)
    checks = check_draft(sample, snapshot, "DE")
    assert checks[0]["code"] == "placeholder" and checks[0]["level"] == "error"
    assert {(c["field"], c["code"]) for c in checks} >= {
        ("标题", "length"),
        ("搜索词", "length"),
        ("详情", "risk_phrase"),
        ("详情", "external_contact"),
    }
    assert not any(
        c["field"] == "标题" and c["code"] == "length" for c in check_draft(sample, snapshot, "US")
    )


def test_citation_is_resolved_to_exact_original_comment_and_record():
    reviews = [{"id": "R010", "text": "表面容易擦干净，但边角粗糙。"}]
    finding = {
        "kind": "positive",
        "text": "日常容易清理",
        "review_ids": ["R010"],
        "quotes": [{"review_id": "R010", "quote": "表面容易擦干净"}],
    }
    review_citations(GroundedInsights(findings=[finding]), reviews)
    finding["quotes"][0]["quote"] = "防水耐用"
    with pytest.raises(ModelFailure, match="原文"):
        review_citations(GroundedInsights(findings=[finding]), reviews)

    items = source_catalog({"facts": [{"label": "外观", "value": "黑色"}], "features": []})
    response = ListingResponse(**draft().model_dump(), field_source_ids={"title": ["spec-1"]})
    assert listing_sources(response, items)["title"][0]["value"] == "黑色"
    response.field_source_ids = {"title": ["R010"]}
    with pytest.raises(ModelFailure, match="无效"):
        listing_sources(response, items)


def test_provider_retries_capacity_once_and_records_safe_metadata():
    calls = []
    sent = []

    def reply(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        body = {
            "choices": [{"message": {"content": json.dumps({"findings": []})}}],
            "usage": {"prompt_tokens": 16, "completion_tokens": 5, "untrusted": "secret"},
        }
        return httpx.Response(200, json=body, headers={"x-request-id": "trace-123"})

    settings = Settings(
        _env_file=None, llm_base_url="https://example.com/v1", llm_api_key="test-secret"
    )
    model = Model(settings, transport=httpx.MockTransport(reply))
    token = start_trace(calls)
    try:
        assert asyncio.run(model.generate("user-provided prompt", {}, Insights)).findings == []
    finally:
        end_trace(token)
    assert len(sent) == 2 and all(s.url.path == "/v1/chat/completions" for s in sent)
    assert len(calls) == 1 and calls[0]["attempts"] == 2
    assert calls[0]["usage"] == {"prompt_tokens": 16, "completion_tokens": 5}
    assert calls[0]["request_id"] == "trace-123"
    assert "secret" not in str(calls) and "prompt" not in calls[0]


@pytest.mark.parametrize(
    ("status", "headers", "content", "failure"),
    [
        (401, {}, "private failure", "provider_auth"),
        (429, {"Retry-After": "120"}, "private failure", "provider_capacity"),
        (200, {}, "x" * 256_001, "response_limit"),
    ],
)
def test_provider_does_not_retry_rejected_large_or_rate_limited_requests(
    status, headers, content, failure
):
    count = 0

    def reply(_):
        nonlocal count
        count += 1
        return httpx.Response(status, headers=headers, content=content)

    settings = Settings(
        _env_file=None, llm_base_url="https://example.com/v1", llm_api_key="test-secret"
    )
    model = Model(settings, transport=httpx.MockTransport(reply))
    with pytest.raises(ModelFailure) as error:
        asyncio.run(model.generate("test", {}, Insights))
    assert error.value.code == failure and count == 1
    assert "private failure" not in str(error.value)


def test_pipeline_preserves_exact_comments_sources_and_per_job_usage(tmp_path):
    def reply(request):
        payload = json.loads(request.content)
        inputs = json.loads(payload["messages"][-1]["content"])
        if "reviews" in inputs:
            review = inputs["reviews"][0]
            answer = {
                "findings": [
                    {
                        "kind": "positive",
                        "text": "整理后的桌面更整洁。",
                        "review_ids": [review["id"]],
                        "quotes": [{"review_id": review["id"], "quote": review["text"]}],
                    }
                ]
            }
        else:
            answer = {
                **draft(detail="32 × 21 × 30 cm.").model_dump(),
                "field_source_ids": {"description": ["spec-3"]},
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"total_tokens": 10},
            },
        )

    settings = Settings(
        _env_file=None,
        database_path=str(tmp_path / "studio.db"),
        llm_base_url="https://example.com/v1",
        llm_api_key="test-secret",
        allow_demo=False,
    )
    model = Model(settings, transport=httpx.MockTransport(reply))
    with TestClient(create_app(settings, model)) as client:
        product = client.get("/api/products").json()[0]
        submitted = client.post(
            "/api/tasks",
            json={
                "product_id": product["id"],
                "market": "US",
                "language": "en-US",
                "request_id": str(uuid4()),
            },
        )
        response = client.get(f"/api/tasks/{submitted.json()['id']}/events")
        assert "event: end" in response.text
        task = client.get(f"/api/tasks/{submitted.json()['id']}").json()
    assert task["status"] == "completed", task.get("error")
    first = task["insights"]["findings"][0]
    assert first["quotes"][0]["review_id"] == task["snapshot"]["reviews"][0]["id"]
    assert first["quotes"][0]["quote"] == task["snapshot"]["reviews"][0]["text"]
    run = task["run_metadata"]
    assert [call["usage"]["total_tokens"] for call in run["calls"]] == [10, 10]
    assert run["field_sources"]["description"][0]["value"] == "32 × 21 × 30 cm"
    assert run["field_sources_version"] == 1
