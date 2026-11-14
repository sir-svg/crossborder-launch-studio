from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MARKETS = {
    "US": {"label": "Amazon 美国站", "language": "en-US", "language_name": "英语"},
    "DE": {"label": "Amazon 德国站", "language": "de-DE", "language_name": "德语"},
}
TEMPLATE = "listing-launch"
TEMPLATE_VERSION = 1
STEPS = [
    {"key": "load", "label": "读取商品资料", "description": "固定本次使用的规格、关键词和评论。"},
    {"key": "insights", "label": "归纳评论", "description": "整理带有评论出处的买家观点。"},
    {"key": "draft", "label": "生成上架稿", "description": "生成标题、五点描述、详情和搜索词。"},
    {"key": "check", "label": "核对文案", "description": "检查缺项、风险词和缺少来源的参数。"},
]

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Fact(Input):
    label: ShortText
    value: Line


class Product(Input):
    sku: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
    name: ShortText
    facts: list[Fact] = Field(default_factory=list, max_length=16)
    features: list[ShortText] = Field(default_factory=list, max_length=15)
    keywords: list[ShortText] = Field(default_factory=list, max_length=30)
    source: ShortText = "卖家录入"


class Review(Input):
    id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")]
    rating: int = Field(ge=1, le=5)
    text: Annotated[str, StringConstraints(min_length=1, max_length=1500)]
    source: ShortText


class TaskInput(Input):
    product_id: UUID
    market: Literal["US", "DE"]
    language: Literal["en-US", "de-DE"]
    notes: str = Field(default="", max_length=600)
    request_id: UUID


class Retry(Input):
    request_id: UUID


class Finding(Input):
    kind: Literal["positive", "negative", "selling_point"]
    text: ShortText
    review_ids: list[str] = Field(min_length=1, max_length=5)


class Insights(Input):
    findings: list[Finding] = Field(max_length=10)


class Draft(Input):
    title: Line
    bullets: list[Line] = Field(min_length=5, max_length=5)
    description: str = Field(max_length=4500)
    search_terms: Line


class Edit(Input):
    base_version: int = Field(ge=1)
    draft: Draft


class Approve(Input):
    version: int = Field(ge=1)


class ReviewImport(Input):
    csv_text: str = Field(min_length=1, max_length=160_000)
