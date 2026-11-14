"""Versioned, deterministic checks for the draft review desk."""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from studio.schemas import Draft

RULESET_VERSION = "listing-quality-2"

# Internal review thresholds. Platform thresholds vary by category and can change.
FIELD_LABELS = {
    "title": "标题",
    **{f"bullet_{i}": f"五点 {i}" for i in range(1, 6)},
    "description": "详情",
    "search_terms": "搜索词",
}
TITLE_LENGTH = {"US": 180, "DE": 160}
RISK_TERMS = {
    "US": r"\bbest\s*seller\b|\bguaranteed\b|\bguarantee\b|\b100\s*%\s*safe\b",
    "DE": r"\bbestseller\b|\bgarantiert\b|\b100\s*%\s*sicher\b",
}
COMMON_RISKS = (
    r"\bfda[\s-]*approved\b|\bce[\s-]*(?:certified|zertifiziert)\b"
    r"|\bclinically[\s-]*proven\b|\bmedical[\s-]*grade\b|永久保证|全网第一|医疗级"
)
PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|TK)\b|待填写|待补充|请填入", re.IGNORECASE)
WEB_LINK = re.compile(r"(?:https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,})", re.I)
LEVEL_ORDER = {"error": 0, "warning": 1, "info": 2}

UNIT_ALIASES = {
    "mm": ("mm", "毫米"),
    "cm": ("cm", "厘米"),
    "m": ("m", "米"),
    "in": ("inches", "inch", "″", "英寸"),
    "ft": ("feet", "foot", "ft", "英尺"),
    "kg": ("kg", "公斤", "千克"),
    "g": ("g", "grams", "gram", "克"),
    "lb": ("lbs", "lb", "pounds", "pound", "磅"),
    "oz": ("oz", "ounces", "ounce", "盎司"),
    "ml": ("ml", "毫升"),
    "l": ("l", "liters", "litres", "liter", "litre", "升"),
    "h": ("h", "hrs", "hr", "hours", "hour", "stunden", "stunde", "小时"),
    "min": ("min", "minutes", "minute", "minuten", "分钟"),
    "mah": ("mah", "毫安时"),
    "w": ("w", "watts", "watt", "瓦"),
    "v": ("v", "volts", "volt", "伏"),
    "%": ("%",),
    "tier": ("tiers", "tier", "ebenen", "ebene", "层"),
    "piece": ("pcs", "pieces", "piece", "stücke", "stück", "件"),
    "°c": ("°c", "摄氏度"),
    "°f": ("°f", "华氏度"),
}
ALIAS_TO_UNIT = {alias: unit for unit, aliases in UNIT_ALIASES.items() for alias in aliases}
UNIT = "|".join(re.escape(unit) for unit in sorted(ALIAS_TO_UNIT, key=len, reverse=True))
NUMBER = r"[+-]?\d{1,12}(?:[.,]\d{1,12}){0,3}"
MEASURE = rf"{NUMBER}(?:[ \t]*-?[ \t]*(?:{UNIT})(?![a-z0-9]))?"
MEASURE_PARTS = re.compile(rf"(?P<number>{NUMBER})(?:[ \t]*-?[ \t]*(?P<unit>{UNIT}))?")
DIMENSION = re.compile(rf"(?<![a-z0-9.,]){MEASURE}(?:[ \t]*[x×✕][ \t]*{MEASURE}){{1,3}}")
RANGE = re.compile(
    rf"(?<![a-z0-9.,])(?P<start>{MEASURE})"
    rf"[ \t]*(?:–|—|~|(?<!-)-(?!-)|\bto\b|\bbis\b|至|到)[ \t]*"
    rf"(?P<end>{MEASURE})"
)
SINGLE = re.compile(
    rf"(?<![a-z0-9.,])(?P<bound>up to\s+|at least\s+|max\.?\s+|min\.?\s+|"
    rf"bis zu\s+|mindestens\s+|höchstens\s+|至少\s*|不超过\s*|[<>≤≥]=?\s*)?"
    rf"(?P<measurement>{NUMBER}[ \t]*-?[ \t]*(?:{UNIT}))(?![a-z0-9])"
)
BOUND_ALIASES = {
    "up to": "max",
    "max": "max",
    "bis zu": "max",
    "höchstens": "max",
    "不超过": "max",
    "<=": "max",
    "≤": "max",
    "<": "lt",
    "at least": "min",
    "min": "min",
    "mindestens": "min",
    "至少": "min",
    ">=": "min",
    "≥": "min",
    ">": "gt",
}


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str
    values: tuple[tuple[str, str], ...]

    @property
    def signature(self):
        return self.kind, self.values


def _decimal(raw):
    """Preserve single-separator ambiguous thousands syntax without choosing a locale."""
    separators = raw.count(".") + raw.count(",")
    if separators == 1:
        integer, decimal = re.split(r"[.,]", raw)
        if len(decimal) == 3 and 1 <= len(integer.lstrip("+-")) <= 3 and int(integer) != 0:
            return "ambiguous:" + raw
    if separators > 1:
        mark = "," if raw.rfind(",") > raw.rfind(".") else "."
        other = "." if mark == "," else ","
        if other not in raw:
            parts = raw.split(mark)
            if all(len(part) == 3 for part in parts[1:]):
                return str(Decimal("".join(parts)).normalize())
            return "literal:" + raw
        integer, fraction = raw.rsplit(mark, 1)
        groups = integer.split(other)
        if mark in integer or not all(len(group) == 3 for group in groups[1:]):
            return "literal:" + raw
        raw = "".join(groups) + "." + fraction
    return str(Decimal(raw.replace(",", ".")).normalize())


def _parts(values):
    parts = [MEASURE_PARTS.fullmatch(value.strip()) for value in values]
    if not all(parts):
        return None
    units = [ALIAS_TO_UNIT.get(part["unit"]) for part in parts]
    shared = {unit for unit in units if unit}
    if not shared or (None in units and len(shared) != 1):
        return None
    return tuple(
        (_decimal(part["number"]), unit or next(iter(shared)))
        for part, unit in zip(parts, units, strict=True)
    )


def _fold(text):
    return unicodedata.normalize("NFKC", text).casefold()


def claims(text):
    """Match tuples and ranges before scalars, even when endpoints already exist."""
    text = _fold(text).replace("−", "-")
    occupied = []
    for kind, pattern in (("dimensions", DIMENSION), ("range", RANGE), ("scalar", SINGLE)):
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            if kind == "dimensions":
                parts = _parts(re.split(r"[x×✕]", match[0]))
            elif kind == "range":
                parts = _parts((match["start"], match["end"]))
            else:
                parts = _parts((match["measurement"],))
            if not parts:
                continue
            occupied.append(span)
            qualifier = match.groupdict().get("bound")
            mode = BOUND_ALIASES.get(qualifier.strip().rstrip("."), kind) if qualifier else kind
            yield Claim(match[0], mode, parts)


def source_catalog(snapshot):
    source = snapshot.get("source") or "商品资料"
    return [
        {
            "id": f"spec-{index}",
            "label": item["label"],
            "value": item["value"],
            "source": source,
        }
        for index, item in enumerate(snapshot["facts"], 1)
        if item["value"]
    ] + [
        {"id": f"feature-{index}", "label": "商品特点", "value": value, "source": source}
        for index, value in enumerate(snapshot["features"], 1)
        if value
    ]


def _fields(draft):
    return {
        "title": draft.title,
        **{f"bullet_{i}": text for i, text in enumerate(draft.bullets, 1)},
        "description": draft.description,
        "search_terms": draft.search_terms,
    }


def _check(level, code, field, text, evidence=None):
    return {
        "level": level,
        "code": code,
        "field": field,
        "text": text,
        "rule_version": RULESET_VERSION,
        "evidence": evidence or [],
    }


def _check_numeric(text, field, known, related):
    reported = set()
    for claim in claims(text):
        signature = claim.signature
        if signature in reported:
            continue
        reported.add(signature)
        matches = known.get(signature)
        if matches:
            yield _check(
                "info",
                "numeric_reference",
                field,
                f"「{claim.text}」与资料中的数字和单位一致，请核对用于同一规格。",
                matches[:3],
            )
        else:
            alternatives = {}
            for _, unit in claim.values:
                for entry in related[unit]:
                    alternatives[entry["id"]] = entry
            yield _check(
                "warning",
                "unsupported_numeric",
                field,
                f"请为「{claim.text}」核对完整尺寸、数值和单位的来源。",
                list(alternatives.values())[:3],
            )


def _check_field(key, text, market, known, related):
    field = FIELD_LABELS[key]
    if not text.strip():
        yield _check("error", "required", field, "请填写此项。")
        return
    limit = {"title": TITLE_LENGTH[market], "description": 2000, "search_terms": 240}.get(key, 240)
    count = len(text.encode("utf-8")) if key == "search_terms" else len(text)
    unit = "个 UTF-8 字节" if key == "search_terms" else "个字符"
    if count > limit:
        yield _check("warning", "length", field, f"当前 {count} {unit}，工作台建议最多 {limit}。")
    if PLACEHOLDER.search(text):
        yield _check("error", "placeholder", field, "替换未填写的占位内容。")
    if WEB_LINK.search(text):
        yield _check("warning", "external_contact", field, "请核对文案内的链接或联系方式。")
    pattern = COMMON_RISKS + "|" + RISK_TERMS[market]
    for word in dict.fromkeys(re.findall(pattern, _fold(text))):
        yield _check("warning", "risk_phrase", field, f"请复核风险表达：{word}。")
    yield from _check_numeric(text, field, known, related)


def check_draft(draft: Draft, snapshot: dict, market: str = "US"):
    market = market if market in TITLE_LENGTH else "US"
    known = defaultdict(list)
    related = defaultdict(list)
    for evidence in source_catalog(snapshot):
        for claim in claims(evidence["value"]):
            if evidence not in known[claim.signature]:
                known[claim.signature].append(evidence)
            for _, unit in claim.values:
                if evidence not in related[unit]:
                    related[unit].append(evidence)
    checks = [
        check
        for key, text in _fields(draft).items()
        for check in _check_field(key, text, market, known, related)
    ]
    seen_bullets = {}
    for index, text in enumerate(draft.bullets, 1):
        phrase = " ".join(_fold(text).split())
        if phrase and phrase in seen_bullets:
            checks.append(
                _check(
                    "warning",
                    "duplicate_bullet",
                    f"五点 {index}",
                    f"与五点 {seen_bullets[phrase]} 重复，请突出不同卖点。",
                )
            )
        seen_bullets.setdefault(phrase, index)
    keywords = re.findall(r"[^\W_]+", _fold(draft.search_terms))
    if len(set(keywords)) < len(keywords):
        checks.append(_check("info", "duplicate_keyword", "搜索词", "搜索词有重复，可以精简。"))
    checks.extend(
        _check("warning", "missing_source", fact["label"], "商品资料待补充。")
        for fact in snapshot["facts"]
        if not fact["value"]
    )
    return sorted(checks, key=lambda check: LEVEL_ORDER[check["level"]])
