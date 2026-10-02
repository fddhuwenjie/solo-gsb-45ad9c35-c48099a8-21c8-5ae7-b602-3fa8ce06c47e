"""根因共识盲评：绑定不可变版本快照的 Schulze 计票评审场。

流程：
- 建场：从某个不可变版本选取 3~20 个事件作为候选，登记唯一评审人并设置最低票数；
- 投票：每位登记评审人提交「包含全部候选且不重复」的严格排序；封榜前可覆盖自己的
  选票，且接口只暴露已提交名单与数量，不泄露任何排序；
- 封榜：在 SQLite 写事务（BEGIN IMMEDIATE）中校验 session_revision 与最低票数并
  冻结选票；revision 过期或重复封榜返回 409；
- 计票：封榜后按 Schulze 法计算两两偏好 d 与最强路径 p，按胜出关系逐层生成并列名次
  （候选 ID 仅用于稳定展示，不打断算法并列），并为任意胜负返回确定性的最强路径见证。

本模块只读写 review_* 表：评审场绑定原版本快照用于校验与展示，
不改写因果图、版本或结论包。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

MIN_CANDIDATES = 3
MAX_CANDIDATES = 20
MAX_REVIEWERS = 100
MAX_REVIEWER_LEN = 64
MAX_TITLE_LEN = 200


class ReviewError(Exception):
    """评审场业务错误：status_code 由路由层原样返回（过期 / 重复封榜等为 409）。"""

    def __init__(self, message: str, code: str = "invalid", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# ---------- 读取辅助 ----------

def _row_or_404(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM review_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise ReviewError(f"评审场不存在：{session_id}", "session_not_found", 404)
    return row


def _candidate_ids(conn: sqlite3.Connection, session_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT node_id FROM review_candidates WHERE session_id = ? ORDER BY node_id",
        (session_id,),
    ).fetchall()
    return [r["node_id"] for r in rows]


def _reviewer_names(conn: sqlite3.Connection, session_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT reviewer FROM review_reviewers WHERE session_id = ? ORDER BY reviewer",
        (session_id,),
    ).fetchall()
    return [r["reviewer"] for r in rows]


def _ballot_rows(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT reviewer, ranking, submitted_at FROM review_ballots "
        "WHERE session_id = ? ORDER BY reviewer",
        (session_id,),
    ).fetchall()


def _titles(conn: sqlite3.Connection, version_id: int, node_ids: list[str]) -> dict[str, str]:
    """候选节点在绑定版本快照中的标题（版本不可变，展示随快照稳定）。"""
    if not node_ids:
        return {}
    marks = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"SELECT id, title FROM nodes WHERE version_id = ? AND id IN ({marks})",
        (version_id, *node_ids),
    ).fetchall()
    return {r["id"]: r["title"] for r in rows}


# ---------- 建场 ----------

def create_session(
    conn: sqlite3.Connection,
    *,
    title: str,
    version_id: int,
    candidate_ids: list[str],
    reviewers: list[str],
    min_votes: int,
) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ReviewError("评审场名称不能为空", "invalid_title")
    if len(title) > MAX_TITLE_LEN:
        raise ReviewError(f"评审场名称过长（>{MAX_TITLE_LEN} 字符）", "invalid_title")
    if conn.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
        raise ReviewError(f"版本不存在：{version_id}", "version_not_found", 404)

    # 候选：3~20 个、不重复、且都是该版本快照中的事件节点
    if not (MIN_CANDIDATES <= len(candidate_ids) <= MAX_CANDIDATES):
        raise ReviewError(
            f"候选事件数量须在 {MIN_CANDIDATES}~{MAX_CANDIDATES} 之间", "invalid_candidates")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ReviewError("候选事件存在重复", "duplicate_candidate")
    marks = ",".join("?" for _ in candidate_ids)
    rows = conn.execute(
        f"SELECT id, type FROM nodes WHERE version_id = ? AND id IN ({marks})",
        (version_id, *candidate_ids),
    ).fetchall()
    found = {r["id"]: r["type"] for r in rows}
    missing = [c for c in candidate_ids if c not in found]
    if missing:
        raise ReviewError(
            f"候选事件在版本 v{version_id} 中不存在：{', '.join(missing)}", "candidate_not_found")
    not_event = [c for c in candidate_ids if found[c] != "event"]
    if not_event:
        raise ReviewError(
            f"候选必须是事件节点：{', '.join(not_event)}", "candidate_not_event")

    # 评审人：去空白后非空且唯一
    names: list[str] = []
    for r in reviewers:
        name = (r or "").strip()
        if not name:
            raise ReviewError("评审人姓名不能为空", "invalid_reviewer")
        if len(name) > MAX_REVIEWER_LEN:
            raise ReviewError(f"评审人姓名过长（>{MAX_REVIEWER_LEN} 字符）", "invalid_reviewer")
        names.append(name)
    if len(names) > MAX_REVIEWERS:
        raise ReviewError(f"评审人数量超过上限 {MAX_REVIEWERS}", "invalid_reviewer")
    if len(set(names)) != len(names):
        raise ReviewError("评审人必须唯一（存在重复姓名）", "duplicate_reviewer")
    if not (1 <= min_votes <= len(names)):
        raise ReviewError(
            f"最低票数须在 1~{len(names)}（评审人数量）之间", "invalid_min_votes")

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO review_sessions (title, version_id, min_votes) VALUES (?, ?, ?)",
            (title, version_id, min_votes),
        )
        sid = cur.lastrowid
        conn.executemany(
            "INSERT INTO review_candidates (session_id, node_id) VALUES (?, ?)",
            [(sid, c) for c in sorted(set(candidate_ids))],
        )
        conn.executemany(
            "INSERT INTO review_reviewers (session_id, reviewer) VALUES (?, ?)",
            [(sid, r) for r in sorted(set(names))],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_session(conn, sid)


# ---------- 投票（封榜前可覆盖自己的选票） ----------

def _validate_ranking(ranking: Any, candidates: list[str]) -> None:
    """选票必须是「包含全部候选且不重复」的严格排序。"""
    if not isinstance(ranking, list) or len(ranking) != len(candidates):
        raise ReviewError(
            f"选票必须包含全部 {len(candidates)} 个候选的严格排序", "invalid_ranking")
    if any(not isinstance(c, str) for c in ranking):
        raise ReviewError("选票候选必须是节点 id 字符串", "invalid_ranking")
    if len(set(ranking)) != len(ranking):
        raise ReviewError("选票中存在重复候选，必须为不重复的严格排序", "invalid_ranking")
    unknown = [c for c in ranking if c not in set(candidates)]
    if unknown:
        raise ReviewError(f"选票包含非本场候选：{', '.join(unknown)}", "invalid_ranking")


def submit_ballot(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    reviewer: str,
    ranking: list[str],
) -> dict[str, Any]:
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ReviewError("评审人姓名不能为空", "invalid_reviewer")
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row_or_404(conn, session_id)
        if row["status"] != "open":
            raise ReviewError("该评审场已封榜，选票已冻结", "session_closed", 409)
        if reviewer not in set(_reviewer_names(conn, session_id)):
            raise ReviewError(f"评审人未登记：{reviewer}", "reviewer_not_registered", 403)
        candidates = _candidate_ids(conn, session_id)
        _validate_ranking(ranking, candidates)
        # 同一评审人重复提交 = 覆盖自己的选票（封榜前允许）
        conn.execute(
            "INSERT INTO review_ballots (session_id, reviewer, ranking) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id, reviewer) DO UPDATE SET "
            "ranking = excluded.ranking, "
            "submitted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (session_id, reviewer, json.dumps(list(ranking), ensure_ascii=False)),
        )
        # 选票变化即评审场状态变化：revision 自增，使基于旧视图的封榜被判为过期
        conn.execute(
            "UPDATE review_sessions SET revision = revision + 1 WHERE id = ?",
            (session_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    row = _row_or_404(conn, session_id)
    submitted = [b["reviewer"] for b in _ballot_rows(conn, session_id)]
    return {
        "session_id": session_id,
        "revision": row["revision"],
        "submitted": submitted,
        "submitted_count": len(submitted),
    }


# ---------- 封榜（写事务内校验 revision 与最低票数，随后冻结选票） ----------

def close_session(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _row_or_404(conn, session_id)
        if row["status"] != "open":
            raise ReviewError(
                f"评审场 {session_id} 已封榜，请勿重复封榜", "already_closed", 409)
        if row["revision"] != expected_revision:
            raise ReviewError(
                f"session_revision 已过期（请求基于 r{expected_revision}，"
                f"当前为 r{row['revision']}），请刷新后重试",
                "stale_revision", 409)
        count = conn.execute(
            "SELECT COUNT(*) FROM review_ballots WHERE session_id = ?", (session_id,),
        ).fetchone()[0]
        if count < row["min_votes"]:
            raise ReviewError(
                f"已提交 {count} 票，未达到最低票数 {row['min_votes']}，暂不能封榜",
                "min_votes_not_met", 409)
        # 冻结：状态置为 closed 后，投票接口一律拒绝，选票即不可再变
        conn.execute(
            "UPDATE review_sessions SET status = 'closed', "
            "closed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), revision = revision + 1 "
            "WHERE id = ?",
            (session_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_session(conn, session_id)


# ---------- 查询 ----------

def get_session(conn: sqlite3.Connection, session_id: int) -> dict[str, Any]:
    row = _row_or_404(conn, session_id)
    candidates = _candidate_ids(conn, session_id)
    reviewers = _reviewer_names(conn, session_id)
    ballots = _ballot_rows(conn, session_id)
    titles = _titles(conn, row["version_id"], candidates)
    submitted = [b["reviewer"] for b in ballots]
    out: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "version_id": row["version_id"],
        "min_votes": row["min_votes"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "candidates": [{"node_id": c, "title": titles.get(c, c)} for c in candidates],
        "reviewers": reviewers,
        # 封榜前只暴露已提交名单与数量，不泄露任何排序
        "submitted": submitted,
        "submitted_count": len(submitted),
        "result": None,
    }
    if row["status"] == "closed":
        rankings = [json.loads(b["ranking"]) for b in ballots]
        result = schulze_result(candidates, rankings)
        # 封榜后公开全部选票，结果可复算
        result["ballots"] = [
            {"reviewer": b["reviewer"], "ranking": json.loads(b["ranking"]),
             "submitted_at": b["submitted_at"]}
            for b in ballots
        ]
        out["result"] = result
    return out


def list_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT s.*, "
        "  (SELECT COUNT(*) FROM review_candidates c WHERE c.session_id = s.id) AS candidate_count, "
        "  (SELECT COUNT(*) FROM review_reviewers r WHERE r.session_id = s.id) AS reviewer_count, "
        "  (SELECT COUNT(*) FROM review_ballots b WHERE b.session_id = s.id) AS submitted_count "
        "FROM review_sessions s ORDER BY s.id DESC",
    ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "status": r["status"],
            "version_id": r["version_id"],
            "min_votes": r["min_votes"],
            "revision": r["revision"],
            "created_at": r["created_at"],
            "closed_at": r["closed_at"],
            "candidate_count": r["candidate_count"],
            "reviewer_count": r["reviewer_count"],
            "submitted_count": r["submitted_count"],
        }
        for r in rows
    ]


# ---------- Schulze 计票（纯函数） ----------

def schulze_result(candidate_ids: list[str], ballot_rankings: list[list[str]]) -> dict[str, Any]:
    """Schulze 计票：两两偏好 d、最强路径 p、逐层并列名次与确定性最强路径见证。

    纯函数：候选先按 ID 排序再计算；p 与名次分层不依赖选票顺序，
    候选 ID 仅用于稳定展示与见证路径的确定性选择，不打断算法并列。
    """
    cands = sorted(candidate_ids)
    n = len(cands)

    # 1) 两两偏好：d[i][j] = 把 i 严格排在 j 之前的选票数
    d = [[0] * n for _ in range(n)]
    for ranking in ballot_rankings:
        pos = {cid: k for k, cid in enumerate(ranking)}
        for i in range(n):
            pi = pos[cands[i]]
            for j in range(n):
                if i != j and pi < pos[cands[j]]:
                    d[i][j] += 1

    # 2) 最强路径（Schulze）：p[i][j] = i→j 所有路径中「最弱一环」的最大值；
    #    只保留 d[i][j] > d[j][i] 的有向边作为初始边权
    p = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j and d[i][j] > d[j][i]:
                p[i][j] = d[i][j]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            for k in range(n):
                if k == i or k == j:
                    continue
                v = min(p[j][i], p[i][k])
                if v > p[j][k]:
                    p[j][k] = v

    # 3) 逐层并列名次：每轮取出「不被其余候选击败」（p[j][i] <= p[i][j] 对所有 j）
    #    的集合作为同一并列层；Schulze 胜出关系是严格偏序，该集合必然非空。
    #    层内按候选 ID 排序仅为稳定展示，同层即算法并列。
    ranks: list[dict[str, Any]] = []
    remaining = list(range(n))
    while remaining:
        top = [i for i in remaining
               if all(p[j][i] <= p[i][j] for j in remaining if j != i)]
        if not top:  # 防御：理论上不会发生（胜出关系无环）
            top = list(remaining)
        ranks.append({"rank": len(ranks) + 1, "node_ids": [cands[i] for i in top]})
        top_set = set(top)
        remaining = [i for i in remaining if i not in top_set]

    # 4) 确定性最强路径见证：在「d[u][v] > d[v][u] 且 d[u][v] >= p[a][b]」的子图上
    #    按候选 ID 升序 DFS，第一条到达 b 的路径即见证；其最弱边恰为 p[a][b]
    #    （>= 由构造保证，> 则与 p 的最强性矛盾）。
    def witness(ai: int, bi: int) -> list[str]:
        threshold = p[ai][bi]
        visited = {ai}
        path = [ai]

        def dfs(u: int) -> bool:
            if u == bi:
                return True
            for v in range(n):
                if v in visited:
                    continue
                if d[u][v] > d[v][u] and d[u][v] >= threshold:
                    visited.add(v)
                    path.append(v)
                    if dfs(v):
                        return True
                    path.pop()
            return False

        dfs(ai)
        return [cands[i] for i in path]

    witnesses: dict[str, dict[str, Any]] = {}
    for a in range(n):
        row_w: dict[str, Any] = {}
        for b in range(n):
            if a == b or p[a][b] <= 0:
                continue
            row_w[cands[b]] = {"strength": p[a][b], "path": witness(a, b)}
        if row_w:
            witnesses[cands[a]] = row_w

    return {
        "method": "schulze",
        # 矩阵行 / 列顺序（按候选 ID 排序，仅为稳定展示）
        "candidate_order": cands,
        "pairwise": {cands[i]: {cands[j]: d[i][j] for j in range(n) if j != i}
                     for i in range(n)},
        "strongest": {cands[i]: {cands[j]: p[i][j] for j in range(n) if j != i}
                      for i in range(n)},
        "ranks": ranks,
        "witnesses": witnesses,
    }
