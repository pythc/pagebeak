"""Evidence-backed assertions; negating one value never asserts global absence."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from akb.evidence_text import date_bounds, temporal_value


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


KINDS = {
    "profile": "个人信息",
    "employment": "任职经历",
    "education": "教育经历",
    "paper": "论文",
    "project": "项目",
    "teaching": "教学活动",
    "award": "荣誉",
}
FIELDS = {
    "profile": {"name": "姓名", "email": "邮箱", "phone": "联系电话", "research_interests": "研究方向"},
    "employment": {"organization": "工作单位", "job_title": "职称", "administrative_position": "行政职务"},
    "education": {"institution": "学校", "degree": "学位", "major": "专业"},
    "paper": {
        "title": "论文题名",
        "doi": "DOI",
        "status": "发表状态",
        "authorship": "作者身份",
        "year": "年份",
    },
    "project": {
        "title": "项目名称",
        "role": "项目角色",
        "status": "项目状态",
        "amount_yuan": "金额（元）",
        "year": "年份",
    },
    "teaching": {"title": "教学活动", "hours": "学时", "enrolments": "学生人次", "year": "年份", "status": "完成状态"},
    "award": {"title": "荣誉名称", "level": "奖项等级", "rank": "排名", "year": "年份"},
}
Kind = Literal["profile", "employment", "education", "paper", "project", "teaching", "award"]


class KnowledgeProposal(Input):
    kind: Kind
    subject: str = Field(min_length=1, max_length=200)
    subject_scope: Literal["self", "reference"] = "reference"
    entity_key: str = Field(min_length=1, max_length=250)
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=500)
    polarity: Literal["positive", "negative", "absent"] = "positive"
    valid_from: str | None = Field(default=None, description="原文开始时间，YYYY / YYYY-MM / YYYY-MM-DD；不补猜月日")
    valid_to: str | None = Field(default=None, description="原文结束时间，同样保留年/月/日精度；至今为 null，保留 time_quote")
    evidence_id: str = Field(min_length=36, max_length=36)
    quote: str = Field(min_length=1, max_length=4000)
    time_quote: str = Field(default="", max_length=1000)
    subject_evidence_id: str | None = Field(default=None, min_length=36, max_length=36)
    subject_quote: str = Field(default="", max_length=1000)

    @field_validator("valid_from", "valid_to")
    @classmethod
    def validate_time(cls, value):
        return temporal_value(value) if value is not None else None

    @model_validator(mode="after")
    def validate_fact(self):
        if self.field not in FIELDS[self.kind]:
            raise ValueError("此知识类型不支持该字段")
        if self.valid_from and self.valid_to and date_bounds(self.valid_from)[0] > date_bounds(self.valid_to)[1]:
            raise ValueError("生效时间不能晚于失效时间")
        if not all(v.strip() for v in (self.subject, self.entity_key, self.value, self.quote)):
            raise ValueError("知识内容不能为空白")
        if self.polarity == "absent" and self.value != "无":
            raise ValueError("明确不存在使用 absent 和值 无；未发现资料不产生事实")
        return self


class KnowledgeSubmission(Input):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)
    items: list[KnowledgeProposal] = Field(default_factory=list, max_length=100)
    complete: bool = False
    reviewed_evidence_ids: list[str] = Field(default_factory=list, max_length=10000)
    note: str = Field(default="", max_length=2000)


class KnowledgeDecisionInput(Input):
    expected_revision: int = Field(ge=1)
    action: Literal["accept", "reject", "correct"]
    value: str | None = Field(default=None, min_length=1, max_length=500)
    polarity: Literal["positive", "negative", "absent"] | None = None
    subject_scope: Literal["self", "reference"] | None = None


class KnowledgeAcceptanceItem(Input):
    fact_id: str = Field(min_length=36, max_length=36)
    expected_revision: int = Field(ge=1)


class KnowledgeAcceptance(Input):
    items: list[KnowledgeAcceptanceItem] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000, description="依据已读原文的采纳理由；不能冒充用户本人确认")

    @model_validator(mode="after")
    def unique_items(self):
        if len({item.fact_id for item in self.items}) != len(self.items):
            raise ValueError("同一批次不能重复列出事实")
        if not self.reason.strip():
            raise ValueError("采纳理由不能为空白")
        return self


class KnowledgeSelector(Input):
    kind: Kind
    field: str = Field(min_length=1, max_length=40)
    operation: Literal["value", "count", "sum"] = "value"
    entity_key: str | None = Field(default=None, min_length=1, max_length=250)
    filters: dict[str, str] = Field(default_factory=dict, max_length=10)
    as_of: date | None = None

    @model_validator(mode="after")
    def validate_selector(self):
        if self.field not in FIELDS[self.kind] or any(k not in FIELDS[self.kind] for k in self.filters):
            raise ValueError("查询字段或过滤条件不属于该知识类型")
        if self.operation == "sum" and self.field not in {"amount_yuan", "hours", "enrolments"}:
            raise ValueError("只能对金额、学时或人次求和")
        return self
