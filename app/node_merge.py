"""同一事故的重复事件归并（Node Merge）。

在**当前最新版本**（工作图基线）上，把一个保留事件与一个或多个同事故重复事件归并为一个事件：

- 重复节点的 causes 入边 / 出边与 supports / refutes 证据边全部迁移到保留节点；
- 迁移后同端点同类型连线只保留一条（取连线 id 最小者），非空标签去重、按字典序排序后
  以 ``" / "`` 合并；
- 标题、描述、时间窗（earliest_time / latest_time）与 trigger_mode 在候选事件间取值
  不同即产生**字段冲突**，必须由用户在冲突项中显式选择取值来自哪个候选；其余字段
  （位置、id、created_at）沿用保留节点。

整次操作拒绝条件（均在 blockers 中给出确定性冲突路径，预览与落库同一口径）：

1. ``candidate_ancestor_relation``：候选事件在 causes 子图上互为祖先；
2. ``self_loop``：边迁移后两端指向同一节点（自环）；
3. ``cycle``：迁移后的图存在环（含经不同重复节点折回形成的环）；
4. ``time_window_disjoint``：候选自报时间窗没有交集；
5. ``all_prerequisite_lost``：归并使任一 ALL 触发节点的不同前因收缩为同一个（前因丢失）；
6. ``time_conflict``：归并后在整图上传播有效时间窗出现下界晚于上界。

预览为纯只读；落库在单个 ``BEGIN IMMEDIATE`` 写事务内重新读取当前版本并重算整份计划
（不信任预览），全部通过才插入一个 ``kind='merge'`` 的版本快照，返回旧节点 → 保留节点的
映射。既有版本以及绑定旧版本的评审 / 提案 / 签发记录一律不改写。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from dataclasses import replace
from typing import Any

from app import graph
from app.graph import Edge, Graph, GraphError, Node

# 归并时需要用户明确取值的字段（位置 / id / created_at 不在此列，一律沿用保留节点）
MERGE_FIELDS = ("title", "description", "earliest_time", "latest_time", "trigger_mode")
FIELD_LABELS = {
    "title": "标题",
    "description": "描述",
    "earliest_time": "最早发生时间",
    "latest_time": "最晚发生时间",
    "trigger_mode": "触发模式",
}
# 同端点同类型多条连线合并标签时的分隔符（非空标签去重、按字典序排序后连接）
LABEL_SEP = " / "

# blockers 在响应中的固定排列顺序（每类至多一条确定性见证，self_loop 除外）
_BLOCKER_ORDER = (
    "candidate_ancestor_relation",
    "self_loop",
    "cycle",
    "time_window_disjoint",
    "all_prerequisite_lost",
    "time_conflict",
)


class NodeMergeError(Exception):
    """归并业务错误：status_code 由路由层原样返回（请求不合法为 400）。"""

    def __init__(self, message: str, code: str = "node_merge_invalid",
                 status_code: int = 400, blockers: list[dict[str, Any]] | None = None,
                 cycle: list[str] | None = None, conflict: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.blockers = blockers or []
        self.cycle = cycle
        self.conflict = conflict


# ---------- 纯函数：候选校验 ----------

def _validate_selection(g: Graph, keep_id: str, duplicate_ids: list[str]) -> tuple[str, list[str]]:
    """校验保留 / 重复候选选择本身合法，返回 (keep_id, 去重后的 duplicate_ids)。"""
    if len(set(duplicate_ids)) != len(duplicate_ids):
        raise NodeMergeError("重复事件列表中存在相同的节点 id", "duplicate_not_unique")
    if keep_id in duplicate_ids:
        raise NodeMergeError("保留事件不能同时出现在重复事件列表中", "keep_in_duplicates")

    keep = g.nodes.get(keep_id)
    if keep is None:
        raise NodeMergeError(f"保留事件不存在：{keep_id}", "keep_not_found", 404)
    if keep.type != "event":
        raise NodeMergeError(f"保留节点必须是事件：{keep_id}", "keep_not_event")
    for nid in duplicate_ids:
        n = g.nodes.get(nid)
        if n is None:
            raise NodeMergeError(f"重复事件不存在：{nid}", "duplicate_not_found", 404)
        if n.type != "event":
            raise NodeMergeError(f"重复节点必须是事件：{nid}", "duplicate_not_event")
    return keep_id, list(duplicate_ids)


def _resolve_field_resolutions(
    candidates: dict[str, Node], resolutions: dict[str, str] | None
) -> dict[str, str]:
    """校验请求携带的字段裁决：字段名合法、取值来源必须是候选之一。"""
    out: dict[str, str] = {}
    for field, source_id in (resolutions or {}).items():
        if field not in MERGE_FIELDS:
            raise NodeMergeError(f"未知的归并字段：{field}", "invalid_field_resolution")
        if source_id not in candidates:
            raise NodeMergeError(
                f"字段 {FIELD_LABELS[field]} 的取值来源不在候选事件中：{source_id}",
                "invalid_field_resolution")
        out[field] = source_id
    return out


# ---------- 纯函数：确定性路径见证 ----------

def _cause_adjacency(nodes: dict[str, Node], edges: dict[str, Edge]):
    """causes 子图邻接表，后继与见证边均按 id 升序，保证 BFS 路径确定性。"""
    adj: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for eid in sorted(edges):
        e = edges[eid]
        if e.kind != "causes" or e.source_id not in nodes or e.target_id not in nodes:
            continue
        adj[e.source_id].append((e.target_id, e.id))
    for u in adj:
        adj[u].sort()
    return adj


def _witness_path(adj, src: str, dst: str) -> tuple[list[str], list[str]] | None:
    """在 causes 子图上 BFS 取 src → dst 的确定性最短路径（邻接按 id 升序）。"""
    prev: dict[str, tuple[str | None, str | None]] = {src: (None, None)}
    q: deque[str] = deque([src])
    while q:
        u = q.popleft()
        if u == dst:
            break
        for v, eid in adj.get(u, []):
            if v not in prev:
                prev[v] = (u, eid)
                q.append(v)
    if dst not in prev:
        return None
    chain = [dst]
    edge_ids: list[str] = []
    cur = dst
    while prev[cur][0] is not None:
        parent, eid = prev[cur]
        edge_ids.append(eid)  # type: ignore[arg-type]
        chain.append(parent)  # type: ignore[arg-type]
        cur = parent  # type: ignore[assignment]
    chain.reverse()
    edge_ids.reverse()
    return chain, edge_ids


# ---------- 纯函数：归并计划 ----------

def build_merge_plan(
    g: Graph,
    keep_id: str,
    duplicate_ids: list[str],
    resolutions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """在内存图 g 上计算归并计划。

    返回字段：
    - candidates / duplicates：候选快照（标题、时间窗、trigger_mode）；
    - field_conflicts：字段冲突项（含各取值选项与等值候选）；
    - resolved_fields：每个冲突字段在当前裁决下的生效值（未裁决取保留节点值占位）；
    - edge_moves：rewired（单条迁移）/ collapsed（多条合一条）清单；
    - blockers：整次操作必须拒绝的确定性冲突路径（空数组表示可落库）；
    - graph：应用全部迁移与字段取值后的内存图（预览图）。
    """
    keep_id, duplicate_ids = _validate_selection(g, keep_id, duplicate_ids)
    dup_set = set(duplicate_ids)
    candidate_ids = sorted(set(duplicate_ids) | {keep_id})
    candidates = {nid: g.nodes[nid] for nid in candidate_ids}
    choices = _resolve_field_resolutions(candidates, resolutions)

    # ---- 1) 字段冲突：同字段在候选间存在不同取值 ----
    field_conflicts: list[dict[str, Any]] = []
    effective: dict[str, Any] = {}
    resolved_fields: dict[str, Any] = {}
    for field in MERGE_FIELDS:
        # 按候选 id 升序收集取值，首次出现的顺序即选项顺序（确定性）
        groups: list[list[str]] = []
        value_of_group: list[Any] = []
        for nid in candidate_ids:
            val = getattr(candidates[nid], field)
            for gi, gv in enumerate(value_of_group):
                if gv == val:
                    groups[gi].append(nid)
                    break
            else:
                value_of_group.append(val)
                groups.append([nid])
        if len(groups) == 1:
            effective[field] = value_of_group[0]
            continue

        def node_brief(nid: str) -> dict[str, Any]:
            n = candidates[nid]
            return {"node_id": nid, "title": n.title}

        options = [
            {"value": val, "source_node_id": ids[0], "equivalent_node_ids": sorted(ids),
             "node_title": candidates[ids[0]].title}
            for val, ids in zip(value_of_group, groups)
        ]
        chosen_id = choices.get(field)
        unresolved = chosen_id is None
        if unresolved:
            # 预览占位：未裁决时暂取保留节点的值（落库前必须显式裁决）
            chosen_id = keep_id
        chosen_value = getattr(candidates[chosen_id], field)
        effective[field] = chosen_value
        field_conflicts.append({
            "field": field,
            "field_label": FIELD_LABELS[field],
            "options": options,
            "resolved": not unresolved,
            "selected_node_id": None if unresolved else chosen_id,
        })
        resolved_fields[field] = {
            "value": chosen_value,
            "source_node_id": chosen_id,
            "unresolved": unresolved,
        }

    # ---- 2) 组装归并后的节点（重复节点删除，保留节点套用生效字段）----
    merged_nodes: dict[str, Node] = {}
    for nid, n in g.nodes.items():
        if nid in dup_set:
            continue
        if nid == keep_id:
            merged_nodes[nid] = replace(
                n,
                title=effective["title"],
                description=effective["description"],
                earliest_time=effective["earliest_time"],
                latest_time=effective["latest_time"],
                trigger_mode=effective["trigger_mode"] if effective["trigger_mode"] in ("ANY", "ALL") else "ANY",
            )
        else:
            merged_nodes[nid] = n

    def mapped(x: str) -> str:
        return keep_id if x in candidates else x

    # ---- 3) 边迁移：按 (新起点, 新终点, kind) 分组合并 ----
    groups_edges: dict[tuple[str, str, str], list[Edge]] = defaultdict(list)
    self_loop_edges: list[Edge] = []
    for eid in sorted(g.edges):
        e = g.edges[eid]
        s, t = mapped(e.source_id), mapped(e.target_id)
        if s == t:
            self_loop_edges.append(e)
            continue
        groups_edges[(s, t, e.kind)].append(e)

    merged_edges: dict[str, Edge] = {}
    rewired: list[dict[str, Any]] = []
    collapsed: list[dict[str, Any]] = []
    for key in sorted(groups_edges):
        s, t, kind = key
        members = sorted(groups_edges[key], key=lambda e: e.id)
        survivor = members[0]
        labels = sorted({e.label for e in members if e.label})
        label = LABEL_SEP.join(labels)
        merged_edges[survivor.id] = Edge(survivor.id, s, t, kind, label, survivor.created_at)
        if len(members) == 1:
            e = members[0]
            # 只有端点实际发生迁移的单边才列入 rewired；本来就连在保留节点上的边不动
            if (e.source_id, e.target_id) != (s, t):
                rewired.append({
                    "edge_id": e.id,
                    "kind": e.kind,
                    "label": label,
                    "old_source_id": e.source_id,
                    "old_target_id": e.target_id,
                    "new_source_id": s,
                    "new_target_id": t,
                    "old_source_title": g.nodes[e.source_id].title if e.source_id in g.nodes else None,
                    "old_target_title": g.nodes[e.target_id].title if e.target_id in g.nodes else None,
                })
        else:
            collapsed.append({
                "kind": kind,
                "source_id": s,
                "target_id": t,
                "source_title": merged_nodes[s].title if s in merged_nodes else None,
                "target_title": merged_nodes[t].title if t in merged_nodes else None,
                "surviving_edge_id": survivor.id,
                "removed_edge_ids": sorted(e.id for e in members[1:]),
                "member_edge_ids": [e.id for e in members],
                "member_labels": {e.id: e.label for e in members},
                "label": label,
            })

    # ---- 4) 拒绝条件（确定性见证路径）----
    blockers: list[dict[str, Any]] = []

    # 4.1 候选在 causes 子图上互为祖先（按候选 id 升序找第一对）
    adj_orig = _cause_adjacency(g.nodes, g.edges)
    ancestor_blocker = None
    for a in candidate_ids:
        for b in candidate_ids:
            if a == b:
                continue
            witness = _witness_path(adj_orig, a, b)
            if witness is not None:
                chain, edge_ids = witness
                ancestor_blocker = {
                    "code": "candidate_ancestor_relation",
                    "message": (
                        f"候选事件「{candidates[a].title}」是「{candidates[b].title}」的原因祖先，"
                        "归并重复事件不允许候选之间存在 causes 祖先关系"),
                    "ancestor_id": a,
                    "descendant_id": b,
                    "ancestor_title": candidates[a].title,
                    "descendant_title": candidates[b].title,
                    "path": chain,
                    "edge_ids": edge_ids,
                }
                break
        if ancestor_blocker is not None:
            break
    if ancestor_blocker is not None:
        blockers.append(ancestor_blocker)

    # 4.2 迁移后自环（证据边结构上不可能；causes 自环通常已被 4.1 拦截，此处兜底）
    for e in self_loop_edges:
        blockers.append({
            "code": "self_loop",
            "message": f"连线 {e.id}（{e.kind}）迁移后两端都指向保留事件，形成自环",
            "edge_id": e.id,
            "kind": e.kind,
            "endpoints": sorted({e.source_id, e.target_id}),
            "node_id": keep_id,
            "node_title": merged_nodes[keep_id].title,
        })

    # 4.3 迁移后的图成环
    cycle = graph.find_cycle(merged_nodes, merged_edges)
    if cycle:
        pair_edge: dict[tuple[str, str], str] = {}
        for e in merged_edges.values():
            pair_edge[(e.source_id, e.target_id)] = e.id
        cycle_edges = [pair_edge[(a, b)] for a, b in zip(cycle, cycle[1:] + cycle[:1])
                       if (a, b) in pair_edge]
        blockers.append({
            "code": "cycle",
            "message": "边迁移后因果图形成环：" + " → ".join(
                [merged_nodes[nid].title for nid in cycle if nid in merged_nodes]
                + [merged_nodes[cycle[0]].title]),
            "cycle": cycle,
            "edge_ids": cycle_edges,
        })

    # 4.4 候选自报时间窗无交集（下界取各候选最早时间的最大，上界取最晚时间的最小）
    earliest_pairs = [(n.earliest_time, nid) for nid, n in candidates.items() if n.earliest_time]
    latest_pairs = [(n.latest_time, nid) for nid, n in candidates.items() if n.latest_time]
    lower = max(earliest_pairs, default=None)
    upper = min(latest_pairs, default=None)
    if lower is not None and upper is not None and lower[0] > upper[0]:
        blockers.append({
            "code": "time_window_disjoint",
            "message": (
                f"候选事件时间窗无交集：最晚上界 {upper[0]}（「{candidates[upper[1]].title}」）"
                f"早于最早下界 {lower[0]}（「{candidates[lower[1]].title}」）"),
            "lower_bound": lower[0],
            "upper_bound": upper[0],
            "lower_node_id": lower[1],
            "upper_node_id": upper[1],
            "lower_node_title": candidates[lower[1]].title,
            "upper_node_title": candidates[upper[1]].title,
            "node_ids": candidate_ids,
        })

    # 4.5 ALL 触发节点的任一前因不得丢失：映射后不同前因的数量不得收缩
    orig_parents: dict[str, set[str]] = defaultdict(set)
    pair_edges: dict[tuple[str, str], list[str]] = defaultdict(list)
    for e in g.edges.values():
        if e.kind != "causes":
            continue
        orig_parents[e.target_id].add(e.source_id)
        pair_edges[(e.target_id, e.source_id)].append(e.id)
    all_loss_blocker = None
    for v in sorted(merged_nodes):
        if merged_nodes[v].type != "event":
            continue
        preimages = set(candidates) if v == keep_id else {v}
        before: set[str] = set()
        for u in preimages:
            before |= orig_parents.get(u, set())
        after = {mapped(p) for p in before}
        after.discard(v)  # 指向自身的边已由 self_loop 拦截，比较时不计入
        mode = merged_nodes[v].trigger_mode
        if mode == "ALL" and len(after) < len(before):
            # 哪些前因被折叠到了同一目标（确定性：按映射目标 id 升序）
            folded: dict[str, list[str]] = defaultdict(list)
            for p in before:
                mp = mapped(p)
                if mp != v:
                    folded[mp].append(p)
            folded_groups = [sorted(ids) for _target, ids in sorted(folded.items()) if len(ids) > 1]
            witness_edges: set[str] = set()
            folded_parents = {p for grp in folded_groups for p in grp}
            for u in preimages:
                for p in folded_parents:
                    witness_edges.update(pair_edges.get((u, p), ()))
            all_loss_blocker = {
                "code": "all_prerequisite_lost",
                "message": (
                    f"ALL 触发节点「{merged_nodes[v].title}」的前因会在归并后丢失："
                    f"迁移前 {len(before)} 个不同前因，迁移后仅剩 {len(after)} 个"),
                "node_id": v,
                "node_title": merged_nodes[v].title,
                "trigger_mode": "ALL",
                "parent_ids_before": sorted(before),
                "parent_ids_after": sorted(after),
                "collapsed_parent_groups": folded_groups,
                "edge_ids": sorted(witness_edges),
            }
            break
    if all_loss_blocker is not None:
        blockers.append(all_loss_blocker)

    # 4.6 归并后整图有效时间窗传播冲突
    windows, time_conflict = graph.compute_time_windows(merged_nodes, merged_edges)
    if time_conflict is not None:
        blockers.append({
            "code": "time_conflict",
            "message": graph._conflict_message(merged_nodes, time_conflict),
            **time_conflict,
        })

    blockers.sort(key=lambda b: _BLOCKER_ORDER.index(b["code"]))

    keep_node = merged_nodes[keep_id]
    return {
        "keep_id": keep_id,
        "keep_title": keep_node.title,
        "duplicate_ids": sorted(dup_set),
        "duplicates": [_candidate_brief(g.nodes[nid]) for nid in sorted(dup_set)],
        "candidates": [_candidate_brief(candidates[nid]) for nid in candidate_ids],
        "candidate_titles": {nid: candidates[nid].title for nid in candidate_ids},
        "field_conflicts": field_conflicts,
        "resolved_fields": resolved_fields,
        "edge_moves": {
            "rewired": rewired,
            "collapsed": collapsed,
            "rewired_count": len(rewired),
            "collapsed_count": len(collapsed),
            "removed_edge_ids": sorted(eid for c in collapsed for eid in c["removed_edge_ids"]),
        },
        "blockers": blockers,
        "cycle": cycle,
        "time_conflict": time_conflict,
        "graph": Graph(g.version_id, merged_nodes, merged_edges),
    }


def _candidate_brief(n: Node) -> dict[str, Any]:
    return {
        "node_id": n.id,
        "title": n.title,
        "description": n.description,
        "earliest_time": n.earliest_time,
        "latest_time": n.latest_time,
        "trigger_mode": n.trigger_mode if n.type == "event" else None,
    }


# ---------- 只读预览 ----------

def preview_merge(conn: sqlite3.Connection, body: Any) -> dict[str, Any]:
    """在当前最新版本上计算归并预览（不写库、不生成版本）。"""
    current = graph.current_version_id(conn)
    base_id = body.base_version_id or current
    if body.base_version_id is not None and body.base_version_id != current:
        raise GraphError(
            f"基准版本已过期（页面基于 v{body.base_version_id}，当前为 v{current}），请刷新后重试",
            "stale_version")
    g = graph.load_graph(conn, base_id)
    if g is None:
        raise NodeMergeError(f"版本不存在：{base_id}", "version_not_found", 404)
    plan = build_merge_plan(g, body.keep_id, list(body.duplicate_ids),
                            dict(body.field_resolutions))
    return _plan_response(conn, plan, base_id, current)


def _node_dict(n: Node, windows: dict, reviews: dict) -> dict[str, Any]:
    data = {"id": n.id, "type": n.type, "title": n.title, "description": n.description,
            "pos_x": n.pos_x, "pos_y": n.pos_y, "created_at": n.created_at,
            "earliest_time": n.earliest_time, "latest_time": n.latest_time,
            "reliability": n.reliability, "source_group": n.source_group,
            "trigger_mode": n.trigger_mode if n.type == "event" else None}
    data["time_window"] = graph.time_window_dict(windows["nodes"], windows["windows"], n.id)
    data["evidence_review"] = graph.evidence_review_dict(reviews["nodes"], reviews["reviews"], n.id)
    return data


def _edge_dict(e: Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label, "created_at": e.created_at}


def _plan_response(conn: sqlite3.Connection, plan: dict[str, Any],
                   base_id: int, current_id: int) -> dict[str, Any]:
    merged: Graph = plan["graph"]
    windows, _c = graph.compute_time_windows(merged.nodes, merged.edges)
    reviews = graph.compute_evidence_review(merged.nodes, merged.edges)
    win_ctx = {"nodes": merged.nodes, "windows": windows}
    rev_ctx = {"nodes": merged.nodes, "reviews": reviews}
    graph_full = {
        "version_id": base_id,
        "nodes": [_node_dict(n, win_ctx, rev_ctx) for nid, n in merged.nodes.items()],
        "edges": [_edge_dict(e) for eid, e in merged.edges.items()],
    }
    return {
        "base_version_id": base_id,
        "current_version_id": current_id,
        "base_is_current": base_id == current_id,
        "keep_id": plan["keep_id"],
        "keep_title": plan["keep_title"],
        "duplicates": plan["duplicates"],
        "candidates": plan["candidates"],
        "candidate_titles": plan["candidate_titles"],
        "field_conflicts": plan["field_conflicts"],
        "resolved_fields": plan["resolved_fields"],
        "edge_moves": plan["edge_moves"],
        "blockers": plan["blockers"],
        "merged_graph_full": graph_full,
    }


# ---------- 写事务落库 ----------

def commit_merge(conn: sqlite3.Connection, body: Any) -> dict[str, Any]:
    """单个 BEGIN IMMEDIATE 事务内重新读取当前版本并重算归并计划；全部通过才生成
    kind='merge' 的新版本快照。任一步失败 → ROLLBACK，不留下版本。"""
    message = body.message.strip()
    new_id: int | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")

        current = graph.current_version_id(conn)
        base_id = body.base_version_id or current
        if base_id != current:
            raise GraphError(
                f"基准版本已过期（页面基于 v{base_id}，当前为 v{current}），请刷新后重新归并",
                "stale_version")
        g = graph.load_graph(conn, current)
        if g is None:
            raise NodeMergeError(f"版本不存在：{current}", "version_not_found", 404)

        # 不信任预览：事务内重新计算整份计划
        plan = build_merge_plan(g, body.keep_id, list(body.duplicate_ids),
                                dict(body.field_resolutions))
        if plan["blockers"]:
            b0 = plan["blockers"][0]
            raise NodeMergeError(
                "归并被拒绝：" + "；".join(b["message"] for b in plan["blockers"]),
                "node_merge_rejected",
                blockers=plan["blockers"],
                cycle=plan["cycle"],
                conflict=plan["time_conflict"],
            )
        unresolved = [c["field"] for c in plan["field_conflicts"] if not c["resolved"]]
        if unresolved:
            raise NodeMergeError(
                "尚有字段冲突未明确取值：" + "、".join(FIELD_LABELS[f] for f in unresolved),
                "unresolved_field_conflict")

        merged: Graph = plan["graph"]
        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'merge', ?)",
            (message, current),
        )
        new_id = cur.lastrowid
        graph.persist_graph(conn, new_id, merged.nodes, merged.edges)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = conn.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    keep_id = plan["keep_id"]
    return {
        "version_id": new_id,
        "kind": "merge",
        "message": message,
        "created_at": row["created_at"],
        "base_version_id": current,
        "keep_id": keep_id,
        "node_map": {nid: keep_id for nid in plan["duplicate_ids"]},
        "removed_node_ids": plan["duplicate_ids"],
        "removed_edge_ids": plan["edge_moves"]["removed_edge_ids"],
        "rewired_edge_ids": [m["edge_id"] for m in plan["edge_moves"]["rewired"]],
        "merged_edge_groups": [
            {"surviving_edge_id": c["surviving_edge_id"],
             "removed_edge_ids": c["removed_edge_ids"]}
            for c in plan["edge_moves"]["collapsed"]
        ],
    }
