"""签发有效性复核（Root-Cause Issuance Validity Recheck）。

已签发的根因结论是在**签发时绑定版本**上按当时的 Schulze 名次、causes 祖先关系、
证据强度与有效时间窗逐项校验通过的；图在后续归并、编辑或回滚后会产生新版本，
原结论可能不再成立。本模块对一条**不可变签发记录**与用户指定的**目标版本**做复核：

- 在同一个 SQLite 写事务（BEGIN IMMEDIATE）中加载签发时绑定版本快照、签发 payload
  与目标版本整图，并由评审场冻结选票重新计算 Schulze 最高并列；
- 按签发原规则逐项复核：
    1. 签发记录与评审场绑定版本一致（版本不一致）；
    2. 签发的每个根因在目标版本中仍存在（缺失任一根因即失效）；
    3. 评审候选在目标版本中仍为事件节点（评审候选 / 版本不一致）；
    4. 签发集合仍恰好覆盖重算的 Schulze 最高并列名次；
    5. 根因之间在目标版本上不存在 causes 祖先关系（附确定性见证路径）；
    6. 每个根因在目标版本上的证据状态不得为 insufficient / refuted；
    7. 目标版本整图有效时间窗传播无冲突（附因果链见证）。
  任一不满足都在 violations 中按**稳定顺序逐项**列出代码、原因与见证。
- 同时计算相对签发版的**节点、证据与时间窗变化**（整图增删 + 每个根因的字段 /
  证据审查 / 有效时间窗差异）。

复核结果写入**不可修改**的只追加表 root_cause_issuance_rechecks
（触发器禁止 UPDATE/DELETE；UNIQUE(issuance_id, target_version_id) 兜底：
同一签发对同一目标版本的重复复核返回 409，绝不覆盖旧记录）。
原图、评审、签发记录与启动方式均不变：本模块只追加新表行。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app import conclusion, graph, review
from app.issuance import (
    EVIDENCE_OK_STATUSES,
    EVIDENCE_STATUS_LABELS,
    _cause_adjacency,
    _descendants,
    _witness_path,
)

FORMAT = "root-cause-issuance-recheck"
FORMAT_VERSION = 1
MAX_NOTE_LEN = 4000

# 根因事件节点参与逐项比对的字段（稳定顺序；证据专属字段不属于事件根因）
NODE_DIFF_FIELDS = (
    "title", "description", "pos_x", "pos_y",
    "earliest_time", "latest_time", "trigger_mode",
)

# 有效时间窗参与逐项比对的字段（来源取节点 id、链路取节点 id 列表）
WINDOW_DIFF_FIELDS = (
    "earliest_time", "latest_time", "effective_lower", "effective_upper",
    "lower_source_node_id", "upper_source_node_id", "lower_chain", "upper_chain",
)

# violations 输出的稳定顺序（同代码内再按节点 / 节点对排序）
VIOLATION_ORDER = {
    "issuance_bound_version_mismatch": 0,
    "root_missing_in_target": 1,
    "root_not_event_in_target": 2,
    "review_candidate_missing_in_target": 3,
    "review_candidate_not_event_in_target": 4,
    "schulze_top_rank_mismatch": 5,
    "candidate_ancestor_relation": 6,
    "evidence_not_admissible": 7,
    "time_window_conflict": 8,
}


class RecheckError(Exception):
    """复核业务错误：status_code 由路由层原样返回（重复复核等为 409）。"""

    def __init__(self, message: str, code: str = "invalid", status_code: int = 400,
                 report: dict[str, Any] | None = None, violations: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.report = report
        self.violations = violations


# ---------- 读取辅助 ----------

def _issuance_row(conn: sqlite3.Connection, issuance_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM root_cause_issuances WHERE id = ?", (issuance_id,)).fetchone()
    if row is None:
        raise RecheckError(f"签发记录不存在：{issuance_id}", "issuance_not_found", 404)
    return row


def _existing_row(conn: sqlite3.Connection, issuance_id: int,
                  target_version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM root_cause_issuance_rechecks "
        "WHERE issuance_id = ? AND target_version_id = ?",
        (issuance_id, target_version_id),
    ).fetchone()


def _ballot_rankings(conn: sqlite3.Connection, session_id: int) -> list[list[str]]:
    rows = conn.execute(
        "SELECT ranking FROM review_ballots WHERE session_id = ? ORDER BY reviewer",
        (session_id,),
    ).fetchall()
    return [json.loads(r["ranking"]) for r in rows]


def _sort_violations(violations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按固定代码顺序，再按 (node_id / ancestor, descendant) 稳定排序。"""
    def key(v: dict[str, Any]):
        return (
            VIOLATION_ORDER.get(v["code"], 100),
            v.get("node_id") or v.get("ancestor_node_id") or "",
            v.get("descendant_node_id") or "",
        )

    return sorted(violations, key=key)


# ---------- 变化比对（纯计算） ----------

def _node_brief(n: graph.Node) -> dict[str, Any]:
    return {"id": n.id, "type": n.type, "title": n.title}


def _edge_brief(e: graph.Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label}


def _selected_index(reviews: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """edge_id -> 入选证据（带方向 / 来源组），用于证据集合差异比对。"""
    out: dict[str, dict[str, Any]] = {}
    for g in reviews.get("groups", []):
        sel = g["selected"]
        out[sel["edge_id"]] = {
            "node_id": sel["node_id"], "edge_id": sel["edge_id"], "title": sel["title"],
            "reliability": sel["reliability"], "reliability_set": sel["reliability_set"],
            "source_group": sel["source_group"], "direction": g["direction"],
            "group": g["group"],
        }
    return out


def _unweighted_index(reviews: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """edge_id -> 同源未计权证据（带方向 / 来源组）。"""
    return {x["edge_id"]: {
        "node_id": x["node_id"], "edge_id": x["edge_id"], "title": x["title"],
        "reliability": x["reliability"], "reliability_set": x["reliability_set"],
        "source_group": x["source_group"], "direction": x["direction"], "group": x["group"],
    } for x in reviews.get("unweighted_evidence", [])}


def _set_diff(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]
              ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """以 edge_id 为身份，返回 (目标版新增, 目标版移除)，各自按 edge_id 排序。"""
    added = [new[k] for k in sorted(set(new) - set(old))]
    removed = [old[k] for k in sorted(set(old) - set(new))]
    return added, removed


def _node_field_changes(old: graph.Node, new: graph.Node) -> list[dict[str, Any]]:
    changes = []
    for field in NODE_DIFF_FIELDS:
        ov, nv = getattr(old, field), getattr(new, field)
        if ov != nv:
            changes.append({"field": field, "issued_value": ov, "target_value": nv})
    return changes


def _window_field_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    for field in WINDOW_DIFF_FIELDS:
        ov, nv = old.get(field), new.get(field)
        if ov != nv:
            changes.append({"field": field, "issued_value": ov, "target_value": nv})
    return changes


def _compute_changes(
    *,
    g_issued: graph.Graph,
    g_target: graph.Graph,
    root_ids: list[str],
    issued_windows: dict[str, dict[str, Any]],
    target_windows: dict[str, dict[str, Any]],
    issued_reviews: dict[str, dict[str, Any]],
    target_reviews: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """相对签发版的整图增删与每个根因的节点字段 / 证据 / 时间窗差异。"""
    issued_ids, target_ids = set(g_issued.nodes), set(g_target.nodes)
    edge_issued, edge_target = set(g_issued.edges), set(g_target.edges)

    node_diffs: dict[str, Any] = {}
    evidence_diffs: dict[str, Any] = {}
    window_diffs: dict[str, Any] = {}
    for rid in root_ids:
        on, tn = g_issued.nodes.get(rid), g_target.nodes.get(rid)
        if on is None:  # 签发版必然存在（payload 来自该版本），防御性处理
            continue
        if tn is None:
            node_diffs[rid] = {"present": False, "target_title": None, "field_changes": []}
            evidence_diffs[rid] = {"present": False}
            window_diffs[rid] = {"present": False, "field_changes": []}
            continue
        node_diffs[rid] = {
            "present": True,
            "issued_title": on.title,
            "target_title": tn.title,
            "field_changes": _node_field_changes(on, tn),
        }
        window_diffs[rid] = {
            "present": True,
            "field_changes": _window_field_changes(issued_windows[rid], target_windows[rid]),
        }
        if on.type != "event" or tn.type != "event":
            evidence_diffs[rid] = {"present": True, "is_event": False}
            continue
        oi, ti = issued_reviews.get(rid), target_reviews.get(rid)
        oi = oi or {"support_score": 0.0, "refute_score": 0.0, "status": "insufficient",
                    "groups": [], "selected_evidence": [], "unweighted_evidence": []}
        ti = ti or {"support_score": 0.0, "refute_score": 0.0, "status": "insufficient",
                    "groups": [], "selected_evidence": [], "unweighted_evidence": []}
        sel_added, sel_removed = _set_diff(_selected_index(oi), _selected_index(ti))
        unw_added, unw_removed = _set_diff(_unweighted_index(oi), _unweighted_index(ti))
        score_changes = []
        for field in ("support_score", "refute_score"):
            if oi.get(field) != ti.get(field):
                score_changes.append({"field": field,
                                      "issued_value": oi.get(field), "target_value": ti.get(field)})
        evidence_diffs[rid] = {
            "present": True,
            "is_event": True,
            "status_change": (
                {"issued": oi["status"],
                 "issued_label": EVIDENCE_STATUS_LABELS.get(oi["status"], oi["status"]),
                 "target": ti["status"],
                 "target_label": EVIDENCE_STATUS_LABELS.get(ti["status"], ti["status"])}
                if oi["status"] != ti["status"] else None
            ),
            "score_changes": score_changes,
            "selected_added": sel_added,
            "selected_removed": sel_removed,
            "unweighted_added": unw_added,
            "unweighted_removed": unw_removed,
        }

    return {
        "graph": {
            "issued": {"node_count": len(g_issued.nodes), "edge_count": len(g_issued.edges)},
            "target": {"node_count": len(g_target.nodes), "edge_count": len(g_target.edges)},
        },
        "nodes_added": [_node_brief(g_target.nodes[nid])
                        for nid in sorted(target_ids - issued_ids)],
        "nodes_removed": [_node_brief(g_issued.nodes[nid])
                          for nid in sorted(issued_ids - target_ids)],
        "edges_added": [_edge_brief(g_target.edges[eid])
                        for eid in sorted(edge_target - edge_issued)],
        "edges_removed": [_edge_brief(g_issued.edges[eid])
                          for eid in sorted(edge_issued - edge_target)],
        "root_nodes": node_diffs,
        "root_evidence": evidence_diffs,
        "root_time_windows": window_diffs,
    }


# ---------- 逐项复核（写事务内调用，纯计算 + 只读图载入） ----------

def evaluate_recheck(
    conn: sqlite3.Connection,
    issuance_id: int,
    target_version_id: int,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """加载签发绑定版本、签发 payload 与目标整图，按原规则逐项复核并计算变化。"""
    irow = _issuance_row(conn, issuance_id)
    payload = json.loads(irow["payload_json"])
    issued_version_id = irow["version_id"]
    session_id = irow["session_id"]

    if conn.execute("SELECT 1 FROM versions WHERE id = ?", (target_version_id,)).fetchone() is None:
        raise RecheckError(f"目标版本不存在：v{target_version_id}", "target_version_not_found", 404)
    srow = conn.execute(
        "SELECT * FROM review_sessions WHERE id = ?", (session_id,)).fetchone()
    if srow is None:  # 外键正常时不会发生，防御性处理
        raise RecheckError(f"签发关联的评审场不存在：{session_id}", "session_not_found", 404)

    g_issued = graph.load_graph(conn, issued_version_id)
    g_target = graph.load_graph(conn, target_version_id)
    if g_issued is None:
        raise RecheckError(f"签发绑定版本不存在：v{issued_version_id}",
                           "issued_version_not_found", 404)

    # 签发 payload 中的最终根因（按 id 排序，稳定输出）
    root_ids = sorted(c["node_id"] for c in payload.get("root_causes", []))
    root_titles = {c["node_id"]: c.get("title", c["node_id"])
                   for c in payload.get("root_causes", [])}
    candidate_info = {c["node_id"]: c for c in payload.get("candidates", [])}

    candidate_ids = review._candidate_ids(conn, session_id)
    rankings = _ballot_rankings(conn, session_id)

    # 评审候选在目标整图中仍存在且仍为事件的，才参与重新计票（缺失 / 改类别的候选
    # 另有「评审候选 / 版本不一致」失效项）；选票中被剔除候选不影响存活候选间的相对
    # 偏好（两两偏好只看两者在选票中的先后），因此直接用冻结选票重算即可。
    voting_candidate_ids = sorted(
        cid for cid in candidate_ids
        if cid in g_target.nodes and g_target.nodes[cid].type == "event")

    # 由冻结选票在「目标版本仍在的候选」上重新计算 Schulze 最高并列（与签发同一口径）
    schulze = review.schulze_result(voting_candidate_ids, rankings) if rankings and voting_candidate_ids else None
    top_rank = list(schulze["ranks"][0]["node_ids"]) if schulze and schulze["ranks"] else []
    top_set = set(top_rank)
    root_set = set(root_ids)

    # 目标整图上的证据强度、有效时间窗与 causes 邻接（均按原规则现算）
    target_windows, target_conflict = graph.compute_time_windows(g_target.nodes, g_target.edges)
    target_reviews = graph.compute_evidence_review(g_target.nodes, g_target.edges)
    adj, _events = _cause_adjacency(g_target)

    # 签发绑定版本上同样重算一份，供「相对签发版变化」比对
    issued_windows, _iwc = graph.compute_time_windows(g_issued.nodes, g_issued.edges)
    issued_reviews = graph.compute_evidence_review(g_issued.nodes, g_issued.edges)

    violations: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}

    # 条件 1：签发记录、payload 与评审场的绑定版本必须一致
    payload_version = (payload.get("meta") or {}).get("version_id")
    bound_ok = (issued_version_id == srow["version_id"]
                and payload_version == issued_version_id)
    checks["issuance_bound_version_consistent"] = bound_ok
    if not bound_ok:
        violations.append({
            "code": "issuance_bound_version_mismatch",
            "message": f"签发记录绑定 v{issued_version_id}、payload 记录 v{payload_version}、"
                       f"评审场绑定 v{srow['version_id']}，三者不一致，无法复核",
            "issuance_version_id": issued_version_id,
            "payload_version_id": payload_version,
            "session_version_id": srow["version_id"],
        })

    # 条件 2：每个签发根因在目标版本中仍存在且仍为事件
    missing_roots = sorted(rid for rid in root_ids if rid not in g_target.nodes)
    checks["roots_present_in_target"] = not missing_roots
    for rid in missing_roots:
        violations.append({
            "code": "root_missing_in_target",
            "node_id": rid,
            "title": root_titles.get(rid, rid),
            "witness_path": [rid],
            "message": f"根因「{root_titles.get(rid, rid)}」在目标版本 v{target_version_id} "
                       "中已不存在（可能被删除或归并）",
        })
    not_event_roots = sorted(
        rid for rid in root_ids
        if rid in g_target.nodes and g_target.nodes[rid].type != "event")
    for rid in not_event_roots:
        violations.append({
            "code": "root_not_event_in_target",
            "node_id": rid,
            "title": root_titles.get(rid, rid),
            "target_type": g_target.nodes[rid].type,
            "witness_path": [rid],
            "message": f"根因「{root_titles.get(rid, rid)}」在目标版本中不再是事件节点",
        })

    # 条件 3：评审候选与目标版本一致（仍存在且仍为事件）
    cand_missing = sorted(cid for cid in candidate_ids if cid not in g_target.nodes)
    cand_not_event = sorted(
        cid for cid in candidate_ids
        if cid in g_target.nodes and g_target.nodes[cid].type != "event")
    checks["review_candidates_consistent"] = not cand_missing and not cand_not_event
    for cid in cand_missing:
        title = candidate_info.get(cid, {}).get("title", cid)
        violations.append({
            "code": "review_candidate_missing_in_target",
            "node_id": cid,
            "title": title,
            "witness_path": [cid],
            "message": f"评审候选「{title}」在目标版本 v{target_version_id} 中已不存在，"
                       "评审候选与版本不一致",
        })
    for cid in cand_not_event:
        title = candidate_info.get(cid, {}).get("title", cid)
        violations.append({
            "code": "review_candidate_not_event_in_target",
            "node_id": cid,
            "title": title,
            "target_type": g_target.nodes[cid].type,
            "witness_path": [cid],
            "message": f"评审候选「{title}」在目标版本中不再是事件节点，评审候选与版本不一致",
        })

    # 条件 4：签发集合仍恰好覆盖重算的 Schulze 最高并列名次
    missing_top = sorted(top_set - root_set)
    extra_top = sorted(root_set - top_set)
    covers_ok = not missing_top and not extra_top
    checks["covers_top_rank_exactly"] = covers_ok
    if not covers_ok:
        violations.append({
            "code": "schulze_top_rank_mismatch",
            "message": "按冻结选票重算 Schulze 最高并列名次后，签发集合不再恰好覆盖最高并列名次",
            "top_rank_node_ids": sorted(top_set),
            "issued_node_ids": root_ids,
            "missing_node_ids": missing_top,
            "extra_node_ids": extra_top,
            "witness_path": sorted(top_set | root_set),
        })

    # 条件 5：目标版本上根因之间不存在 causes 祖先关系（逐对，附确定性见证路径）
    present_event_roots = {rid for rid in root_ids
                           if rid in g_target.nodes and g_target.nodes[rid].type == "event"}
    ancestor_pairs: list[dict[str, Any]] = []
    for a in sorted(present_event_roots):
        for b in sorted(_descendants(adj, a, present_event_roots)):
            ancestor_pairs.append({
                "code": "candidate_ancestor_relation",
                "ancestor_node_id": a,
                "descendant_node_id": b,
                "ancestor_title": g_target.nodes[a].title,
                "descendant_title": g_target.nodes[b].title,
                "witness_path": _witness_path(adj, a, b),
                "message": f"目标版本中「{g_target.nodes[a].title}」成为"
                           f"「{g_target.nodes[b].title}」的 causes 祖先，根因之间不得存在因果祖先关系",
            })
    checks["no_candidate_ancestor_relation"] = not ancestor_pairs
    violations.extend(ancestor_pairs)

    # 条件 6：每个现存根因证据状态不得为 insufficient / refuted
    bad_evidence: list[dict[str, Any]] = []
    for rid in sorted(present_event_roots):
        rv = target_reviews.get(rid)
        status = rv["status"] if rv is not None else "insufficient"
        if status not in EVIDENCE_OK_STATUSES:
            bad_evidence.append({
                "code": "evidence_not_admissible",
                "node_id": rid,
                "title": g_target.nodes[rid].title,
                "evidence_status": status,
                "evidence_status_label": EVIDENCE_STATUS_LABELS.get(status, status),
                "support_score": rv["support_score"] if rv is not None else 0.0,
                "refute_score": rv["refute_score"] if rv is not None else 0.0,
                "witness_path": [rid],
                "message": f"根因「{g_target.nodes[rid].title}」在目标版本中的证据状态变为"
                           f"「{EVIDENCE_STATUS_LABELS.get(status, status)}」，不再满足签发条件",
            })
    checks["evidence_admissible"] = not bad_evidence
    violations.extend(bad_evidence)

    # 条件 7：目标版本整图有效时间窗传播无冲突（附因果链见证）
    time_ok = target_conflict is None
    checks["time_window_ok"] = time_ok
    if target_conflict is not None:
        violations.append({
            "code": "time_window_conflict",
            "message": graph._conflict_message(g_target.nodes, target_conflict),
            "conflict": target_conflict,
            "witness_path": target_conflict.get("chain_node_ids", []),
            "witness_edge_ids": target_conflict.get("chain_edge_ids", []),
        })

    violations = _sort_violations(violations)

    # 候选 / 根因明细行（签发版信息取自 payload，目标版信息取自整图）
    rank_of: dict[str, int | None] = {cid: None for cid in candidate_ids}
    if schulze is not None:
        for layer in schulze["ranks"]:
            for cid in layer["node_ids"]:
                rank_of[cid] = layer["rank"]
    candidate_rows = []
    for cid in candidate_ids:
        tn = g_target.nodes.get(cid)
        candidate_rows.append({
            "node_id": cid,
            "issued_title": candidate_info.get(cid, {}).get("title", cid),
            "issued_rank": rank_of.get(cid),
            "is_top_rank": cid in top_set,
            "is_root_cause": cid in root_set,
            "present_in_target": tn is not None,
            "target_type": tn.type if tn is not None else None,
            "target_title": tn.title if tn is not None else None,
        })

    changes = _compute_changes(
        g_issued=g_issued, g_target=g_target, root_ids=root_ids,
        issued_windows=issued_windows, target_windows=target_windows,
        issued_reviews=issued_reviews, target_reviews=target_reviews,
    )

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "issuance_id": issuance_id,
        "session_id": session_id,
        "session_title": srow["title"],
        "issued_version_id": issued_version_id,
        "target_version_id": target_version_id,
        "note": (note or "").strip(),
        "root_cause_node_ids": root_ids,
        "top_rank_node_ids": sorted(top_set),
        "has_schulze_result": schulze is not None,
        "ballot_count": len(rankings),
        "checks": checks,
        "valid": not violations,
        "violation_count": len(violations),
        "violations": violations,
        "candidates": candidate_rows,
        "changes": changes,
    }


# ---------- 写入只追加复核记录（单写事务，不覆盖旧记录） ----------

def create_recheck(
    conn: sqlite3.Connection,
    issuance_id: int,
    *,
    target_version_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise RecheckError(f"复核说明过长（>{MAX_NOTE_LEN} 字符）", "note_too_long", 400)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _issuance_row(conn, issuance_id)  # 不存在直接 404
        # 同一签发 + 同一目标版本已复核过：拒绝，绝不覆盖旧的不可变记录
        existed = _existing_row(conn, issuance_id, target_version_id)
        if existed is not None:
            raise RecheckError(
                f"签发记录 {issuance_id} 对目标版本 v{target_version_id} 已存在复核记录"
                f"（#{existed['id']}，{existed['status']}），重复复核不得覆盖旧记录",
                "recheck_already_exists", 409)

        report = evaluate_recheck(conn, issuance_id, target_version_id, note=note)
        checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report["checked_at"] = checked_at
        report_text = conclusion.canonical_json_bytes(report).decode("utf-8")
        digest = conclusion.sha256_hex(report_text.encode("utf-8"))
        status = "valid" if report["valid"] else "invalid"

        cur = conn.execute(
            "INSERT INTO root_cause_issuance_rechecks "
            "(issuance_id, target_version_id, status, note, report_json, "
            "digest_algorithm, digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (issuance_id, target_version_id, status, note, report_text,
             conclusion.DIGEST_ALGORITHM, digest),
        )
        recheck_id = cur.lastrowid
        conn.execute("COMMIT")
    except RecheckError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        # UNIQUE(issuance_id, target_version_id) 在并发复核竞争时兜底：先到者已写入
        existed = _existing_row(conn, issuance_id, target_version_id)
        rid = existed["id"] if existed is not None else None
        raise RecheckError(
            f"签发记录 {issuance_id} 对目标版本 v{target_version_id} 已存在复核记录"
            + (f"（#{rid}）" if rid is not None else "")
            + "，重复复核不得覆盖旧记录",
            "recheck_already_exists", 409) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_recheck(conn, issuance_id, recheck_id)


# ---------- 查询 ----------

def recheck_brief(row: sqlite3.Row) -> dict[str, Any]:
    report = json.loads(row["report_json"])
    return {
        "id": row["id"],
        "issuance_id": row["issuance_id"],
        "target_version_id": row["target_version_id"],
        "status": row["status"],
        "valid": row["status"] == "valid",
        "note": row["note"],
        "checked_at": row["checked_at"],
        "digest_algorithm": row["digest_algorithm"],
        "digest": row["digest"],
        "violation_count": report.get("violation_count", len(report.get("violations", []))),
        "root_cause_node_ids": report.get("root_cause_node_ids", []),
    }


def recheck_full(row: sqlite3.Row) -> dict[str, Any]:
    out = recheck_brief(row)
    out["report"] = json.loads(row["report_json"])
    return out


def _row_or_404(conn: sqlite3.Connection, issuance_id: int, recheck_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM root_cause_issuance_rechecks WHERE id = ?", (recheck_id,)).fetchone()
    if row is None or row["issuance_id"] != issuance_id:
        raise RecheckError(
            f"签发记录 {issuance_id} 下不存在复核记录：{recheck_id}",
            "recheck_not_found", 404)
    return row


def get_recheck(conn: sqlite3.Connection, issuance_id: int, recheck_id: int) -> dict[str, Any]:
    _issuance_row(conn, issuance_id)
    return recheck_full(_row_or_404(conn, issuance_id, recheck_id))


def list_rechecks(conn: sqlite3.Connection, issuance_id: int) -> dict[str, Any]:
    _issuance_row(conn, issuance_id)
    rows = conn.execute(
        "SELECT * FROM root_cause_issuance_rechecks WHERE issuance_id = ? ORDER BY id DESC",
        (issuance_id,),
    ).fetchall()
    return {"issuance_id": issuance_id, "rechecks": [recheck_brief(r) for r in rows]}
