"""根因结论签发（Root-Cause Conclusion Issuance）。

从一个**已封榜**的根因共识盲评场签发不可变的最终根因集合：

- 签发（issue_issuance）在同一个 SQLite 写事务（BEGIN IMMEDIATE）中重新读取
  评审绑定版本（nodes/edges 快照）、Schulze 名次（由冻结选票现算）、证据审查与
  有效时间窗，逐项校验：
    1. 评审场已封榜（closed）；
    2. 同场尚未签发过（同场不可重复签发）；
    3. 提交集合非空、不重复、成员都是本场候选；
    4. 集合**恰好覆盖**最高并列名次（top-rank）：不缺、不多；
    5. 候选之间不存在 causes 祖先关系（任意一对提交候选，一个不是另一个的祖先）；
    6. 每个候选的证据状态不得为 insufficient 或 refuted
       （即仅允许 supported / disputed）；
    7. 绑定版本快照在有效时间窗传播上无冲突（历史快照的重算兜底）。
  任一条件不满足都拒绝（400/409），并在 `violations` 中**逐项**说明原因。
- 全部通过后写入**不可修改**的签发记录（root_cause_issuances，只追加触发器
  禁止 UPDATE/DELETE），保存规范化结论 payload 与 SHA-256 摘要，并关联评审场
  与版本；同场唯一约束兜底重复签发。

签发只读上面的版本 / 评审数据并追加一张新表，**不改写**图、评审票或历史快照；
记录自带规范化 payload 与摘要，后续图版本变化不影响记录。

validate 接口为纯只读预检：复用同一套逐项校验（可指定任意候选集合），
返回结构化检查结果供前端展示，不写库。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app import conclusion, graph, review

FORMAT = "root-cause-issuance"
FORMAT_VERSION = 1
MAX_NOTE_LEN = 4000

# 允许签发的证据状态：insufficient / refuted 一律拒绝
EVIDENCE_OK_STATUSES = ("supported", "disputed")
EVIDENCE_STATUS_LABELS = {
    "supported": "已支持",
    "disputed": "存在争议",
    "refuted": "已反驳",
    "insufficient": "证据不足",
}


class IssuanceError(Exception):
    """签发业务错误：status_code 由路由层原样返回（重复签发等为 409）。"""

    def __init__(self, message: str, code: str = "invalid", status_code: int = 400,
                 report: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.report = report


# ---------- 读取辅助 ----------

def _session_row(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM review_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise IssuanceError(f"评审场不存在：{session_id}", "session_not_found", 404)
    return row


def _ballot_rankings(conn: sqlite3.Connection, session_id: int) -> list[list[str]]:
    rows = conn.execute(
        "SELECT ranking FROM review_ballots WHERE session_id = ? ORDER BY reviewer",
        (session_id,),
    ).fetchall()
    return [json.loads(r["ranking"]) for r in rows]


def _issuance_row(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM root_cause_issuances WHERE session_id = ?", (session_id,),
    ).fetchone()


def _cause_adjacency(g: graph.Graph) -> tuple[dict[str, list[str]], set[str]]:
    """事件 causes 子图的（正向邻接, 事件集合）；邻接表按节点 id 升序，保证确定性。"""
    events = {nid for nid, n in g.nodes.items() if n.type == "event"}
    adj: dict[str, list[str]] = {nid: [] for nid in events}
    for e in g.edges.values():
        if e.kind == "causes" and e.source_id in events and e.target_id in events:
            adj[e.source_id].append(e.target_id)
    for u in adj:
        adj[u].sort()
    return adj, events


def _descendants(adj: dict[str, list[str]], start: str, within: set[str]) -> set[str]:
    """start 沿 causes 边可到达的、且在 within 内的节点（不含 start 自身）。"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        u = stack.pop()
        for v in adj.get(u, []):
            if v in within and v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def _witness_path(adj: dict[str, list[str]], ancestor: str, descendant: str) -> list[str]:
    """确定性地求 ancestor → … → descendant 的一条 causes 路径（按 id 升序 DFS）。"""
    path = [ancestor]
    visited = {ancestor}

    def dfs(u: str) -> bool:
        if u == descendant:
            return True
        for v in adj.get(u, []):
            if v in visited:
                continue
            visited.add(v)
            path.append(v)
            if dfs(v):
                return True
            path.pop()
        return False

    dfs(ancestor)
    return list(path)


# ---------- 逐项校验（读事务 / 写事务共用，纯计算） ----------

def evaluate(
    conn: sqlite3.Connection,
    session_id: int,
    node_ids: list[str] | None,
    *,
    note: str | None = None,
    for_issue: bool = False,
) -> dict[str, Any]:
    """重新读取评审绑定版本、Schulze 名次、证据审查与有效时间窗，逐项校验签发条件。

    node_ids 为用户提交的最终根因集合；为 None 时默认取最高并列名次（预检默认值）。
    返回结构化报告：checks（各条件是否通过）、violations（逐项失败原因）、
    candidates（每个候选的名次 / 证据状态 / 有效时间窗）。
    """
    srow = _session_row(conn, session_id)
    version_id = srow["version_id"]
    g = graph.load_graph(conn, version_id)
    if g is None:  # 外键正常时不会发生，防御性处理
        raise IssuanceError(f"评审场绑定的版本不存在：v{version_id}", "version_not_found", 404)

    candidate_ids = review._candidate_ids(conn, session_id)  # 已按 id 排序
    candidate_set = set(candidate_ids)
    titles = review._titles(conn, version_id, candidate_ids)
    rankings = _ballot_rankings(conn, session_id)
    closed = srow["status"] == "closed"

    # Schulze 名次（由冻结 / 当前选票现算；预检在开放场也能给出当前名次参考）
    schulze = review.schulze_result(candidate_ids, rankings) if rankings else None
    rank_of: dict[str, int | None] = {cid: None for cid in candidate_ids}
    top_rank: list[str] = []
    if schulze is not None:
        for layer in schulze["ranks"]:
            for cid in layer["node_ids"]:
                rank_of[cid] = layer["rank"]
        top_rank = list(schulze["ranks"][0]["node_ids"]) if schulze["ranks"] else []
    top_set = set(top_rank)

    submitted = list(top_rank) if node_ids is None else list(node_ids)
    submitted_set = set(submitted)

    # 有效时间窗（候选在绑定版本上重算）与证据审查（整版本重算，口径与 /api/state 一致）
    windows, win_conflict = graph.compute_time_windows(g.nodes, g.edges)
    reviews = graph.compute_evidence_review(g.nodes, g.edges)
    adj, _events = _cause_adjacency(g)

    # 每个候选的明细行
    candidate_rows: list[dict[str, Any]] = []
    for cid in candidate_ids:
        rv = reviews.get(cid)
        status = rv["status"] if rv is not None else "insufficient"
        w = windows.get(cid)
        candidate_rows.append({
            "node_id": cid,
            "title": titles.get(cid, cid),
            "rank": rank_of.get(cid),
            "is_top_rank": cid in top_set,
            "in_submitted_set": cid in submitted_set,
            "evidence_status": status,
            "evidence_status_label": EVIDENCE_STATUS_LABELS.get(status, status),
            "support_score": rv["support_score"] if rv is not None else 0.0,
            "refute_score": rv["refute_score"] if rv is not None else 0.0,
            "time_window": graph.time_window_dict(g.nodes, windows, cid),
        })

    violations: list[dict[str, Any]] = []

    # 条件 0：评审场必须已封榜
    session_open = not closed
    if session_open:
        violations.append({
            "code": "session_not_closed",
            "message": "评审场尚未封榜，只有封榜并冻结选票后才能签发最终根因集合",
        })

    # 条件 0b：签发说明非空（仅签发时强制；预检允许空以便即时校验）
    note = (note or "").strip()
    note_empty = for_issue and not note
    if note_empty:
        violations.append({
            "code": "note_empty",
            "message": "签发说明不能为空",
        })
    if len(note) > MAX_NOTE_LEN:
        violations.append({
            "code": "note_too_long",
            "message": f"签发说明过长（>{MAX_NOTE_LEN} 字符）",
        })

    # 条件 0c：同场不可重复签发
    irow = _issuance_row(conn, session_id)
    already_issued = irow is not None
    if already_issued:
        violations.append({
            "code": "already_issued",
            "message": f"评审场 {session_id} 已签发根因结论（签发记录 #{irow['id']}），同场不可重复签发",
            "issuance_id": irow["id"],
        })

    # 条件 3a：非空 / 不重复 / 成员都是本场候选
    malformed: list[dict[str, Any]] = []
    if not submitted:
        malformed.append({"code": "empty_set", "message": "最终根因集合不能为空"})
    dup_ids = sorted({x for x in submitted if submitted.count(x) > 1})
    if dup_ids:
        malformed.append({
            "code": "duplicate_node",
            "message": f"集合中存在重复候选：{', '.join(dup_ids)}",
            "node_ids": dup_ids,
        })
    unknown = sorted(x for x in submitted_set if x not in candidate_set)
    if unknown:
        malformed.append({
            "code": "not_candidate",
            "message": f"以下节点不是本场候选：{', '.join(unknown)}",
            "node_ids": unknown,
        })
    well_formed = not malformed
    violations.extend(malformed)

    # 条件 4：集合恰好覆盖最高并列名次
    missing = sorted(top_set - submitted_set)
    extra = sorted(submitted_set - top_set)
    coverage_ok = not missing and not extra
    if not coverage_ok and schulze is not None:
        if missing:
            violations.append({
                "code": "missing_top_rank",
                "message": "集合未覆盖全部最高并列名次，缺少："
                           + "、".join(f"「{titles.get(x, x)}」" for x in missing),
                "node_ids": missing,
            })
        if extra:
            violations.append({
                "code": "extra_not_top_rank",
                "message": "集合包含不在最高并列名次的候选："
                           + "、".join(
                               f"「{titles.get(x, x)}」（第 {rank_of.get(x) or '?'} 名）" for x in extra),
                "node_ids": extra,
            })

    # 条件 5：候选之间不存在 causes 祖先关系（逐对报告，含确定性见证路径）
    ancestor_pairs: list[dict[str, Any]] = []
    if well_formed:
        for a in sorted(submitted_set):
            for b in sorted(_descendants(adj, a, submitted_set)):
                ancestor_pairs.append({
                    "code": "candidate_ancestor_relation",
                    "ancestor_node_id": a,
                    "descendant_node_id": b,
                    "ancestor_title": titles.get(a, a),
                    "descendant_title": titles.get(b, b),
                    "witness_path": _witness_path(adj, a, b),
                    "message": f"「{titles.get(a, a)}」是「{titles.get(b, b)}」的 causes 祖先，"
                               f"最终根因之间不得存在因果祖先关系",
                })
        ancestor_pairs.sort(key=lambda v: (v["ancestor_node_id"], v["descendant_node_id"]))
    violations.extend(ancestor_pairs)

    # 条件 6：每个候选证据状态不得为 insufficient / refuted
    bad_evidence: list[dict[str, Any]] = []
    if well_formed:
        for cid in sorted(submitted_set):
            rv = reviews.get(cid)
            status = rv["status"] if rv is not None else "insufficient"
            if status not in EVIDENCE_OK_STATUSES:
                bad_evidence.append({
                    "code": "evidence_not_admissible",
                    "node_id": cid,
                    "title": titles.get(cid, cid),
                    "evidence_status": status,
                    "evidence_status_label": EVIDENCE_STATUS_LABELS.get(status, status),
                    "support_score": rv["support_score"] if rv is not None else 0.0,
                    "refute_score": rv["refute_score"] if rv is not None else 0.0,
                    "message": f"「{titles.get(cid, cid)}」证据状态为"
                               f"「{EVIDENCE_STATUS_LABELS.get(status, status)}」，"
                               "不得作为最终根因（仅允许已支持 / 存在争议）",
                })
    violations.extend(bad_evidence)

    # 条件 7：绑定版本有效时间窗无冲突（提交时已保证；对历史快照重算兜底）
    time_conflict_ok = win_conflict is None
    if win_conflict is not None:
        violations.append({
            "code": "time_window_conflict",
            "message": graph._conflict_message(g.nodes, win_conflict),
            "conflict": win_conflict,
        })

    checks = {
        "session_closed": closed,
        "note_present": bool(note) if for_issue else None,
        "not_already_issued": not already_issued,
        "set_well_formed": well_formed,
        "covers_top_rank_exactly": coverage_ok,
        "no_candidate_ancestor_relation": not ancestor_pairs,
        "evidence_admissible": not bad_evidence,
        "time_window_ok": time_conflict_ok,
    }
    valid = not violations
    return {
        "session_id": session_id,
        "session_title": srow["title"],
        "session_status": srow["status"],
        "session_revision": srow["revision"],
        "version_id": version_id,
        "submitted_node_ids": sorted(submitted),
        "top_rank_node_ids": sorted(top_rank),
        "has_schulze_result": schulze is not None,
        "ballot_count": len(rankings),
        "checks": checks,
        "valid": valid,
        "violations": violations,
        "candidates": candidate_rows,
        "issuance": issuance_brief(irow) if irow is not None else None,
    }


# ---------- 规范化 payload 与摘要 ----------

def _build_payload(
    *,
    srow: sqlite3.Row,
    vrow: sqlite3.Row,
    note: str,
    root_ids: list[str],
    schulze: dict[str, Any],
    candidates_detail: list[dict[str, Any]],
    issued_at: str,
) -> dict[str, Any]:
    """构造规范化结论 payload（结构不依赖 dict 遍历顺序；摘要对其紧凑 JSON 计算）。"""
    root_detail = [c for c in candidates_detail if c["node_id"] in set(root_ids)]
    root_detail.sort(key=lambda c: c["node_id"])
    return {
        "format": FORMAT,
        "meta": {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "session_id": srow["id"],
            "session_title": srow["title"],
            "session_revision": srow["revision"],
            "session_closed_at": srow["closed_at"],
            "version_id": vrow["id"],
            "version_message": vrow["message"],
            "version_kind": vrow["kind"],
            "version_base_id": vrow["base_id"],
            "version_created_at": vrow["created_at"],
            "note": note,
            "issued_at": issued_at,
            "min_votes": srow["min_votes"],
        },
        "root_causes": root_detail,
        "candidates": sorted(candidates_detail, key=lambda c: c["node_id"]),
        "schulze": schulze,
    }


def build_package(payload: dict[str, Any], issued_at: str) -> dict[str, Any]:
    """对规范化 payload 计算 SHA-256，组成完整签发文件。"""
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "digest": {
            "algorithm": conclusion.DIGEST_ALGORITHM,
            "canonicalization": conclusion.CANONICALIZATION,
            "value": conclusion.sha256_hex(conclusion.canonical_json_bytes(payload)),
        },
        "issued_at": issued_at,
        "payload": payload,
    }


def _candidate_detail(
    g: graph.Graph,
    cid: str,
    rank_of: dict[str, int | None],
    top_set: set[str],
    reviews: dict[str, dict[str, Any]],
    windows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rv = reviews.get(cid)
    status = rv["status"] if rv is not None else "insufficient"
    return {
        "node_id": cid,
        "title": g.nodes[cid].title,
        "rank": rank_of.get(cid),
        "is_top_rank": cid in top_set,
        "evidence_status": status,
        "support_score": rv["support_score"] if rv is not None else 0.0,
        "refute_score": rv["refute_score"] if rv is not None else 0.0,
        "time_window": graph.time_window_dict(g.nodes, windows, cid),
    }


# ---------- 签发（单写事务，全成或全不成） ----------

def issue_issuance(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    node_ids: list[str],
    note: str,
) -> dict[str, Any]:
    note = (note or "").strip()
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = evaluate(conn, session_id, node_ids, note=note, for_issue=True)
        if not report["valid"]:
            first = report["violations"][0]
            # 重复签发是状态冲突 → 409；其余校验失败 → 400
            status = 409 if first["code"] == "already_issued" else 400
            raise IssuanceError(first["message"], first["code"], status, report=report)

        srow = _session_row(conn, session_id)
        vrow = conn.execute(
            "SELECT * FROM versions WHERE id = ?", (srow["version_id"],)).fetchone()
        g = graph.load_graph(conn, srow["version_id"])
        candidate_ids = review._candidate_ids(conn, session_id)
        rankings = _ballot_rankings(conn, session_id)
        schulze = review.schulze_result(candidate_ids, rankings)
        schulze["ballots"] = [  # 与 GET /api/reviews 一致：公开全部冻结选票
            {"reviewer": b["reviewer"], "ranking": json.loads(b["ranking"]),
             "submitted_at": b["submitted_at"]}
            for b in review._ballot_rows(conn, session_id)
        ]
        rank_of = {cid: None for cid in candidate_ids}
        top_set: set[str] = set()
        for layer in schulze["ranks"]:
            for cid in layer["node_ids"]:
                rank_of[cid] = layer["rank"]
            if layer["rank"] == 1:
                top_set = set(layer["node_ids"])
        windows, _conflict = graph.compute_time_windows(g.nodes, g.edges)
        reviews = graph.compute_evidence_review(g.nodes, g.edges)
        detail = [_candidate_detail(g, cid, rank_of, top_set, reviews, windows)
                  for cid in candidate_ids]

        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        root_ids = sorted(set(node_ids))
        payload = _build_payload(
            srow=srow, vrow=vrow, note=note, root_ids=root_ids,
            schulze=schulze, candidates_detail=detail, issued_at=issued_at,
        )
        package = build_package(payload, issued_at)
        digest = package["digest"]["value"]
        payload_text = conclusion.canonical_json_bytes(payload).decode("utf-8")
        package_text = conclusion.canonical_json_bytes(package).decode("utf-8")

        cur = conn.execute(
            "INSERT INTO root_cause_issuances "
            "(session_id, version_id, note, payload_json, package_json, "
            "digest_algorithm, digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, srow["version_id"], note, payload_text, package_text,
             conclusion.DIGEST_ALGORITHM, digest),
        )
        issuance_id = cur.lastrowid
        conn.execute("COMMIT")
    except IssuanceError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        # UNIQUE(session_id) 在并发签发竞争时兜底：先到者已写入
        raise IssuanceError(
            f"评审场 {session_id} 已签发根因结论，同场不可重复签发",
            "already_issued", 409) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_issuance(conn, session_id)


# ---------- 查询 ----------

def issuance_brief(row: sqlite3.Row) -> dict[str, Any]:
    """列表 / 评审场摘要用：不解析 payload。"""
    payload = json.loads(row["payload_json"])
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "version_id": row["version_id"],
        "note": row["note"],
        "issued_at": row["issued_at"],
        "digest_algorithm": row["digest_algorithm"],
        "digest": row["digest"],
        "root_cause_node_ids": [c["node_id"] for c in payload["root_causes"]],
        "root_cause_titles": [c["title"] for c in payload["root_causes"]],
    }


def issuance_full(row: sqlite3.Row) -> dict[str, Any]:
    """单条详情：附带规范化 payload 与完整签发文件（可下载留存）。"""
    out = issuance_brief(row)
    out["payload"] = json.loads(row["payload_json"])
    out["package"] = json.loads(row["package_json"])
    return out


def get_issuance(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    row = _issuance_row(conn, session_id)
    if row is None:
        raise IssuanceError(
            f"评审场 {session_id} 尚无签发记录", "issuance_not_found", 404)
    return issuance_full(row)


def list_sessions_issued(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """session_id -> 签发摘要，供评审场列表 / 详情批量挂接。"""
    rows = conn.execute("SELECT * FROM root_cause_issuances").fetchall()
    return {r["session_id"]: issuance_brief(r) for r in rows}
