from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from akb.knowledge_schema import KnowledgeSelector

FactKey = Literal["name", "email", "organization", "job_title", "phone", "research_interests"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)


class Register(Login):
    display_name: str = Field(min_length=1, max_length=80)


class TokenRequest(Input):
    label: str = Field(min_length=1, max_length=80)
    scopes: list[Literal["knowledge:read", "knowledge:write", "profile:read", "forms:write"]] = Field(
        min_length=1, max_length=4
    )


class TextImport(Input):
    title: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=200000)
    subject_scope: Literal["self", "reference"] = "reference"


class Search(Input):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=30)


class DecisionInput(Input):
    action: Literal["accept", "reject", "correct"]
    expected_revision: int = Field(ge=1)
    value: str | None = Field(default=None, max_length=500)


class ProposedFact(Input):
    field: FactKey
    value: str = Field(min_length=1, max_length=250)
    evidence_id: str = Field(min_length=36, max_length=36)
    quote: str = Field(min_length=1, max_length=4000)


class Proposals(Input):
    source_id: str = Field(min_length=36, max_length=36)
    expected_revision: int = Field(ge=1)
    items: list[ProposedFact] = Field(min_length=1, max_length=30)


class PageInput(Input):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    fact_ids: list[str] = Field(default_factory=list, max_length=100)
    expected_revision: int | None = Field(default=None, ge=1)


class CatalogEdit(Input):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class CatalogEntryEdit(Input):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=60)
    summary: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=4000)


class FormTarget(Input):
    """The destination's contract, read from the actual form before choosing a query."""

    knowledge: KnowledgeSelector
    value_type: Literal["text", "number", "integer"] = "text"
    unit: Literal["元", "万元", "学时", "人次", "项", "篇", "门"] | None = None


class FormNarrative(Input):
    field_keys: list[str] = Field(min_length=1, max_length=30)


class FormField(Input):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=80)
    max_length: int = Field(default=500, ge=1, le=4000)
    allowed_values: list[str] = Field(default_factory=list, max_length=50)
    allow_sensitive: bool = False
    knowledge: KnowledgeSelector | None = None
    target: FormTarget | None = None
    narrative: FormNarrative | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.narrative and (self.knowledge or self.target):
            raise ValueError("事迹段落只引用本草稿字段，不另设查询或目标映射")
        if self.target and not self.knowledge:
            raise ValueError("目标契约须与知识查询一起提供")
        return self


class FormInput(Input):
    title: str = Field(min_length=1, max_length=200)
    fields: list[FormField] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_fields(self):
        fields = {field.key: field for field in self.fields}
        if len(fields) != len(self.fields):
            raise ValueError("表单字段 key 不能重复")
        for field in self.fields:
            if field.narrative:
                keys = field.narrative.field_keys
                if len(set(keys)) != len(keys) or any(k not in fields or fields[k].narrative for k in keys):
                    raise ValueError("事迹段落须引用不重复且存在的普通字段，不能嵌套段落")
        return self


class FormOutput(Input):
    values: dict[str, str] = Field(max_length=30)


class UploadInput(Input):
    folder_id: str | None = Field(default=None, max_length=36)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=120)
    size: int = Field(gt=0, le=2 * 1024**3)
    subject_scope: Literal["self", "reference"] = "reference"
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class QuotaInput(Input):
    quota_bytes: int = Field(ge=0, le=1024**5)
    expected_quota_bytes: int = Field(ge=0)


class CleanupCitation(Input):
    evidence_id: str = Field(min_length=36, max_length=36)
    quote: str = Field(min_length=1, max_length=1000)


class CleanupSummary(Input):
    source_id: str = Field(min_length=36, max_length=36)
    content: str = Field(min_length=1, max_length=12000)
    citations: list[CleanupCitation] = Field(min_length=1, max_length=20)


class CleanupInput(Input):
    source_ids: list[str] = Field(min_length=1, max_length=20)
    mode: Literal["delete", "summarize"] = "delete"
    summaries: list[CleanupSummary] = Field(default_factory=list, max_length=20)


class CleanupApproval(Input):
    acknowledge_loss: bool = False
