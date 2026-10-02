"""签发有效性复核（Issuance Revalidation）。

已签发的根因结论是在**签发时绑定版本**上按当时的 Schulze 最高并列、causes 祖先
关系、证据强度与有效时间窗逐项校验通过后写入的**不可变记录**；但因果图后续经过
归并、编辑或回滚会产生新版本，同一份签发结论在新版本上可能不再成立。

复核流程（create_revalidation，单个 SQLite 写事务 BEGIN IMMEDIATE）：
  1. 加载用户选择的**不可变签发记录**（root_cause_issuances 行）与其规范化 payload、
     签发时绑定版本（bound version）整图、目标版本（target version）整图、
     评审场 / 候选 / 冻结选票；
  2. 先做**评审候选 / 版本一致性**检查：payload 摘要、session_id、绑定版本、
     候选集合、封榜状态与 payload 声明一致（这些在正常流程中恒成立，属于防御性兜底）；
  3. 按**原签发规则**在目标整图上重新计算：
     a. Schulze 最高并列（由冻结选票现算）：签发根因集合须仍**恰好覆盖**最高并列；
     b. 根因之间不得存在 causes 祖先关系（附确定性见证路径）；
     c. 每个根因证据状态不得为 insufficient / refuted；
     d. 目标版本有效时间窗传播无冲突（附因果链见证）；
     e. 每个签发根因在目标版本中仍存在且仍为事件节点。
  任一不满足都在 `violations` 中按**稳定顺序逐项**列出代码、原因与见证；
  全部满足才标记 result='valid'。
  4. 无论有效与否，结果都写入**只追加**表 issuance_revalidations（触发器禁止
     UPDATE/DELETE），记录自带规范化报告与 SHA-256 摘要；同一签发 + 同一目标版本
     的重复复核返回 409 且**不覆盖**旧记录（UNIQUE 约束兜底并发）。

报告同时给出相对签发版的**节点字段、证据强度 / 证据连线与有效时间窗变化**，
以及目标版上重算的完整 Schulze 结果，供离线审计。复核只追加新表行，**不改写**
原图、评审、签发记录与任何历史快照。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app import conclusion, graph, issuance, review

FORMAT = "issuance-revalidation"
FORMAT_VERSION = 1
MAX_NOTE_LEN = 4000

# violations 的稳定排列次序（code 固定顺序；同 code 内按节点 / 见证排序）
_VIOLATION_ORDER = {
    "review_mismatch": 0,
    "root_cause_missing": 1,
    "missing_top_rank": 2,
    "extra_not_top_rank": 3,
    "candidate_ancestor_relation": 4,
    "evidence_not_admissible": 5,
    "time_window_conflict": 6,
}

# 参与「节点字段变化」对比的根因节点字段（位置 / 标题 / 描述 / 时间窗自报值 / 触发模式 / 类型）
_NODE_FIELDS = ("type", "title", "description", "pos_x", "pos_y",
                "earliest_time", "latest_time", "trigger_mode")
# 证据强度对比字段
_EVIDENCE_FIELDS = ("status", "support_score", "refute_score",
                    "selected_support_edges", "selected_refute_edges")
# 有效时间窗对比字段（time_window_dict 口径）
_WINDOW_FIELDS = ("earliest_time", "latest_time", "effective_lower", "effective_upper",
                  "lower_source_node_id", "upper_source_node_id",
                  "lower_chain", "upper_chain")


class RevalidationError(Exception):
    """复核业务错误：status_code 由路由层原样返回（重复复核为 409）。"""

    def __init__(self, message: str, code: str = "invalid", status_code: int = 400,
                 report: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.report = report


# ---------- 读取辅助 ----------

def _issuance_row(conn: sqlite3.Connection, issuance_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM root_cause_issuances WHERE id = ?", (issuance_id,)).fetchone()
    if row is None:
        raise RevalidationError(
            f"签发记录不存在：{issuance_id}", "issuance_not_found", 404)
    return row


def _existing_id(conn: sqlite3.Connection, issuance_id: int,
                 target_version_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM issuance_revalidations "
        "WHERE issuance_id = ? AND target_version_id = ?",
        (issuance_id, target_version_id),
    ).fetchone()
    return None if row is None else row["id"]


def _version_brief(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "message": row["message"], "kind": row["kind"],
            "base_id": row["base_id"], "created_at": row["created_at"]}


def _cause_edge_ids(g: graph.Graph, path: list[str]) -> list[str]:
    """目标图上见证节点链对应的 causes 边 id（按链顺序，确定性）。"""
    pair_edge: dict[tuple[str, str], str] = {}
    for e in g.edges.values():
        if e.kind == "causes":
            pair_edge[(e.source_id, e.target_id)] = e.id
    out: list[str] = []
    for a, b in zip(path, path[1:]):
        eid = pair_edge.get((a, b))
        if eid is not None:
            out.append(eid)
    return out


def _edge_brief(g: graph.Graph, e: graph.Edge) -> dict[str, Any]:
    src = g.nodes.get(e.source_id)
    dst = g.nodes.get(e.target_id)
    return {
        "edge_id": e.id, "source_id": e.source_id, "target_id": e.target_id,
        "kind": e.kind, "label": e.label,
        "source_title": src.title if src is not None else None,
        "target_title": dst.title if dst is not None else None,
    }


# ---------- 证据 / 时间窗快照 ----------

def _selected_edges(rv: dict[str, Any], direction: str) -> list[str]:
    return sorted(
        grp["selected"]["edge_id"]
        for grp in rv["groups"] if grp["direction"] == direction
    )


def _evidence_snapshot(rv: dict[str, Any] | None) -> dict[str, Any]:
    if rv is None:
        return {"status": "insufficient", "support_score": 0.0, "refute_score": 0.0,
                "selected_support_edges": [], "selected_refute_edges": []}
    return {
        "status": rv["status"],
        "support_score": rv["support_score"],
        "refute_score": rv["refute_score"],
        "selected_support_edges": _selected_edges(rv, "supports"),
        "selected_refute_edges": _selected_edges(rv, "refutes"),
    }


def _window_snapshot(nodes: dict[str, graph.Node],
                     windows: dict[str, dict[str, Any]], nid: str) -> dict[str, Any] | None:
    w = graph.time_window_dict(nodes, windows, nid)
    if w is None:
        return None
    return {
        "earliest_time": w["earliest_time"],
        "latest_time": w["latest_time"],
        "effective_lower": w["effective_lower"],
        "effective_upper": w["effective_upper"],
        "lower_source_node_id": w["lower_source"]["node_id"] if w["lower_source"] else None,
        "upper_source_node_id": w["upper_source"]["node_id"] if w["upper_source"] else None,
        "lower_chain": w["lower_chain"],
        "upper_chain": w["upper_chain"],
    }


def _changed_fields(bound: dict[str, Any], target: dict[str, Any],
                    fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """逐字段对比，返回 {field: {"bound": ..., "target": ...}}（仅含变化项）。"""
    out: dict[str, dict[str, Any]] = {}
    for f in fields:
        bv, tv = bound.get(f), target.get(f)
        if bv != tv:
            out[f] = {"bound": bv, "target": tv}
    return out


# ---------- 复核计算（读事务 / 写事务共用，纯计算） ----------

def evaluate_revalidation(
    conn: sqlite3.Connection,
    issuance_id: int,
    target_version_id: int,
    note: str,
) -> dict[str, Any]:
    """在调用方的（写）事务内加载签发记录 / 绑定版 / 目标版 / 冻结选票并按原规则复核。

    返回结构化报告（同时也是落库的不可变记录内容）；目标版本不存在抛 404。
    """
    evaluated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1) 不可变签发记录 + 规范化 payload（摘要重新计算，不信任落库值以外的任何输入）
    irow = _issuance_row(conn, issuance_id)
    bound_version_id = irow["version_id"]
    session_id = irow["session_id"]
    try:
        payload = json.loads(irow["payload_json"])
    except (json.JSONDecodeError, TypeError):
        payload = {}
    recomputed_digest = conclusion.sha256_hex(
        conclusion.canonical_json_bytes(payload)) if isinstance(payload, dict) else None

    srow = conn.execute(
        "SELECT * FROM review_sessions WHERE id = ?", (session_id,)).fetchone()
    bvrow = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (bound_version_id,)).fetchone()
    tvrow = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (target_version_id,)).fetchone()
    if tvrow is None:
        raise RevalidationError(
            f"目标版本不存在：v{target_version_id}", "target_version_not_found", 404)

    bound_g = graph.load_graph(conn, bound_version_id)
    target_g = graph.load_graph(conn, target_version_id)

    def _id_list(key: str) -> list[str]:
        """从 payload 安全提取按 id 排序的节点 id 列表（payload 被篡改时不抛异常）。"""
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            return []
        return sorted(c.get("node_id") for c in payload[key]
                      if isinstance(c, dict) and c.get("node_id"))

    meta = payload.get("meta") if isinstance(payload, dict) else None
    payload_meta = meta if isinstance(meta, dict) else {}
    payload_root_ids = _id_list("root_causes")
    payload_candidate_ids = _id_list("candidates")

    session_candidate_ids = (
        review._candidate_ids(conn, session_id) if srow is not None else [])
    rankings = issuance._ballot_rankings(conn, session_id) if srow is not None else []

    violations: list[dict[str, Any]] = []

    # 2) 评审候选 / 版本一致性（防御性：正常流程下全部恒成立）
    mismatch: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        mismatch.append({"reason": "payload_malformed",
                         "message": "签发记录 payload 不是合法对象，无法复核"})
    else:
        if recomputed_digest != irow["digest"]:
            mismatch.append({
                "reason": "payload_digest_mismatch",
                "message": "签发 payload 重新计算的 SHA-256 与记录摘要不一致",
                "digest_stored": irow["digest"], "digest_recomputed": recomputed_digest,
            })
        if payload_meta.get("session_id") != session_id:
            mismatch.append({
                "reason": "session_id_mismatch",
                "message": "payload.meta.session_id 与签发记录绑定的评审场不一致",
                "payload_session_id": payload_meta.get("session_id"),
                "record_session_id": session_id,
            })
        meta_vid = payload_meta.get("version_id")
        if meta_vid != bound_version_id or (srow is not None
                                            and srow["version_id"] != bound_version_id):
            mismatch.append({
                "reason": "bound_version_mismatch",
                "message": "payload / 评审场 / 签发记录三者绑定的版本不一致",
                "payload_version_id": meta_vid,
                "session_version_id": srow["version_id"] if srow is not None else None,
                "issuance_version_id": bound_version_id,
            })
        if srow is None:
            mismatch.append({
                "reason": "session_not_found",
                "message": f"签发记录绑定的评审场不存在：{session_id}",
            })
        elif srow["status"] != "closed":
            mismatch.append({
                "reason": "session_not_closed",
                "message": "签发记录绑定的评审场当前不是已封榜状态",
                "session_status": srow["status"],
            })
        if bound_g is None or bvrow is None:
            mismatch.append({
                "reason": "bound_version_not_found",
                "message": f"签发时绑定版本 v{bound_version_id} 的快照缺失，无法对照",
            })
        pset, sset = set(payload_candidate_ids), set(session_candidate_ids)
        if srow is not None and pset != sset:
            mismatch.append({
                "reason": "candidate_set_mismatch",
                "message": "签发 payload 中的候选集合与评审场登记的候选不一致",
                "payload_candidate_ids": payload_candidate_ids,
                "session_candidate_ids": session_candidate_ids,
                "missing_in_payload": sorted(sset - pset),
                "extra_in_payload": sorted(pset - sset),
            })
        payload_roots_in_candidates = all(rid in pset for rid in payload_root_ids)
        if payload_root_ids and not payload_roots_in_candidates:
            mismatch.append({
                "reason": "payload_root_causes_invalid",
                "message": "签发 payload 的根因成员不全在其候选集合中",
                "root_cause_node_ids": payload_root_ids,
                "candidate_node_ids": payload_candidate_ids,
            })
    for m in sorted(mismatch, key=lambda x: x["reason"]):
        violations.append({
            "code": "review_mismatch",
            "message": f"评审候选 / 版本不一致：{m['message']}",
            **{k: v for k, v in m.items() if k != "message"},
        })

    # 评审场候选即复核口径的候选集（评审场缺失时退化为 payload 候选）
    candidate_ids = session_candidate_ids or payload_candidate_ids
    candidate_set = set(candidate_ids)

    # 3a) 目标版上重算 Schulze（冻结选票现算，与签发 / 封榜计票同一纯函数）
    schulze_target = review.schulze_result(candidate_ids, rankings) if candidate_ids else None
    rank_of: dict[str, int | None] = {cid: None for cid in candidate_ids}
    top_rank: list[str] = []
    if schulze_target is not None and schulze_target["ranks"]:
        for layer in schulze_target["ranks"]:
            for cid in layer["node_ids"]:
                rank_of[cid] = layer["rank"]
        top_rank = list(schulze_target["ranks"][0]["node_ids"])
    top_set = set(top_rank)
    root_ids = list(payload_root_ids)
    root_set = set(root_ids)

    # 3b) 目标整图重算证据强度与有效时间窗
    t_windows, t_conflict = graph.compute_time_windows(target_g.nodes, target_g.edges)
    t_reviews = graph.compute_evidence_review(target_g.nodes, target_g.edges)
    t_adj, _ = issuance._cause_adjacency(target_g)
    bound_windows: dict[str, dict[str, Any]] = {}
    bound_reviews: dict[str, dict[str, Any]] = {}
    if bound_g is not None:
        bound_windows, _ = graph.compute_time_windows(bound_g.nodes, bound_g.edges)
        bound_reviews = graph.compute_evidence_review(bound_g.nodes, bound_g.edges)

    titles_bound = review._titles(conn, bound_version_id, candidate_ids) if bound_g else {}
    titles_target = {
        nid: target_g.nodes[nid].title
        for nid in candidate_ids if nid in target_g.nodes
    }

    # 条件 1：每个签发根因在目标版本中仍存在且仍为事件节点
    present_roots: list[str] = []
    for rid in root_ids:
        node = target_g.nodes.get(rid)
        if node is None:
            violations.append({
                "code": "root_cause_missing",
                "node_id": rid,
                "title_bound": titles_bound.get(rid, rid),
                "present_in_target": False,
                "message": f"根因「{titles_bound.get(rid, rid)}」在目标版本 v{target_version_id} "
                           "中已不存在（被删除或归并移除）",
            })
        elif node.type != "event":
            violations.append({
                "code": "root_cause_missing",
                "node_id": rid,
                "title_bound": titles_bound.get(rid, rid),
                "present_in_target": True,
                "target_type": node.type,
                "message": f"根因节点「{titles_bound.get(rid, rid)}」在目标版本 "
                           f"v{target_version_id} 中类型变为 {node.type}，不再是事件根因",
            })
        else:
            present_roots.append(rid)
    present_set = set(present_roots)

    # 条件 2：根因集合仍恰好覆盖目标版最高并列名次
    missing_top = sorted(top_set - root_set)
    extra_not_top = sorted(root_set - top_set)
    if missing_top:
        violations.append({
            "code": "missing_top_rank",
            "node_ids": missing_top,
            "message": "目标版本上重算的最高并列名次未被签发根因集合完整覆盖，缺少："
                       + "、".join(f"「{titles_target.get(x, titles_bound.get(x, x))}」"
                                   for x in missing_top),
        })
    if extra_not_top:
        violations.append({
            "code": "extra_not_top_rank",
            "node_ids": extra_not_top,
            "message": "签发根因在目标版本上已不在最高并列名次："
                       + "、".join(
                           f"「{titles_target.get(x, titles_bound.get(x, x))}」"
                           f"（第 {rank_of.get(x) or '?'} 名）" for x in extra_not_top),
        })

    # 条件 3：现存根因之间不得出现 causes 祖先关系（逐对，附确定性见证路径）
    ancestor_pairs: list[dict[str, Any]] = []
    for a in sorted(present_set):
        for b in sorted(issuance._descendants(t_adj, a, present_set)):
            path = issuance._witness_path(t_adj, a, b)
            ancestor_pairs.append({
                "code": "candidate_ancestor_relation",
                "ancestor_node_id": a,
                "descendant_node_id": b,
                "ancestor_title": target_g.nodes[a].title,
                "descendant_title": target_g.nodes[b].title,
                "witness_path": path,
                "witness_edge_ids": _cause_edge_ids(target_g, path),
                "message": f"目标版本上「{target_g.nodes[a].title}」成为"
                           f"「{target_g.nodes[b].title}」的 causes 祖先，"
                           "根因之间不得存在因果祖先关系",
            })
    ancestor_pairs.sort(key=lambda v: (v["ancestor_node_id"], v["descendant_node_id"]))
    violations.extend(ancestor_pairs)

    # 条件 4：每个现存根因证据状态仍须为 supported / disputed
    for rid in sorted(present_set):
        rv = t_reviews.get(rid)
        status = rv["status"] if rv is not None else "insufficient"
        if status not in issuance.EVIDENCE_OK_STATUSES:
            violations.append({
                "code": "evidence_not_admissible",
                "node_id": rid,
                "title": target_g.nodes[rid].title,
                "evidence_status": status,
                "evidence_status_label": issuance.EVIDENCE_STATUS_LABELS.get(status, status),
                "support_score": rv["support_score"] if rv is not None else 0.0,
                "refute_score": rv["refute_score"] if rv is not None else 0.0,
                "witness_evidence_node_ids": sorted(
                    grp["selected"]["node_id"]
                    for grp in (rv["groups"] if rv is not None else [])
                    if grp["direction"] == "refutes"),
                "message": f"目标版本上「{target_g.nodes[rid].title}」证据状态变为"
                           f"「{issuance.EVIDENCE_STATUS_LABELS.get(status, status)}」，"
                           "不再满足签发条件（仅允许已支持 / 存在争议）",
            })

    # 条件 5：目标整图有效时间窗传播无冲突（附因果链见证）
    if t_conflict is not None:
        violations.append({
            "code": "time_window_conflict",
            "message": graph._conflict_message(target_g.nodes, t_conflict),
            "conflict": t_conflict,
            "witness_path": t_conflict.get("chain_node_ids"),
            "witness_edge_ids": t_conflict.get("chain_edge_ids"),
        })

    violations.sort(key=lambda v: (_VIOLATION_ORDER.get(v["code"], 99),
                                   v.get("node_id") or v.get("ancestor_node_id") or "",
                                   v.get("reason") or ""))

    # 4) 相对签发版的节点 / 证据 / 时间窗变化（仅对两版都存在的根因）
    common_roots = sorted(rid for rid in root_set if rid in present_set
                          and bound_g is not None and rid in bound_g.nodes
                          and bound_g.nodes[rid].type == "event")
    node_changes: list[dict[str, Any]] = []
    evidence_changes: list[dict[str, Any]] = []
    window_changes: list[dict[str, Any]] = []
    for rid in common_roots:
        bn, tn = bound_g.nodes[rid], target_g.nodes[rid]
        b_fields = {f: getattr(bn, f) for f in _NODE_FIELDS}
        t_fields = {f: getattr(tn, f) for f in _NODE_FIELDS}
        field_diff = _changed_fields(b_fields, t_fields, _NODE_FIELDS)
        if field_diff:
            node_changes.append({"node_id": rid,
                                 "title_bound": bn.title, "title_target": tn.title,
                                 "changed_fields": field_diff})

        b_ev = _evidence_snapshot(bound_reviews.get(rid))
        t_ev = _evidence_snapshot(t_reviews.get(rid))
        ev_diff = _changed_fields(b_ev, t_ev, _EVIDENCE_FIELDS)
        if ev_diff:
            evidence_changes.append({"node_id": rid,
                                     "title_bound": bn.title, "title_target": tn.title,
                                     "bound": b_ev, "target": t_ev,
                                     "changed_fields": sorted(ev_diff)})

        b_win = _window_snapshot(bound_g.nodes, bound_windows, rid)
        t_win = _window_snapshot(target_g.nodes, t_windows, rid)
        if b_win is not None and t_win is not None:
            win_diff = _changed_fields(b_win, t_win, _WINDOW_FIELDS)
            if win_diff:
                window_changes.append({"node_id": rid,
                                       "title_bound": bn.title, "title_target": tn.title,
                                       "bound": b_win, "target": t_win,
                                       "changed_fields": sorted(win_diff)})

    # 根因相关 causes 边（两端都是根因）与指向根因的证据边：新增 / 消失清单
    bound_edges = _root_related_edges(bound_g, set(common_roots)) if bound_g else {}
    target_edges = _root_related_edges(target_g, set(common_roots))
    b_keys = set(bound_edges)
    t_keys = set(target_edges)
    edge_changes = {
        "added": [target_edges[k] for k in sorted(t_keys - b_keys)],
        "removed": [bound_edges[k] for k in sorted(b_keys - t_keys)],
    }

    # 5) 根因 / 候选明细
    root_rows = [_root_detail(rid, bound_g, target_g, bound_windows, t_windows,
                              bound_reviews, t_reviews, rank_of, top_set, titles_bound)
                 for rid in root_ids]
    candidate_rows = [
        _candidate_detail(cid, bound_g, target_g, bound_windows, t_windows,
                          bound_reviews, t_reviews, rank_of, top_set, titles_bound)
        for cid in candidate_ids
    ]

    checks = {
        "review_candidate_version_consistent": not any(
            v["code"] == "review_mismatch" for v in violations),
        "all_root_causes_present": not any(
            v["code"] == "root_cause_missing" for v in violations),
        "covers_top_rank_exactly": not missing_top and not extra_not_top,
        "no_candidate_ancestor_relation": not ancestor_pairs,
        "evidence_admissible": not any(
            v["code"] == "evidence_not_admissible" for v in violations),
        "time_window_ok": t_conflict is None,
    }
    valid = not violations

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "issuance_id": issuance_id,
        "session_id": session_id,
        "note": note,
        "evaluated_at": evaluated_at,
        "bound_version": _version_brief(bvrow),
        "target_version": _version_brief(tvrow),
        "bound_version_id": bound_version_id,
        "target_version_id": target_version_id,
        "issuance_digest": irow["digest"],
        "issued_at": irow["issued_at"],
        "root_cause_node_ids": root_ids,
        "top_rank_node_ids": sorted(top_rank),
        "result": "valid" if valid else "invalid",
        "valid": valid,
        "violation_count": len(violations),
        "violations": violations,
        "checks": checks,
        "root_causes": root_rows,
        "candidates": candidate_rows,
        "schulze_target": schulze_target,
        "changes": {
            "nodes": node_changes,
            "evidence": evidence_changes,
            "time_windows": window_changes,
            "root_cause_edges": edge_changes,
            "changed_node_count": len(node_changes),
            "changed_evidence_count": len(evidence_changes),
            "changed_time_window_count": len(window_changes),
        },
    }


def _root_related_edges(g: graph.Graph, root_set: set[str]) -> dict[tuple, dict[str, Any]]:
    """两端都是根因的 causes 边 + 指向根因的 supports/refutes 证据边，按内容元组去重。"""
    out: dict[tuple, dict[str, Any]] = {}
    for e in g.edges.values():
        related = (e.kind == "causes" and e.source_id in root_set and e.target_id in root_set)
        related = related or (e.kind in ("supports", "refutes") and e.target_id in root_set)
        if not related:
            continue
        key = (e.id, e.source_id, e.target_id, e.kind, e.label)
        out[key] = _edge_brief(g, e)
    return out


def _root_detail(rid, bound_g, target_g, b_windows, t_windows,
                 b_reviews, t_reviews, rank_of, top_set, titles_bound) -> dict[str, Any]:
    tnode = target_g.nodes.get(rid)
    bnode = bound_g.nodes.get(rid) if bound_g is not None else None
    present = tnode is not None and tnode.type == "event"
    bound_ev = _evidence_snapshot(b_reviews.get(rid)) if (
        bnode is not None and bnode.type == "event") else None
    target_ev = _evidence_snapshot(t_reviews.get(rid)) if present else None
    bound_win = _window_snapshot(bound_g.nodes, b_windows, rid) if (
        bound_g is not None and bnode is not None and bnode.type == "event") else None
    target_win = _window_snapshot(target_g.nodes, t_windows, rid) if present else None

    field_changes = None
    if bnode is not None and present:
        field_changes = _changed_fields(
            {f: getattr(bnode, f) for f in _NODE_FIELDS},
            {f: getattr(tnode, f) for f in _NODE_FIELDS},
            _NODE_FIELDS)
    return {
        "node_id": rid,
        "title_bound": bnode.title if bnode is not None else titles_bound.get(rid, rid),
        "present_in_target": tnode is not None,
        "is_event_in_target": tnode.type == "event" if tnode is not None else False,
        "title_target": tnode.title if tnode is not None else None,
        "target_type": tnode.type if tnode is not None else None,
        "rank_target": rank_of.get(rid),
        "is_top_rank_target": rid in top_set,
        "bound_evidence": bound_ev,
        "target_evidence": target_ev,
        "bound_time_window": bound_win,
        "target_time_window": target_win,
        "node_field_changes": field_changes,
    }


def _candidate_detail(cid, bound_g, target_g, b_windows, t_windows,
                      b_reviews, t_reviews, rank_of, top_set, titles_bound) -> dict[str, Any]:
    tnode = target_g.nodes.get(cid)
    bnode = bound_g.nodes.get(cid) if bound_g is not None else None
    present = tnode is not None and tnode.type == "event"
    rv = t_reviews.get(cid) if present else None
    return {
        "node_id": cid,
        "title_bound": bnode.title if bnode is not None else titles_bound.get(cid, cid),
        "present_in_target": tnode is not None,
        "is_event_in_target": tnode.type == "event" if tnode is not None else False,
        "title_target": tnode.title if tnode is not None else None,
        "rank_target": rank_of.get(cid),
        "is_top_rank_target": cid in top_set,
        "evidence_status_target": (rv["status"] if rv is not None else
                                   ("insufficient" if present else None)),
        "target_time_window": _window_snapshot(target_g.nodes, t_windows, cid) if present else None,
    }


# ---------- 写事务落库（只追加；重复不覆盖） ----------

def create_revalidation(
    conn: sqlite3.Connection,
    issuance_id: int,
    *,
    target_version_id: int,
    note: str | None = None,
) -> dict[str, Any]:
    note = (note or "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise RevalidationError(
            f"复核说明过长（>{MAX_NOTE_LEN} 字符）", "note_too_long")
    try:
        conn.execute("BEGIN IMMEDIATE")
        irow = _issuance_row(conn, issuance_id)

        dup_id = _existing_id(conn, issuance_id, target_version_id)
        if dup_id is not None:
            raise RevalidationError(
                f"签发记录 {issuance_id} 对目标版本 v{target_version_id} 已存在复核结果"
                f"（复核记录 #{dup_id}）；复核记录只追加，不得覆盖旧记录",
                "already_revalidated", 409,
                report={"existing_revalidation_id": dup_id})

        report = evaluate_revalidation(conn, issuance_id, target_version_id, note)
        report_text = conclusion.canonical_json_bytes(report).decode("utf-8")
        digest = conclusion.sha256_hex(report_text.encode("utf-8"))

        cur = conn.execute(
            "INSERT INTO issuance_revalidations "
            "(issuance_id, session_id, bound_version_id, target_version_id, note, "
            " result, violation_count, report_json, digest_algorithm, digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issuance_id, irow["session_id"], irow["version_id"], target_version_id,
             note, report["result"], report["violation_count"], report_text,
             conclusion.DIGEST_ALGORITHM, digest),
        )
        revalidation_id = cur.lastrowid
        conn.execute("COMMIT")
    except RevalidationError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        # UNIQUE(issuance_id, target_version_id) 在并发复核竞争时兜底：先到者已写入
        raise RevalidationError(
            f"签发记录 {issuance_id} 对目标版本 v{target_version_id} 已存在复核结果，"
            "复核记录只追加，不得覆盖旧记录",
            "already_revalidated", 409) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_revalidation(conn, revalidation_id)


# ---------- 查询 ----------

def revalidation_brief(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "issuance_id": row["issuance_id"],
        "session_id": row["session_id"],
        "bound_version_id": row["bound_version_id"],
        "target_version_id": row["target_version_id"],
        "note": row["note"],
        "result": row["result"],
        "valid": row["result"] == "valid",
        "violation_count": row["violation_count"],
        "digest_algorithm": row["digest_algorithm"],
        "digest": row["digest"],
        "created_at": row["created_at"],
    }


def revalidation_full(row: sqlite3.Row) -> dict[str, Any]:
    out = revalidation_brief(row)
    out["report"] = json.loads(row["report_json"])
    return out


def _row_or_404(conn: sqlite3.Connection, revalidation_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM issuance_revalidations WHERE id = ?", (revalidation_id,)).fetchone()
    if row is None:
        raise RevalidationError(
            f"复核记录不存在：{revalidation_id}", "revalidation_not_found", 404)
    return row


def get_revalidation(conn: sqlite3.Connection, revalidation_id: int) -> dict[str, Any]:
    return revalidation_full(_row_or_404(conn, revalidation_id))


def list_for_issuance(conn: sqlite3.Connection, issuance_id: int) -> dict[str, Any]:
    _issuance_row(conn, issuance_id)  # 签发不存在 → 404
    rows = conn.execute(
        "SELECT * FROM issuance_revalidations WHERE issuance_id = ? ORDER BY id",
        (issuance_id,),
    ).fetchall()
    return {
        "issuance_id": issuance_id,
        "revalidations": [revalidation_brief(r) for r in rows],
        "count": len(rows),
    }
