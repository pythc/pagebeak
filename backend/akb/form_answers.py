"""Destination contracts, exact numeric formatting and evidence-preserving prose.

Contracts describe the destination, not its DOM: callers must read them from the
actual form. The server does not claim to infer arbitrary website semantics.
"""

from decimal import Decimal, InvalidOperation

from akb.knowledge_schema import FIELDS, KINDS

NUMERIC_UNITS = {"amount_yuan": "元", "hours": "学时", "enrolments": "人次"}


def block(answer, reason):
    answer.update(status="blocked", value=None, reason=reason)


def apply_contract(field, answer):
    query = field.knowledge
    if not query:
        return
    answer["binding"] = query.model_dump(mode="json")
    if not field.target:
        block(answer, "请先读取目标表单，提供字段含义、筛选条件、操作、类型和单位契约")
        return
    target = field.target
    answer["target"] = target.model_dump(mode="json")
    if target.knowledge != query:
        block(answer, "查询与目标字段不一致：核对字段、操作、筛选条件、事项和时点，不能用数量填写名称")
        return
    numeric = query.operation != "value" or query.field in NUMERIC_UNITS
    expected_type = "integer" if query.operation == "count" else "number" if numeric else "text"
    if target.value_type != expected_type and not (expected_type == "number" and target.value_type == "integer"):
        block(answer, "目标数据类型与查询结果不匹配")
        return
    unit = None
    if query.operation == "count":
        if target.unit not in {None, "项", "篇", "门"}:
            block(answer, "计数字段不能使用金额或工作量单位")
            return
        unit = target.unit
    elif numeric:
        unit = NUMERIC_UNITS[query.field]
        allowed = {"元", "万元"} if unit == "元" else {unit}
        if target.unit not in allowed:
            block(answer, "数值字段必须明确目标单位，且单位须与原始字段相容")
            return
    elif target.unit is not None:
        block(answer, "文字字段不能设置数值单位")
        return
    answer["contract_matched"] = True
    if answer["status"] != "ready" or not numeric:
        return
    from akb.knowledge import number

    raw = answer["value"]
    try:
        # SUM is already expressed in the canonical unit; VALUE still holds source text.
        canonical = Decimal(raw) if query.operation != "value" else number(raw, query.field)
        output = canonical / Decimal(10000) if target.unit == "万元" else canonical
        if not output.is_finite() or (target.value_type == "integer" and output != output.to_integral_value()):
            raise ValueError("非有限值或不是整数")
        formatted = format(output, "f")
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        answer["value"] = formatted
        answer["conversion"] = {"raw_value": raw, "canonical_value": format(canonical, "f"),
                                "canonical_unit": unit, "output_unit": target.unit,
                                "method": "decimal_exact", "rounded": False}
    except (ValueError, InvalidOperation, KeyError):
        block(answer, "数值、单位或整数精度不符合目标要求，未猜测或四舍五入")


def apply_constraints(field, answer):
    if answer["status"] != "ready":
        return
    value = answer["value"]
    if len(value) > field.max_length:
        block(answer, "完整事实超过长度限制，未截断内容")
    elif field.allowed_values and value not in field.allowed_values:
        block(answer, "现有事实不在表单允许的选项内")


def render_narrative(field, answers):
    """Compose canonical clauses; never accept model-authored connective assertions.

    Unknown fields stay visible in issues and block the whole paragraph, rather
    than silently disappearing. User-provided destination labels are not facts.
    """
    selected = [answers[key] for key in field.narrative.field_keys]
    result = {"key": field.key, "label": field.label, "status": "ready", "value": None,
              "reason": "按已核对字段生成；保留主体、筛选条件和单位，未作自由语义改写",
              "evidence_ids": [], "claims": [], "sentences": [], "pending_tasks": 0,
              "render_mode": "evidence_fields_v1"}
    unavailable = [a["key"] for a in selected if a["status"] != "ready"]
    if unavailable:
        block(result, "段落依赖字段尚不可用：" + "、".join(unavailable))
        return result
    for answer in selected:
        query = answer.get("binding")
        if query:
            labels = FIELDS[query["kind"]]
            context = "本人" + KINDS[query["kind"]]
            constraints = [f"{labels[k]}：{v}" for k, v in query["filters"].items()]
            if query.get("as_of"):
                constraints.append("核对时点：" + query["as_of"])
            if constraints:
                context += "（" + "；".join(constraints) + "）"
            operation = {"value": "", "sum": "合计", "count": "事项数量"}[query["operation"]]
            label = "" if query["operation"] == "count" else labels[query["field"]]
            if query["field"] == "amount_yuan" and query["operation"] != "count":
                # The canonical field is stored in yuan; the rendered value can use wan.
                label = "金额"
            unit = answer.get("target", {}).get("unit") or ""
            if query["operation"] == "count":
                context += "（仅统计已采纳可用记录）"
            elif query["operation"] == "sum":
                context += "（仅汇总已采纳可用记录）"
            text = f"{context}：{label}{operation}为{answer['value']}{unit}。"
        else:
            # Legacy profile fields have a fixed service-owned label, never a caller's label.
            label = next((fields[answer.get("fact_key")] for fields in FIELDS.values()
                          if answer.get("fact_key") in fields), None)
            if not label:
                block(result, "段落字段缺少已核对的语义映射：" + answer["key"])
                return result
            text = f"本人的{label}为{answer['value']}。"
        sentence = {"text": text, "field_key": answer["key"],
                    "evidence_ids": answer["evidence_ids"],
                    "fact_ids": [c["id"] for c in answer["claims"]]}
        result["sentences"].append(sentence)
        result["evidence_ids"].extend(answer["evidence_ids"])
        result["claims"].extend(answer["claims"])
        result["pending_tasks"] = max(result["pending_tasks"], answer.get("pending_tasks", 0))
    result["value"] = "".join(s["text"] for s in result["sentences"])
    result["evidence_ids"] = list(dict.fromkeys(result["evidence_ids"]))
    result["claims"] = list({c["id"]: c for c in result["claims"]}.values())
    apply_constraints(field, result)
    return result


def validate_output(draft, values):
    if draft["stale"]:
        return {"valid": False, "issues": [{"code": "stale", "message": "资料已变化，须重新生成草稿"}]}
    expected = {a["key"]: a["value"] if a["status"] == "ready" else "" for a in draft["answers"]}
    issues = [{"key": key, "code": "missing_field"} for key in expected.keys() - values.keys()]
    issues += [{"key": key, "code": "unknown_field"} for key in values.keys() - expected.keys()]
    issues += [{"key": key, "code": "changed_value", "message": "填写值与草稿不一致，禁止改写、改单位或补填未知"}
               for key in expected.keys() & values.keys() if values[key] != expected[key]]
    return {"valid": not issues, "issues": issues, "snapshot_revision": draft["snapshot_revision"],
            "scope": "仅核对传入的填写值；实际网页或 Word 仍须保存后回读"}
