"""Source-bound arithmetic and exact output receipts; no second knowledge engine."""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select

from akb import services
from akb.evidence_text import visible_text
from akb.memory_evidence import EvidenceAnchor, resolve
from akb.memory_library import digest, source_version
from akb.models import FillingReceipt, Source, now
from akb.schemas import Input


class CalculationRow(Input):
    entity_key: str = Field(min_length=1, max_length=200)
    value: str = Field(default="", max_length=2000)
    unit: str = Field(default="", max_length=20)
    attributes: dict[str, str | None] = Field(default_factory=dict, max_length=20)
    evidence: EvidenceAnchor
    unit_evidence: EvidenceAnchor | None = None
    value_column: int | None = Field(default=None, ge=1, le=200)


class FillingField(Input):
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=200)
    operation: Literal["extract", "sum", "count", "unknown", "compose"]
    value_type: Literal["text", "number", "integer"] = "text"
    unit: str = Field(default="", max_length=20)
    filters: dict[str, str] = Field(default_factory=dict, max_length=20)
    rows: list[CalculationRow] = Field(default_factory=list, max_length=500)
    field_keys: list[str] = Field(default_factory=list, max_length=80)
    missing_reason: str = Field(default="", max_length=500)


class FillingPlan(Input):
    request_id: UUID
    fields: list[FillingField] = Field(min_length=1, max_length=80)


class FillingCheck(Input):
    values: dict[str, str] = Field(max_length=80)
    artifact: Literal["draft", "web_readback", "word_readback"]


def number(value):
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value) or len(value) > 50:
        raise HTTPException(422, "数值须为明确十进制数字，千分位需合法；不接受估算、范围或公式")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise HTTPException(422, "非法数值") from None


def rendered(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def source_manifest(session):
    # An uploaded but not-yet-parsed source already changes the evidence universe.
    # Include failed/unsupported files too; an unchanged omission is disclosed by
    # the library API, while retrying its parser invalidates the previous receipt.
    from akb.models import LocalSourceLink

    local_states = {}
    for link in session.scalars(select(LocalSourceLink)):
        local_states.setdefault(link.source_id, []).append([link.id, link.current_hash, link.stale])
    return {
        src.id: digest([source_version(src), src.status] + ([sorted(local_states[src.id])] if src.id in local_states else []))
        for src in session.scalars(
            select(Source).where(Source.revoked_at.is_(None), Source.trashed_at.is_(None))
        )
    }


def convert(value, source, target):
    if not source or not target:
        raise HTTPException(422, "数量必须明确原始单位和目标单位")
    if source == target:
        return value
    if {source, target} <= {"元", "万元"}:
        return value * (Decimal(10000) if source == "万元" else Decimal("0.0001"))
    raise HTTPException(422, "单位不兼容，不换算币种，也不把批准额当到账额")


def unit_in_header(unit, text):
    # Match longest known unit first: 元 must never match inside 万元, 人 inside 人次.
    explicit = re.findall(r"[（(]([^（）()]+)[）)]", text)
    unit_text = " ".join(explicit) if explicit else text
    known = re.findall(r"万元|千元|亿元|美元|欧元|港元|日元|元|学时|小时|人次|人|篇|门|项|个", unit_text)
    return set(known) == {unit}


def numeric_value(session, row, field, anchor):
    raw = visible_text(row.value)
    text = visible_text(anchor["quote"])
    number_pattern = r"(?<![\d.,+-])" + re.escape(raw) + r"(?![\d.,])"
    if not re.search(number_pattern, text):
        raise HTTPException(422, "数值必须完整存在于原文，不能从年份或其他数字中截取")
    value = number(raw)
    if not row.unit and not field.unit and field.operation == "extract" and field.value_type == "integer":
        if row.unit_evidence or row.value_column:
            raise HTTPException(422, "无量纲整数不使用单位表头")
        return value, None
    if row.unit_evidence:
        header = resolve(session, row.unit_evidence)
        h, r = header["locator"], anchor["locator"]
        if (
            header["source_id"] != anchor["source_id"]
            or header["source_version"] != anchor["source_version"]
            or h.get("kind") not in {"csv_row", "table", "cells"}
            or h.get("kind") != r.get("kind")
            or h.get("table") != r.get("table")
            or h.get("sheet") != r.get("sheet")
            or h.get("row") != 1
            or not isinstance(r.get("row"), int)
            or r["row"] <= 1
        ):
            raise HTTPException(422, "单位表头须为同一来源版本、同一表/工作表的第一行，且在数据行之前")
        col = row.value_column
        hc, rc = h.get("cells", []), r.get("cells", [])
        if not col or col > len(hc) or col > len(rc):
            raise HTTPException(422, "需要 value_column 及完整单元格定位；旧解析资料请 reparse_source 后重读")
        head, cell = visible_text(hc[col - 1]), visible_text(rc[col - 1]).strip()
        if cell != raw or head not in visible_text(header["quote"]) or not unit_in_header(row.unit, head):
            raise HTTPException(422, "数值和单位必须对应同列单元格；表头引文须完整包含该列名称")
        return convert(value, row.unit, field.unit), header
    if row.value_column is not None:
        raise HTTPException(422, "value_column 需要 unit_evidence")
    # Bind the unit to this number, not any unrelated amount elsewhere in the quote.
    unit = re.escape(row.unit)
    pair = number_pattern + r"\s*(?:[|：:]\s*)?" + unit + r"(?![A-Za-z次])"
    reverse = r"(?<![万千百亿美欧日港澳新加])" + unit + r"\s*[:：=]\s*" + number_pattern
    if not row.unit or not (re.search(pair, text) or re.search(reverse, text)):
        raise HTTPException(
            422, "该数值与原单位未直接关联；表格请提供 unit_evidence 和 value_column，不得把万元当元"
        )
    return convert(value, row.unit, field.unit), None


def _compute(session, plan):
    values, details, versions = {}, {}, {}
    keys = [f.key for f in plan.fields]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "字段 key 重复")
    with localcontext() as ctx:
        ctx.prec = 80
        for field in plan.fields:
            if field.operation == "compose":
                continue
            if field.field_keys:
                raise HTTPException(422, "只有 compose 可引用其他字段")
            if field.operation == "unknown":
                if field.rows or not field.missing_reason:
                    raise HTTPException(422, "缺失字段不得提供猜测值，须说明未找到依据")
                values[field.key], details[field.key] = (
                    "",
                    {"status": "declared_missing", "reason": field.missing_reason},
                )
                continue
            if not field.rows:
                raise HTTPException(422, "无记录不能推断为零；请使用 unknown")
            included, excluded, duplicates, anchors = {}, [], [], []
            for row_index, row in enumerate(field.rows, start=1):
                anchor = resolve(session, row.evidence)
                versions[anchor["source_id"]] = anchor["source_version"]
                text = visible_text(anchor["quote"])
                if visible_text(row.entity_key) not in text:
                    raise HTTPException(422, f"第 {row_index} 条记录的 entity_key 不在同条引文中")
                invalid_attributes = [
                    key for key, value in row.attributes.items()
                    if value is not None and visible_text(value) not in text
                ]
                if invalid_attributes:
                    raise HTTPException(422,
                        f"第 {row_index} 条记录的属性值不在同条引文中：{', '.join(invalid_attributes)}。"
                        "attributes 只填写原文属性；不要添加 exclude/duplicate 等处理标签，"
                        "排除由 filters 判断，重复由 entity_key 判断")
                # A definite exclusion is enough, even if an unrelated attribute is unknown.
                if any(
                    row.attributes.get(k) is not None and row.attributes[k] != v
                    for k, v in field.filters.items()
                ):
                    excluded.append(row.entity_key)
                    continue
                if any(row.attributes.get(k) is None for k in field.filters):
                    raise HTTPException(422, "相关记录缺少筛选属性，不能静默排除或纳入")
                if field.operation == "count":
                    if field.value_type != "integer" or not field.unit:
                        raise HTTPException(422, "计数目标必须是 integer 并明确单位")
                    val = Decimal(1)
                elif field.value_type in {"number", "integer"}:
                    val, header = numeric_value(session, row, field, anchor)
                    if header:
                        anchors.append(header)
                else:
                    if field.operation != "extract" or field.unit:
                        raise HTTPException(422, "文本字段只支持原文提取，不能填入计数或金额")
                    if not row.value or visible_text(row.value) not in text:
                        raise HTTPException(422, "文本值必须得到引文支持；改写请在知识页保留解释与出处")
                    val = visible_text(row.value)
                if row.entity_key in included:
                    if included[row.entity_key] != val:
                        raise HTTPException(409, "同一事项存在冲突值，需核对原文后解决")
                    duplicates.append(row.entity_key)
                else:
                    included[row.entity_key] = val
                anchors.append(anchor)
            if not included:
                raise HTTPException(422, "筛选后没有已核实记录，不能证明数量为零")
            if field.operation == "extract":
                distinct = set(included.values())
                if len(distinct) != 1:
                    raise HTTPException(409, "单值字段有多个不同结果")
                value = next(iter(distinct))
            else:
                if field.value_type not in {"number", "integer"}:
                    raise HTTPException(422, "求和/计数不能输出文本")
                value = sum(included.values(), Decimal(0))
            if isinstance(value, Decimal):
                if field.value_type == "integer" and value != value.to_integral_value():
                    raise HTTPException(422, "目标字段只接受整数，不能悄悄舍入")
                value = rendered(value)
            values[field.key] = value
            details[field.key] = {
                "status": "checked",
                "included": list(included),
                "excluded": excluded,
                "duplicates": duplicates,
                "unit": field.unit,
                "evidence": anchors,
            }
    by_key = {f.key: f for f in plan.fields}
    for field in plan.fields:
        if field.operation != "compose":
            continue
        if (
            field.rows
            or field.filters
            or field.unit
            or field.value_type != "text"
            or not field.field_keys
            or len(set(field.field_keys)) != len(field.field_keys)
        ):
            raise HTTPException(422, "compose 仅引用不重复的普通字段，不能自行添加事实")
        if any(
            k not in values or by_key[k].operation == "compose" or not values[k] for k in field.field_keys
        ):
            raise HTTPException(422, "叙述依赖的字段缺失或未通过校验")
        values[field.key] = "".join(
            f"{by_key[k].label}：{values[k]}{by_key[k].unit}。" for k in field.field_keys
        )
        details[field.key] = {"status": "controlled_wording", "field_keys": field.field_keys}
    return values, details, versions


def compute(session, plan):
    # Return all ordinary-field failures in one response; never issue partial receipts.
    keys = [f.key for f in plan.fields]
    if len(keys) != len(set(keys)):
        raise HTTPException(422, "字段 key 重复")
    errors = []
    for field in plan.fields:
        if field.operation == "compose":
            continue
        try:
            _compute(session, FillingPlan(request_id=plan.request_id, fields=[field]))
        except HTTPException as exc:
            if exc.status_code not in {409, 422}:
                raise
            errors.append({"field_key": field.key, "status": exc.status_code, "message": exc.detail})
    if errors:
        raise HTTPException(
            409 if any(e["status"] == 409 for e in errors) else 422,
            {"code": "invalid_filling_fields", "errors": errors},
        )
    return _compute(session, plan)


def prepare(session, principal, plan):
    services.lock_tenant(session, principal.tenant_id)
    request_hash = digest(plan.model_dump(mode="json"))
    previous = session.scalar(select(FillingReceipt).where(FillingReceipt.request_id == str(plan.request_id)))
    if previous and previous.request_hash != request_hash:
        raise HTTPException(409, "request_id 已用于其他填写计划")
    values, details, versions = compute(session, plan)
    used_versions = versions
    versions = source_manifest(session)
    hashes = {k: digest(v) for k, v in values.items()}
    if previous and (previous.source_versions != versions or previous.expires_at <= now()):
        raise HTTPException(409, "校验回执已过期，请新建计划")
    receipt = previous or FillingReceipt(
        tenant_id=principal.tenant_id,
        request_id=str(plan.request_id),
        request_hash=request_hash,
        value_hashes=hashes,
        source_versions=versions,
        expires_at=now() + timedelta(days=1),
    )
    if not previous:
        session.add(receipt)
    session.flush()
    return {
        "receipt_id": receipt.id,
        "values": values,
        "details": details,
        "source_versions": used_versions,
        "expires_at": receipt.expires_at,
        "limits": "校验原文锚点、提交记录的去重、算术与输出一致性；模型须核实字段含义及记录完整性，不证明未提交的遗漏记录不存在。",
    }


def validate(session, receipt_id, body):
    receipt = session.get(FillingReceipt, receipt_id)
    if not receipt:
        raise HTTPException(404, "填写回执不存在")
    sources = source_manifest(session)
    if receipt.source_versions != sources or receipt.expires_at <= now():
        raise HTTPException(409, "原资料已变化或填写回执到期，请重新计算")
    missing = sorted(set(receipt.value_hashes) - set(body.values))
    extra = sorted(set(body.values) - set(receipt.value_hashes))
    mismatched = sorted(
        k
        for k in body.values
        if k in receipt.value_hashes and digest(body.values[k]) != receipt.value_hashes[k]
    )
    return {
        "valid": not (missing or extra or mismatched),
        "missing": missing,
        "extra": extra,
        "mismatched": mismatched,
        "artifact": body.artifact,
        "limits": "仅校验调用者提交的回读值；不独立证明浏览器已保存或 Word 已落盘。",
    }
