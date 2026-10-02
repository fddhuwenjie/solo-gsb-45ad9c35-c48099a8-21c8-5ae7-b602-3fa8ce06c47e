"""API 请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

NodeType = Literal["event", "evidence"]
EdgeKind = Literal["causes", "supports", "refutes"]
# 事件触发模式：ANY=任一前因事件发生即触发；ALL=全部前因事件均发生才触发。
# 没有前因（causes 入度为 0）的事件默认发生，trigger_mode 对其无影响。
TriggerMode = Literal["ANY", "ALL"]


def to_utc_iso(dt: datetime) -> str:
    """把带时区的时间统一换算为 UTC，并序列化为 `...Z` 形式（词序即时间序）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_aware(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field} 必须带时区，例如 2026-09-20T10:00:00+08:00")
    return dt


# ---------- 提交一组修改 ----------

class NodeCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: NodeType
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    pos_x: float
    pos_y: float
    # 仅事件节点有效；可留空（None）。必须带时区，后端统一换算为 UTC。
    earliest_time: Optional[datetime] = None
    latest_time: Optional[datetime] = None
    # 仅证据节点有效：reliability 为证据可靠度（0~1）；source_group 为来源组，
    # 同组证据在每个方向只取 reliability 最高的一条。
    reliability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_group: Optional[str] = Field(default=None, max_length=100)
    # 仅事件节点有效：触发模式，缺省按 ANY（任一前因发生即可触发）
    trigger_mode: Optional[TriggerMode] = None

    @field_validator("earliest_time", "latest_time")
    @classmethod
    def _tz_required(cls, v: Optional[datetime], info) -> Optional[datetime]:
        return _check_aware(v, info.field_name) if v is not None else v


class NodeUpdate(BaseModel):
    # 注意：区分「未提供」与「显式置空（清除该界限）」需配合 model_fields_set 使用，
    # None 在语义上表示清除，因此不能把 None 当作「未修改」的默认值。
    id: str
    title: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = None
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None
    earliest_time: Optional[datetime] = None
    latest_time: Optional[datetime] = None
    reliability: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    source_group: Optional[str] = Field(default=None, max_length=100)
    # 仅事件节点有效；显式传 None 等价于重置为 ANY（由 graph 层处理）
    trigger_mode: Optional[TriggerMode] = None

    @field_validator("earliest_time", "latest_time")
    @classmethod
    def _tz_required(cls, v: Optional[datetime], info) -> Optional[datetime]:
        return _check_aware(v, info.field_name) if v is not None else v


class EdgeCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    source_id: str
    target_id: str
    kind: EdgeKind
    label: str = ""


class EdgeUpdate(BaseModel):
    id: str
    label: Optional[str] = None
    # 仅允许在证据连线 supports / refutes 之间切换立场（causes 不可改），由 graph 层校验
    kind: Optional[EdgeKind] = None


class Changeset(BaseModel):
    base_version_id: int
    node_creates: list[NodeCreate] = []
    node_updates: list[NodeUpdate] = []
    node_deletes: list[str] = []
    edge_creates: list[EdgeCreate] = []
    edge_updates: list[EdgeUpdate] = []
    edge_deletes: list[str] = []


class CommitRequest(BaseModel):
    changes: Changeset
    message: str = Field(min_length=1, max_length=200)


class CommitResponse(BaseModel):
    version_id: int
    message: str
    created_at: str


# ---------- 变更提案 / 三方合并 ----------

class ProposalSaveRequest(BaseModel):
    # 提案标题（不生成版本，只把 changeset 绑定到 base_version_id 保存）
    title: str = Field(min_length=1, max_length=200)
    changes: Changeset


class MergePreviewRequest(BaseModel):
    # 两个必须同基线、且均未合并的提案 id
    proposal_a_id: int = Field(ge=1)
    proposal_b_id: int = Field(ge=1)


class MergeResolutionItem(BaseModel):
    # 与合并预览返回的 conflict.key 一一对应；
    # choice：a=采纳提案 A，b=采纳提案 B，base=保留基线，drop=丢弃双方新建
    key: str = Field(min_length=1, max_length=200)
    choice: Literal["a", "b", "base", "drop"]


class MergeCommitRequest(BaseModel):
    proposal_a_id: int = Field(ge=1)
    proposal_b_id: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=200)
    resolutions: list[MergeResolutionItem] = []


# ---------- 重复事件归并（当前工作图：预览 / 确认落库） ----------

class NodeMergePreviewRequest(BaseModel):
    # 保留事件：归并后该节点 id 保留；重复事件在新版本中删除
    keep_id: str = Field(min_length=1, max_length=64)
    # 一个或多个同一事故的重复事件（须均为事件节点，且不含 keep_id）
    duplicate_ids: list[str] = Field(min_length=1, max_length=100)
    # 字段冲突的临时取值（仅影响预览图）：field -> 取值来源候选节点 id
    field_resolutions: dict[str, str] = {}
    # 乐观锁：不传或等于当前最新版本才计算；过期由服务端拒绝
    base_version_id: Optional[int] = None

    @field_validator("duplicate_ids")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("重复事件列表中存在相同的节点 id")
        return v


class NodeMergeCommitRequest(NodeMergePreviewRequest):
    message: str = Field(min_length=1, max_length=200)


# ---------- 回滚 ----------

class RollbackRequest(BaseModel):
    target_version_id: int
    message: Optional[str] = Field(default=None, max_length=200)


# ---------- 反事实干预沙盘（只读推演，不产生 changeset / 版本） ----------

class CounterfactualRequest(BaseModel):
    # 不传 version_id 即在当前最新版本上推演；传历史快照 id 则按该版本推演
    version_id: Optional[int] = None
    target_id: str = Field(min_length=1, max_length=64)
    prevented_ids: list[str] = []

    @field_validator("prevented_ids")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in v:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out


# ---------- 根因共识盲评 ----------

class ReviewCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    version_id: int = Field(ge=1)
    # 候选事件 3~20 个；是否重复 / 是否为该版本事件由 review 层校验
    candidate_ids: list[str] = Field(min_length=3, max_length=20)
    reviewers: list[str] = Field(min_length=1, max_length=100)
    min_votes: int = Field(ge=1)


class ReviewBallotRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=64)
    # 必须恰好是全部候选的一个排列，由 review 层校验
    ranking: list[str] = Field(min_length=1, max_length=20)


class ReviewCloseRequest(BaseModel):
    # 封榜乐观锁：必须等于当前 session_revision，否则 409
    expected_revision: int = Field(ge=1)


# ---------- 根因结论签发（从已封榜盲评场签发不可变最终根因集合） ----------

class IssuanceIssueRequest(BaseModel):
    # 用户提交的最终根因集合：服务端在写事务内重新校验其恰好覆盖最高并列名次
    node_ids: list[str] = Field(min_length=1, max_length=20)
    note: str = Field(min_length=1, max_length=4000)  # 签发说明（必填）


class IssuanceValidateRequest(BaseModel):
    # 预检（只读）：不传 / 传 null 时默认按最高并列名次校验；note 可留空
    node_ids: Optional[list[str]] = Field(default=None, max_length=20)
    note: Optional[str] = Field(default=None, max_length=4000)


# ---------- 签发有效性复核（不可变签发记录 × 目标版本；只追加复核记录） ----------

class IssuanceRecheckRequest(BaseModel):
    # 目标版本：复核在写事务内加载签发绑定版本、签发 payload 与该版本整图按原规则重算
    target_version_id: int = Field(ge=1)
    note: Optional[str] = Field(default=None, max_length=4000)  # 复核说明（可留空）


# ---------- 通用错误 ----------

class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
    cycle: Optional[list[str]] = None      # 成环时返回环上的节点 id
    conflict: Optional[dict[str, Any]] = None  # 时间窗冲突时返回两端节点与因果链
