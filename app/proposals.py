"""变更提案（Change Proposal）与三方合并。

提案：
- 用户把一组未提交修改（changeset）保存为绑定 base_version_id 的提案；
- 保存时在写事务内把 changeset 应用到对应基线图上，复用 graph.apply_changes
  做整图校验（成环、时间窗、字段约束等），通过后只写 proposals 表，**不生成版本**。

三方合并：
- 选择两个同基线且未合并的提案，以「基线 / 提案 A 结果 / 提案 B 结果」三方比较：
  - 单侧修改、双方相同修改 → 直接采纳；
  - 同一字段 / 连线被改成不同值、删除与修改相遇、双方新建同 id 实体 → 列为冲突，不落库；
- 用户逐项裁决（a / b / base / drop）后，服务端在 SQLite 写事务（BEGIN IMMEDIATE）中
  重新合并：先确认两份提案仍未使用（status='open'）、当前版本仍等于 base_version_id，
  再把裁决合成新的 changeset 并复用 graph.apply_changes 重新执行成环 / 时间窗等整图校验；
  全部通过才插入一个 kind='merge' 的版本并把两份提案标记为 merged。
  任一步失败 → ROLLBACK，不留下版本或已合并状态。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app import graph
from app.graph import Edge, Graph, GraphError, Node
from app.schemas import (
    Changeset,
    EdgeCreate,
    EdgeUpdate,
    MergeCommitRequest,
    MergePreviewRequest,
    NodeCreate,
    NodeUpdate,
)


class ProposalError(Exception):
    """提案 / 合并业务错误：status_code 由路由层原样返回（状态冲突为 409）。"""

    def __init__(self, message: str, code: str = "invalid", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# 参与三方比较的字段（created_at 不参与；source/target 对已存在连线不可变）
NODE_FIELDS = ("type", "title", "description", "pos_x", "pos_y",
               "earliest_time", "latest_time", "reliability", "source_group", "trigger_mode")
EDGE_FIELDS = ("source_id", "target_id", "kind", "label")
_TIME_FIELDS = ("earliest_time", "latest_time")

NODE_FIELD_LABELS = {
    "type": "类型", "title": "标题", "description": "描述",
    "pos_x": "横坐标", "pos_y": "纵坐标",
    "earliest_time": "最早发生时间", "latest_time": "最晚发生时间",
    "reliability": "可靠度", "source_group": "来源组", "trigger_mode": "触发模式",
}
EDGE_FIELD_LABELS = {
    "source_id": "起点", "target_id": "终点", "kind": "连线类型", "label": "连线说明",
}


# ---------- 读取辅助 ----------

def _row_or_404(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise ProposalError(f"提案不存在：{proposal_id}", "proposal_not_found", 404)
    return row


def _changeset_from_row(row: sqlite3.Row) -> Changeset:
    try:
        return _changeset_from_json(json.loads(row["changes_json"]))
    except ProposalError:
        raise
    except Exception as exc:  # 存储内容理论上一定合法（保存时已校验）
        raise ProposalError(f"提案 {row['id']} 的修改集无法解析：{exc}", "bad_stored_changes")


def dump_changeset(ch: Changeset) -> dict[str, Any]:
    """序列化 changeset 以便入库。

    NodeUpdate / EdgeUpdate 的未提供字段默认是 None（语义为「不修改」），而显式提交
    None 又表示「清除该字段」——两者必须靠 model_fields_set 区分。因此每条更新额外
    存一个下划线开头的 `_fields_set`，加载时经 model_validate 后用它还原 fields_set；
    不影响普通 changeset 语义（该键不在 Pydantic 模型字段内，额外键在模型侧会被忽略）。
"""
    doc = ch.model_dump(mode="json", exclude_none=False)
    for key, models in (("node_updates", ch.node_updates), ("edge_updates", ch.edge_updates)):
        for item, raw in zip(doc[key], models):
            item["_fields_set"] = sorted(raw.model_fields_set)
    return doc


def _changeset_from_json(doc: Any) -> Changeset:
    if not isinstance(doc, dict):
        raise ProposalError("修改集格式非法", "bad_stored_changes")
    parsed = Changeset.model_validate(doc)  # 完整字段校验（含 ISO 时间串 → datetime）
    # model_validate 会把缺省字段也标记为 provided（默认 None）；用保存时附带的
    # _fields_set 还原「未提供 / 显式置空」的区分（apply_changes 依赖该集合决定是否清除字段）。
    # 没有 _fields_set 的旧数据保持 model_validate 的集合（等价于「None 字段都显式提供」，
    # 与旧版保存路径一致，不影响既有提案的语义）。
    for key in ("node_updates", "edge_updates"):
        for model, raw in zip(getattr(parsed, key), doc.get(key, [])):
            fields_set = raw.get("_fields_set")
            if fields_set:
                object.__setattr__(model, "__pydantic_fields_set__", set(fields_set))
    return parsed


def _change_count(ch: Changeset) -> int:
    return len(ch.node_creates) + len(ch.node_updates) + len(ch.node_deletes) \
        + len(ch.edge_creates) + len(ch.edge_updates) + len(ch.edge_deletes)


def _is_empty(ch: Changeset) -> bool:
    return _change_count(ch) == 0


def proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        count = _change_count(_changeset_from_row(row))
    except ProposalError:
        count = 0
    return {
        "id": row["id"],
        "title": row["title"],
        "base_version_id": row["base_version_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "merged_at": row["merged_at"],
        "merged_version_id": row["merged_version_id"],
        "change_count": count,
    }


def list_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM proposals ORDER BY id DESC").fetchall()
    return [proposal_dict(r) for r in rows]


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict[str, Any]:
    row = _row_or_404(conn, proposal_id)
    out = proposal_dict(row)
    # 对前端返回与 /api/changes/commit 同构的干净 changeset（去掉内部的 _fields_set）
    out["changes"] = _changeset_from_row(row).model_dump(mode="json", exclude_none=False)
    return out


def proposal_graphs(conn: sqlite3.Connection, proposal_id: int) -> tuple[dict[str, Any], Graph, Graph]:
    """只读：返回 (提案详情, 基线图, 应用提案后的图)。供单提案预览使用。"""
    row = _row_or_404(conn, proposal_id)
    base_id = row["base_version_id"]
    base = graph.load_graph(conn, base_id)
    if base is None:
        raise ProposalError(f"基准版本不存在：{base_id}", "version_not_found", 404)
    nodes, edges = graph.apply_changes(base, _changeset_from_row(row))
    return proposal_dict(row), base, Graph(base_id, nodes, edges)


# ---------- 保存提案（校验在对应基线上进行；不生成版本） ----------

def save_proposal(conn: sqlite3.Connection, *, title: str, changes: Changeset) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ProposalError("提案标题不能为空", "invalid_title")
    if _is_empty(changes):
        raise ProposalError("没有任何可保存的修改", "empty_changeset")

    base_id = changes.base_version_id
    base = graph.load_graph(conn, base_id)
    if base is None:
        raise ProposalError(f"基准版本不存在：{base_id}", "version_not_found", 404)

    try:
        conn.execute("BEGIN IMMEDIATE")
        # 事务内再确认一次基线存在；复用现有整图校验：
        # 成环 / 时间窗 / 字段约束 / 重复边等任一失败都会抛 GraphError → 回滚
        if conn.execute("SELECT 1 FROM versions WHERE id = ?", (base_id,)).fetchone() is None:
            raise ProposalError(f"基准版本不存在：{base_id}", "version_not_found", 404)
        graph.apply_changes(base, changes)
        cur = conn.execute(
            "INSERT INTO proposals (title, base_version_id, changes_json) VALUES (?, ?, ?)",
            (title, base_id,
             json.dumps(dump_changeset(changes), ensure_ascii=False)),
        )
        pid = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_proposal(conn, pid)


# ---------- 三方比较（纯函数：不写库） ----------

def _node_values(n: Node) -> dict[str, Any]:
    return {
        "type": n.type, "title": n.title, "description": n.description,
        "pos_x": n.pos_x, "pos_y": n.pos_y,
        "earliest_time": n.earliest_time, "latest_time": n.latest_time,
        "reliability": n.reliability, "source_group": n.source_group,
        "trigger_mode": n.trigger_mode,
    }


def _edge_values(e: Edge) -> dict[str, Any]:
    return {"source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label}


def _node_title(nodes: dict[str, Node], nid: str) -> str | None:
    n = nodes.get(nid)
    return n.title if n else None


def _edge_title(edges: dict[str, Edge], nodes: dict[str, Node], eid: str) -> str | None:
    e = edges.get(eid)
    if e is None:
        return None
    s = _node_title(nodes, e.source_id) or e.source_id
    t = _node_title(nodes, e.target_id) or e.target_id
    return f"{s} → {t}"


def _auto(subject: str, xid: str, action: str, side: str,
          title: str | None, field: str | None = None) -> dict[str, Any]:
    out = {"subject": subject, "id": xid, "title": title, "action": action, "side": side}
    if field:
        out["field"] = field
    return out


def _conflict(key: str, subject: str, cid: str, ctype: str, title: str | None,
              *, field: str | None = None, field_label: str | None = None,
              base_v: Any = None, a_v: Any = None, b_v: Any = None,
              options: list[str] | None = None,
              edge: Edge | None = None, nodes: dict[str, Node] | None = None,
              ) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": key, "subject": subject, "id": cid, "type": ctype, "title": title,
        "field": field, "field_label": field_label,
        "base_value": base_v, "a_value": a_v, "b_value": b_v,
        "options": options or ["a", "b", "base"],
    }
    if subject == "edge" and edge is not None and nodes is not None:
        out["source_id"] = edge.source_id
        out["target_id"] = edge.target_id
        out["source_title"] = _node_title(nodes, edge.source_id)
        out["target_title"] = _node_title(nodes, edge.target_id)
    return out


def _resolve_value(resolutions: dict[str, str] | None, key: str,
                   vb: Any, va: Any, vr: Any) -> tuple[str, Any]:
    """按裁决取值；未裁决 / base 回落到基线值。返回 (choice, value)。"""
    choice = (resolutions or {}).get(key, "base")
    if choice == "a":
        return choice, va
    if choice == "b":
        return choice, vr
    return choice, vb


def compute_merge(
    base: Graph,
    ga: Graph,
    gb: Graph,
    resolutions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """三方比较核心：返回 merged 图（version_id 沿用基线）、conflicts、auto_resolved。

    未裁决冲突按「保留基线 / 丢弃新建」给出占位结果，因此预览图可直接渲染；
    最终落库前要求所有冲突都有合法裁决。
    """
    conflicts: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    node_pools = {"base": base.nodes, "a": ga.nodes, "b": gb.nodes}
    edge_pools = {"base": base.edges, "a": ga.edges, "b": gb.edges}

    def node_choice(nid: str) -> str | None:
        """节点级「删除与修改相遇」冲突的裁决（未裁决返回 None）。"""
        return (resolutions or {}).get(f"node:delete:{nid}")

    # ---- 节点 ----
    merged_nodes: dict[str, Node] = {}
    for nid in sorted(set(base.nodes) | set(ga.nodes) | set(gb.nodes)):
        nB, nA, nR = base.nodes.get(nid), ga.nodes.get(nid), gb.nodes.get(nid)

        # 删除与修改相遇 → 节点实体级冲突；若存在一侧根本没碰该节点，则属单侧删除，直接采纳
        if nB is not None and (nA is None) != (nR is None):
            deleted_side = "a" if nA is None else "b"
            kept = nR if nA is None else nA
            key = f"node:delete:{nid}"
            if _node_values(kept) == _node_values(nB):
                auto.append(_auto("node", nid, "delete", deleted_side, nB.title))
                continue
            conflicts.append({
                **_conflict(key, "node", nid, "delete_modify", nB.title,
                           base_v=_node_values(nB),
                           a_v=None if nA is None else _node_values(nA),
                           b_v=None if nR is None else _node_values(nR)),
                "deleted_side": deleted_side,
                "kept_side": "b" if deleted_side == "a" else "a",
            })
            choice = (resolutions or {}).get(key, "base")
            if choice == "a":
                chosen = nA
            elif choice == "b":
                chosen = nR
            else:
                chosen = nB  # base：未裁决时保留基线节点
            if chosen is not None:
                merged_nodes[nid] = Node(id=nid, created_at=chosen.created_at,
                                         **_node_values(chosen))
            continue

        # 双方新建同 id
        if nB is None and nA is not None and nR is not None:
            va, vr = _node_values(nA), _node_values(nR)
            key = f"node:create:{nid}"
            if va == vr:
                auto.append(_auto("node", nid, "create", "both_same", nA.title))
                merged_nodes[nid] = Node(id=nid, created_at=nA.created_at, **va)
                continue
            conflicts.append(_conflict(
                key, "node", nid, "create_create",
                _node_title(ga.nodes, nid) or _node_title(gb.nodes, nid),
                base_v=None, a_v=va, b_v=vr, options=["a", "b", "drop"]))
            choice = (resolutions or {}).get(key)
            if choice == "a":
                merged_nodes[nid] = Node(id=nid, created_at=nA.created_at, **va)
            elif choice == "b":
                merged_nodes[nid] = Node(id=nid, created_at=nR.created_at, **vr)
            # 未裁决 / drop：结果中不含该节点
            continue

        # 单侧新建
        if nB is None:
            if nA is not None:
                auto.append(_auto("node", nid, "create", "a", nA.title))
                merged_nodes[nid] = Node(id=nid, created_at=nA.created_at, **_node_values(nA))
            elif nR is not None:
                auto.append(_auto("node", nid, "create", "b", nR.title))
                merged_nodes[nid] = Node(id=nid, created_at=nR.created_at, **_node_values(nR))
            continue

        # 基线存在：双方都删除
        if nA is None and nR is None:
            auto.append(_auto("node", nid, "delete", "both_same", nB.title))
            continue
        if nA is None:
            auto.append(_auto("node", nid, "delete", "a", nB.title))
            continue
        if nR is None:
            auto.append(_auto("node", nid, "delete", "b", nB.title))
            continue

        # 三方都在：逐字段合并
        vb, va, vr = _node_values(nB), _node_values(nA), _node_values(nR)
        merged_vals = dict(vb)
        a_changed = {f: va[f] for f in NODE_FIELDS if va[f] != vb[f]}
        b_changed = {f: vr[f] for f in NODE_FIELDS if vr[f] != vb[f]}
        for f in NODE_FIELDS:
            in_a, in_b = f in a_changed, f in b_changed
            if not in_a and not in_b:
                continue
            if in_a and not in_b:
                merged_vals[f] = va[f]
                auto.append(_auto("node", nid, "field_change", "a", nB.title, f))
            elif in_b and not in_a:
                merged_vals[f] = vr[f]
                auto.append(_auto("node", nid, "field_change", "b", nB.title, f))
            elif va[f] == vr[f]:
                merged_vals[f] = va[f]
                auto.append(_auto("node", nid, "field_change", "both_same", nB.title, f))
            else:
                key = f"node:field:{nid}:{f}"
                conflicts.append(_conflict(
                    key, "node", nid, "value_diverge", nB.title,
                    field=f, field_label=NODE_FIELD_LABELS.get(f, f),
                    base_v=vb[f], a_v=va[f], b_v=vr[f]))
                _, merged_vals[f] = _resolve_value(resolutions, key, vb[f], va[f], vr[f])
        merged_nodes[nid] = Node(id=nid, created_at=nB.created_at, **merged_vals)

    # ---- 连线 ----
    merged_edges: dict[str, Edge] = {}
    dangling: list[dict[str, Any]] = []
    for eid in sorted(set(base.edges) | set(ga.edges) | set(gb.edges)):
        eB, eA, eR = base.edges.get(eid), ga.edges.get(eid), gb.edges.get(eid)

        # 双方新建同 id
        if eB is None and eA is not None and eR is not None:
            va, vr = _edge_values(eA), _edge_values(eR)
            key = f"edge:create:{eid}"
            if va == vr:
                auto.append(_auto("edge", eid, "create", "both_same",
                                  _edge_title(ga.edges, ga.nodes, eid)))
                chosen = eA
            else:
                conflicts.append(_conflict(
                    key, "edge", eid, "create_create",
                    _edge_title(ga.edges, ga.nodes, eid) or _edge_title(gb.edges, gb.nodes, eid),
                    base_v=None, a_v=va, b_v=vr, options=["a", "b", "drop"],
                    edge=eA, nodes=ga.nodes))
                choice = (resolutions or {}).get(key)
                chosen = eA if choice == "a" else (eR if choice == "b" else None)
            if chosen is not None:
                _put_edge(merged_edges, dangling, auto, eid, chosen, merged_nodes,
                          cascade_from="new")
            continue

        # 单侧新建
        if eB is None:
            chosen = eA if eA is not None else eR
            side = "a" if eA is not None else "b"
            auto.append(_auto("edge", eid, "create", side,
                              _edge_title(ga.edges if side == "a" else gb.edges,
                                          ga.nodes if side == "a" else gb.nodes, eid)))
            _put_edge(merged_edges, dangling, auto, eid, chosen, merged_nodes,
                      cascade_from="new")
            continue

        # 基线存在：双方都删除
        if eA is None and eR is None:
            auto.append(_auto("edge", eid, "delete", "both_same",
                              _edge_title(base.edges, base.nodes, eid)))
            continue

        # 删除与（另一侧的）修改相遇 → 连线实体级冲突；
        # 若另一侧与基线一致（没碰），则通常是单侧显式删除，或随该侧「节点删除与修改」
        # 冲突中被级联移除 —— 后者的去留跟随节点裁决（保留节点则保留边，选删除才删边）。
        if eA is None or eR is None:
            evb = _edge_values(eB)
            deleted_side = "a" if eA is None else "b"
            present = eR if eA is None else eA
            if present is None or _edge_values(present) == evb:
                # 检查消失一侧是否由「端点节点被删」导致，且该节点存在删除/修改冲突
                src = ga.nodes if deleted_side == "a" else gb.nodes
                endpoint_deleted = (eB.source_id not in src) or (eB.target_id not in src)
                endpoint_id = next((x for x in (eB.source_id, eB.target_id)
                                    if x not in src and x in base.nodes), None)
                node_kept = None
                if endpoint_deleted and endpoint_id is not None:
                    nc = node_choice(endpoint_id)
                    # 节点裁决：a/b 中「保留侧」= 未删节点的一侧；选删除侧才删边，
                    # base / 保留侧都应保留这条边（值取 present 或基线）
                    if nc is None:
                        node_kept = eB if present is None else present  # 预览默认保留
                    else:
                        deleted_is = "a" if endpoint_id not in ga.nodes else "b"
                        node_kept = None if nc == deleted_is else (present or eB)
                if node_kept is not None:
                    _put_edge(merged_edges, dangling, auto, eid, node_kept, merged_nodes)
                    continue
                auto.append(_auto("edge", eid, "delete", deleted_side,
                                  _edge_title(base.edges, base.nodes, eid)))
                continue
            key = f"edge:delete:{eid}"
            conflicts.append({
                **_conflict(key, "edge", eid, "delete_modify",
                           _edge_title(base.edges, base.nodes, eid),
                           base_v=evb,
                           a_v=None if eA is None else _edge_values(eA),
                           b_v=None if eR is None else _edge_values(eR),
                           edge=eB, nodes=base.nodes),
                "deleted_side": deleted_side,
                "kept_side": "b" if deleted_side == "a" else "a",
            })
            choice = (resolutions or {}).get(key, "base")
            if choice == "a":
                chosen = eA
            elif choice == "b":
                chosen = eR
            else:
                chosen = eB  # base：未裁决时保留基线连线
            if chosen is not None:
                _put_edge(merged_edges, dangling, auto, eid, chosen, merged_nodes)
            continue

        # 三方都在：逐字段（通常仅 kind / label 会不同）
        vb, va, vr = _edge_values(eB), _edge_values(eA), _edge_values(eR)
        merged_vals = dict(vb)
        a_changed = {f: va[f] for f in EDGE_FIELDS if va[f] != vb[f]}
        b_changed = {f: vr[f] for f in EDGE_FIELDS if vr[f] != vb[f]}
        for f in EDGE_FIELDS:
            in_a, in_b = f in a_changed, f in b_changed
            if not in_a and not in_b:
                continue
            if in_a and not in_b:
                merged_vals[f] = va[f]
                auto.append(_auto("edge", eid, "field_change", "a",
                                  _edge_title(base.edges, base.nodes, eid), f))
            elif in_b and not in_a:
                merged_vals[f] = vr[f]
                auto.append(_auto("edge", eid, "field_change", "b",
                                  _edge_title(base.edges, base.nodes, eid), f))
            elif va[f] == vr[f]:
                merged_vals[f] = va[f]
                auto.append(_auto("edge", eid, "field_change", "both_same",
                                  _edge_title(base.edges, base.nodes, eid), f))
            else:
                key = f"edge:field:{eid}:{f}"
                conflicts.append(_conflict(
                    key, "edge", eid, "value_diverge",
                    _edge_title(base.edges, base.nodes, eid),
                    field=f, field_label=EDGE_FIELD_LABELS.get(f, f),
                    base_v=vb[f], a_v=va[f], b_v=vr[f],
                    edge=eB, nodes=base.nodes))
                _, merged_vals[f] = _resolve_value(resolutions, key, vb[f], va[f], vr[f])
        _put_edge_values(merged_edges, dangling, auto, eid, merged_vals, eB.created_at, merged_nodes)

    conflicts.sort(key=lambda c: c["key"])
    auto.sort(key=lambda x: (x["subject"], x["id"], x.get("field") or ""))
    dangling.sort(key=lambda d: d["id"])
    return {
        "graph": Graph(base.version_id, merged_nodes, merged_edges),
        "conflicts": conflicts,
        "auto_resolved": auto,
        "dangling": dangling,
    }


def _put_edge(merged_edges, dangling, auto, eid: str, chosen: Edge,
              merged_nodes: dict[str, Node], *, cascade_from: str | None = None) -> None:
    _put_edge_values(merged_edges, dangling, auto, eid, _edge_values(chosen),
                     chosen.created_at, merged_nodes, cascade_from=cascade_from)


def _put_edge_values(merged_edges, dangling, auto, eid: str, vals: dict[str, Any],
                     created_at: str, merged_nodes: dict[str, Node],
                     *, cascade_from: str | None = None) -> None:
    """端点已被删除 / 丢弃的连线不进合并图。

    cascade_from='new'：连线本身也是单侧 / 双方新建，随端点（新建）的删除或丢弃而级联消失，
    记为自动采纳（与 apply_changes 的节点删除级联语义一致）；
    cascade_from='base'：连线在基线上存在，端点是基线节点却不在合并结果（仅可能来自裁决矛盾），
    登记 dangling，落库前作为冲突拦截。
    """
    missing = vals["source_id"] not in merged_nodes or vals["target_id"] not in merged_nodes
    if missing:
        if cascade_from == "new":
            auto.append({"subject": "edge", "id": eid, "action": "cascade_delete",
                         "side": "both_same", "title": None})
            return
        dangling.append({"id": eid, **vals})
        return
    merged_edges[eid] = Edge(eid, vals["source_id"], vals["target_id"],
                             vals["kind"], vals["label"], created_at)


# ---------- 把合并结果合成为新的 changeset（相对基线） ----------

def _utc(v: str) -> datetime:
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_merged_changeset(base: Graph, merged: Graph) -> Changeset:
    """把「基线 → 合并结果」的差异重新表达为一个标准 Changeset。

    生成的 changeset 再走一次 graph.apply_changes：因此服务端落库前必定重新执行
    成环检测、时间窗传播与全部字段 / 连线约束，与普通提交完全同一条校验路径。
    """
    node_creates: list[NodeCreate] = []
    node_updates: list[NodeUpdate] = []
    node_deletes: list[str] = []
    for nid, n in merged.nodes.items():
        old = base.nodes.get(nid)
        if old is None:
            node_creates.append(NodeCreate(
                id=nid, type=n.type, title=n.title, description=n.description,
                pos_x=n.pos_x, pos_y=n.pos_y,
                earliest_time=_utc(n.earliest_time) if n.earliest_time else None,
                latest_time=_utc(n.latest_time) if n.latest_time else None,
                reliability=n.reliability, source_group=n.source_group,
                trigger_mode=n.trigger_mode if n.type == "event" else None,
            ))
            continue
        patch: dict[str, Any] = {}
        ov, nv = _node_values(old), _node_values(n)
        for f in NODE_FIELDS:
            if ov[f] == nv[f]:
                continue
            patch[f] = _utc(nv[f]) if f in _TIME_FIELDS and nv[f] is not None else nv[f]
        if patch:
            node_updates.append(NodeUpdate(id=nid, **patch))
    for nid in base.nodes:
        if nid not in merged.nodes:
            node_deletes.append(nid)

    edge_creates: list[EdgeCreate] = []
    edge_updates: list[EdgeUpdate] = []
    edge_deletes: list[str] = []
    for eid, e in merged.edges.items():
        old = base.edges.get(eid)
        if old is None:
            edge_creates.append(EdgeCreate(
                id=eid, source_id=e.source_id, target_id=e.target_id,
                kind=e.kind, label=e.label))
            continue
        patch: dict[str, Any] = {}
        ov, nv = _edge_values(old), _edge_values(e)
        # source_id/target_id 对已存在连线不可变；正常裁决不会产生端点差异
        if ov["kind"] != nv["kind"]:
            patch["kind"] = nv["kind"]
        if ov["label"] != nv["label"]:
            patch["label"] = nv["label"]
        if patch:
            edge_updates.append(EdgeUpdate(id=eid, **patch))
    for eid in base.edges:
        if eid not in merged.edges:
            edge_deletes.append(eid)

    return Changeset(
        base_version_id=base.version_id,
        node_creates=node_creates, node_updates=node_updates, node_deletes=node_deletes,
        edge_creates=edge_creates, edge_updates=edge_updates, edge_deletes=edge_deletes,
    )


# ---------- 提案重放与三方预览 ----------

def _open_pair(conn: sqlite3.Connection, a_id: int, b_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    if a_id == b_id:
        raise ProposalError("请选择两份不同的提案", "same_proposal")
    row_a, row_b = _row_or_404(conn, a_id), _row_or_404(conn, b_id)
    for r in (row_a, row_b):
        if r["status"] != "open":
            raise ProposalError(
                f"提案 #{r['id']} 已合并（生成 v{r['merged_version_id']}），不能再次使用",
                "proposal_already_merged", 409)
    return row_a, row_b


def _replay_pair(conn: sqlite3.Connection, row_a: sqlite3.Row, row_b: sqlite3.Row
                 ) -> tuple[int, Graph, Graph, Graph]:
    if row_a["base_version_id"] != row_b["base_version_id"]:
        raise ProposalError(
            f"两份提案的基线不同（提案 #{row_a['id']} 基于 v{row_a['base_version_id']}，"
            f"提案 #{row_b['id']} 基于 v{row_b['base_version_id']}），无法三方合并",
            "different_base")
    base_id = row_a["base_version_id"]
    base = graph.load_graph(conn, base_id)
    if base is None:
        raise ProposalError(f"基准版本不存在：{base_id}", "version_not_found", 404)
    try:
        na, ea = graph.apply_changes(base, _changeset_from_row(row_a))
        nb, eb = graph.apply_changes(base, _changeset_from_row(row_b))
    except GraphError as exc:
        # 保存时已校验通过；基线不可变，理论上不会到达
        raise ProposalError(f"提案在基线上重放失败：{exc.message}", exc.code or "invalid")
    return base_id, base, Graph(base_id, na, ea), Graph(base_id, nb, eb)


def preview_merge(conn: sqlite3.Connection, body: MergePreviewRequest) -> dict[str, Any]:
    """只读三方比较：不写库、不生成版本。merged 为内存图，由路由层序列化。"""
    row_a, row_b = _open_pair(conn, body.proposal_a_id, body.proposal_b_id)
    base_id, base, ga, gb = _replay_pair(conn, row_a, row_b)
    result = compute_merge(base, ga, gb, resolutions=None)
    merged: Graph = result["graph"]
    cycle = graph.find_cycle(merged.nodes, merged.edges)
    _windows, time_conflict = graph.compute_time_windows(merged.nodes, merged.edges)
    return {
        "proposal_a": proposal_dict(row_a),
        "proposal_b": proposal_dict(row_b),
        "base_version_id": base_id,
        "current_version_id": graph.current_version_id(conn),
        "base_is_current": graph.current_version_id(conn) == base_id,
        "auto_resolved": result["auto_resolved"],
        "conflicts": result["conflicts"],
        "dangling": result["dangling"],
        "cycle": cycle,
        "time_conflict": time_conflict,
        "merged": merged,
    }


# ---------- 裁决落库（写事务：重算 + 状态复查 + 整图校验，全成或全不成） ----------

def commit_merge(conn: sqlite3.Connection, body: MergeCommitRequest) -> dict[str, Any]:
    row_a, row_b = _open_pair(conn, body.proposal_a_id, body.proposal_b_id)
    keys = [r.key for r in body.resolutions]
    if len(set(keys)) != len(keys):
        raise ProposalError("裁决清单中存在重复的冲突 key", "duplicate_resolution")
    choices = {r.key: r.choice for r in body.resolutions}
    message = body.message.strip()

    new_id: int | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 1) 事务内重新读取并复查：两份提案仍未使用
        cur_a = conn.execute("SELECT * FROM proposals WHERE id = ?", (row_a["id"],)).fetchone()
        cur_b = conn.execute("SELECT * FROM proposals WHERE id = ?", (row_b["id"],)).fetchone()
        if cur_a is None or cur_b is None:
            raise ProposalError("提案不存在", "proposal_not_found", 404)
        for r in (cur_a, cur_b):
            if r["status"] != "open":
                raise ProposalError(
                    f"提案 #{r['id']} 已被其他合并使用（生成 v{r['merged_version_id']}）",
                    "proposal_already_merged", 409)
        if cur_a["base_version_id"] != cur_b["base_version_id"]:
            raise ProposalError("两份提案的基线不同，无法合并", "different_base")
        base_id = cur_a["base_version_id"]

        # 2) 当前版本必须仍等于 base_version（基线过期则拒绝）
        current = graph.current_version_id(conn)
        if current != base_id:
            raise GraphError(
                f"提案基线已过期（提案基于 v{base_id}，当前为 v{current}），"
                "请刷新提案列表后基于当前版本重新保存提案",
                "stale_version",
            )
        base = graph.load_graph(conn, base_id)
        if base is None:
            raise ProposalError(f"基准版本不存在：{base_id}", "version_not_found", 404)

        # 3) 事务内重新做三方比较（不信任此前的预览结果），并核对裁决覆盖全部冲突
        _bid, _bg, ga, gb = _replay_pair(conn, cur_a, cur_b)
        result = compute_merge(base, ga, gb, resolutions=choices)
        if result["dangling"]:
            ids = ", ".join(d["id"] for d in result["dangling"])
            raise ProposalError(
                f"裁决互相矛盾：连线 {ids} 被保留，但其端点节点已被删除或丢弃；"
                "请把对应连线改判为删除侧，或保留其端点节点",
                "dangling_edge")
        conflicts = result["conflicts"]
        conflict_keys = {c["key"] for c in conflicts}
        unknown = set(choices) - conflict_keys
        if unknown:
            raise ProposalError(
                f"裁决包含已不存在的冲突项：{', '.join(sorted(unknown))}，请重新预览",
                "stale_conflict", 409)
        for c in conflicts:
            choice = choices.get(c["key"])
            if choice is None:
                raise ProposalError(f"尚有冲突未裁决：{conflict_brief(c)}", "unresolved_conflict")
            if choice not in c["options"]:
                raise ProposalError(
                    f"冲突 {c['key']} 不支持选项 {choice}"
                    f"（可选：{', '.join(c['options'])}）",
                    "invalid_choice")

        # 4) 合成新的 changeset 并复用整图校验（成环 / 时间窗 / 字段与连线约束）
        changes = build_merged_changeset(base, result["graph"])
        if _is_empty(changes):
            raise ProposalError("裁决结果相对基线没有任何修改，无需生成版本", "empty_merge")
        new_nodes, new_edges = graph.apply_changes(base, changes)

        # 5) 全部通过：生成一个 merge 版本，并在同一事务内标记两份提案已合并
        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'merge', ?)",
            (message, base_id),
        )
        new_id = cur.lastrowid
        graph.persist_graph(conn, new_id, new_nodes, new_edges)
        conn.execute(
            "UPDATE proposals SET status = 'merged', merged_version_id = ?, "
            "merged_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id IN (?, ?)",
            (new_id, cur_a["id"], cur_b["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    return {
        "version_id": new_id,
        "message": message,
        "created_at": row["created_at"],
        "merged_proposal_ids": [row_a["id"], row_b["id"]],
    }


def conflict_brief(c: dict[str, Any]) -> str:
    if c["subject"] == "node":
        label = f"节点「{c.get('title') or c['id']}」"
    else:
        label = f"连线「{c.get('title') or c['id']}」"
    if c["type"] == "value_diverge":
        return f"{label} 的 {c.get('field_label') or c.get('field')} 取值冲突"
    if c["type"] == "delete_modify":
        return f"{label} 删除与修改相遇"
    return f"{label} 双方新建内容不同"
