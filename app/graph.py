"""图加载、修改集事务校验（含成环检测与时间窗传播）、下游影响计算。

所有校验在内存中先跑一遍：任何一条不通过都抛 GraphError，
由路由层整体回滚事务 —— 已执行的 DELETE/INSERT 不会落库。
"""
from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any

from app.schemas import Changeset, to_utc_iso


class GraphError(Exception):
    def __init__(
        self,
        message: str,
        code: str = "invalid",
        cycle: list[str] | None = None,
        conflict: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.cycle = cycle
        self.conflict = conflict


@dataclass
class Node:
    id: str
    type: str
    title: str
    description: str
    pos_x: float
    pos_y: float
    created_at: str
    earliest_time: str | None = None  # UTC ISO 串（...Z）；仅事件节点
    latest_time: str | None = None
    reliability: float | None = None  # 证据可靠度 0~1；仅证据节点，历史快照可为 None
    source_group: str | None = None   # 证据来源组；仅证据节点
    trigger_mode: str = "ANY"         # 事件触发模式 ANY/ALL；证据节点恒为 ANY（推演时忽略）


@dataclass
class Edge:
    id: str
    source_id: str
    target_id: str
    kind: str
    label: str
    created_at: str


@dataclass
class Graph:
    version_id: int
    nodes: dict[str, Node]
    edges: dict[str, Edge]


# ---------- 读取 ----------

def load_graph(conn: sqlite3.Connection, version_id: int) -> Graph | None:
    if conn.execute("SELECT 1 FROM versions WHERE id = ?", (version_id,)).fetchone() is None:
        return None
    nodes = {
        r["id"]: Node(
            id=r["id"], type=r["type"], title=r["title"], description=r["description"],
            pos_x=r["pos_x"], pos_y=r["pos_y"], created_at=r["created_at"],
            earliest_time=r["earliest_time"], latest_time=r["latest_time"],
            reliability=r["reliability"], source_group=r["source_group"],
            # 旧快照可能没有该列 / 为 NULL：事件节点按默认 ANY
            trigger_mode=r["trigger_mode"] if r["trigger_mode"] in ("ANY", "ALL") else "ANY",
        )
        for r in conn.execute("SELECT * FROM nodes WHERE version_id = ?", (version_id,))
    }
    edges = {
        r["id"]: Edge(r["id"], r["source_id"], r["target_id"], r["kind"], r["label"], r["created_at"])
        for r in conn.execute("SELECT * FROM edges WHERE version_id = ?", (version_id,))
    }
    return Graph(version_id, nodes, edges)


def current_version_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) FROM versions").fetchone()
    return row[0]


# ---------- 修改集校验与应用（纯内存） ----------

def _norm_source_group(group: str | None) -> str | None:
    """来源组以去空格后的字符串为 key；空串 / 纯空白视为「未分组」（独立组）。"""
    if group is None:
        return None
    g = group.strip()
    return g or None


def apply_changes(graph: Graph, changes: Changeset) -> tuple[dict[str, Node], dict[str, Edge]]:
    """在 graph 副本上应用 changes，全部校验通过后返回新的 nodes/edges。

    失败统一抛 GraphError，调用方负责回滚。
    """
    nodes: dict[str, Node] = dict(graph.nodes)
    edges: dict[str, Edge] = dict(graph.edges)

    def node_or_die(nid: str) -> Node:
        n = nodes.get(nid)
        if n is None:
            raise GraphError(f"节点不存在：{nid}", "node_not_found")
        return n

    # 1) 批次内部不能重复操作同一个 id
    if len({n.id for n in changes.node_creates}) != len(changes.node_creates):
        raise GraphError("同一批次中存在重复的新建节点 id", "duplicate_op")
    if len({n.id for n in changes.node_updates}) != len(changes.node_updates):
        raise GraphError("同一批次中存在重复的节点更新", "duplicate_op")
    if len({e.id for e in changes.edge_creates}) != len(changes.edge_creates):
        raise GraphError("同一批次中存在重复的新建连线 id", "duplicate_op")

    # 2) 删除节点（关联边一并级联删除，同批次稍后对这些边的删除允许、更新/新建到这些节点禁止）
    deleted_nodes: set[str] = set()
    for nid in changes.node_deletes:
        node_or_die(nid)
        deleted_nodes.add(nid)
    cascade_edges = {eid for eid, e in edges.items() if e.source_id in deleted_nodes or e.target_id in deleted_nodes}
    for nid in deleted_nodes:
        nodes.pop(nid, None)

    # 3) 删除边
    for eid in changes.edge_deletes:
        if eid in cascade_edges:
            continue  # 节点已删，边级联删除，显式删除属于幂等
        if eid not in edges:
            raise GraphError(f"待删除的连线不存在：{eid}", "edge_not_found")
    for eid in changes.edge_deletes:
        edges.pop(eid, None)
    for eid in cascade_edges:
        edges.pop(eid, None)

    # 4) 更新节点（不能碰到已删除的）
    for upd in changes.node_updates:
        if upd.id in deleted_nodes:
            raise GraphError(f"节点 {upd.id} 在同一批次中既删除又更新", "conflicting_op")
        node = node_or_die(upd.id)
        earliest, latest = node.earliest_time, node.latest_time
        reliability, source_group = node.reliability, node.source_group
        trigger_mode = node.trigger_mode
        # 用 model_fields_set 区分「未提供」与「显式置空（清除界限）」
        provided = upd.model_fields_set
        if "earliest_time" in provided:
            if upd.earliest_time is not None and node.type != "event":
                raise GraphError("只有事件节点可以填写发生时间窗", "time_window_not_allowed")
            earliest = to_utc_iso(upd.earliest_time) if upd.earliest_time is not None else None
        if "latest_time" in provided:
            if upd.latest_time is not None and node.type != "event":
                raise GraphError("只有事件节点可以填写发生时间窗", "time_window_not_allowed")
            latest = to_utc_iso(upd.latest_time) if upd.latest_time is not None else None
        if "reliability" in provided:
            if upd.reliability is not None and node.type != "evidence":
                raise GraphError("只有证据节点可以设置可靠度 reliability", "evidence_field_not_allowed")
            reliability = upd.reliability
        if "source_group" in provided:
            sg = _norm_source_group(upd.source_group)
            if sg is not None and node.type != "evidence":
                raise GraphError("只有证据节点可以设置来源组 source_group", "evidence_field_not_allowed")
            source_group = sg
        if "trigger_mode" in provided:
            if node.type != "event":
                raise GraphError("只有事件节点可以设置触发模式 trigger_mode", "trigger_mode_not_allowed")
            # 显式 null 重置为默认 ANY
            trigger_mode = upd.trigger_mode or "ANY"
        nodes[upd.id] = Node(
            id=node.id,
            type=node.type,
            title=upd.title if upd.title is not None else node.title,
            description=upd.description if upd.description is not None else node.description,
            pos_x=upd.pos_x if upd.pos_x is not None else node.pos_x,
            pos_y=upd.pos_y if upd.pos_y is not None else node.pos_y,
            created_at=node.created_at,
            earliest_time=earliest,
            latest_time=latest,
            reliability=reliability,
            source_group=source_group,
            trigger_mode=trigger_mode,
        )

    # 5) 新建节点
    for cr in changes.node_creates:
        if cr.id in nodes:
            raise GraphError(f"节点 id 已存在：{cr.id}", "node_exists")
        earliest = to_utc_iso(cr.earliest_time) if cr.earliest_time is not None else None
        latest = to_utc_iso(cr.latest_time) if cr.latest_time is not None else None
        if cr.type != "event" and (earliest is not None or latest is not None):
            raise GraphError("只有事件节点可以填写发生时间窗", "time_window_not_allowed")
        source_group = _norm_source_group(cr.source_group)
        if cr.type != "evidence" and (cr.reliability is not None or source_group is not None):
            raise GraphError("只有证据节点可以设置可靠度与来源组", "evidence_field_not_allowed")
        if cr.type != "event" and cr.trigger_mode is not None:
            raise GraphError("只有事件节点可以设置触发模式 trigger_mode", "trigger_mode_not_allowed")
        nodes[cr.id] = Node(
            id=cr.id, type=cr.type, title=cr.title, description=cr.description,
            pos_x=cr.pos_x, pos_y=cr.pos_y, created_at="",
            earliest_time=earliest, latest_time=latest,
            reliability=cr.reliability, source_group=source_group,
            trigger_mode=cr.trigger_mode or "ANY",
        )

    # 6) 更新边（label 任意；kind 仅允许在证据立场 supports/refutes 之间切换）
    for upd in changes.edge_updates:
        edge = edges.get(upd.id)
        if edge is None:
            raise GraphError(f"待更新的连线不存在：{upd.id}", "edge_not_found")
        kind = edge.kind
        if upd.kind is not None and upd.kind != edge.kind:
            if edge.kind == "causes" or upd.kind == "causes":
                raise GraphError("因果连线（causes）不允许修改类型；证据立场切换仅支持 supports ↔ refutes",
                                 "kind_constraint")
            # 立场切换后不能与既有同类型连线重复
            if any(e.id != edge.id and e.source_id == edge.source_id and e.target_id == edge.target_id
                   and e.kind == upd.kind for e in edges.values()):
                raise GraphError("切换立场后与既有同类型连线重复", "duplicate_edge")
            kind = upd.kind
        edges[upd.id] = Edge(edge.id, edge.source_id, edge.target_id, kind,
                             upd.label if upd.label is not None else edge.label, edge.created_at)

    # 7) 新建边
    for cr in changes.edge_creates:
        if cr.id in edges:
            raise GraphError(f"连线 id 已存在：{cr.id}", "edge_exists")
        src, dst = node_or_die(cr.source_id), node_or_die(cr.target_id)
        if src.id == dst.id:
            raise GraphError("不允许节点连接自身", "self_loop", cycle=[src.id])
        # 端点类型约束：causes 必须 event -> event；supports/refutes 必须 evidence -> event
        if cr.kind == "causes" and not (src.type == "event" and dst.type == "event"):
            raise GraphError(f"因果连线 {cr.id} 两端都必须是事件节点", "kind_constraint")
        if cr.kind in ("supports", "refutes") and not (src.type == "evidence" and dst.type == "event"):
            raise GraphError(f"证据连线 {cr.id} 必须从证据指向事件", "kind_constraint")
        # 同一对端点 + 同 kind 不允许重复（避免无意义平行边）
        if any(e.source_id == src.id and e.target_id == dst.id and e.kind == cr.kind for e in edges.values()):
            raise GraphError(f"{src.title} → {dst.title} 已存在同类型连线", "duplicate_edge")
        edges[cr.id] = Edge(cr.id, src.id, dst.id, cr.kind, cr.label, created_at="")

    # 8) 整张图成环检测 —— 在应用完全部变更后的图上跑
    cycle = find_cycle(nodes, edges)
    if cycle:
        names = [nodes[nid].title for nid in cycle if nid in nodes]
        raise GraphError(
            "该变更会使图形成环：" + " → ".join(names + [names[0]] if names else []),
            "cycle",
            cycle=cycle,
        )

    # 9) 时间上下界传播（只走 causes 边；DAG 已由上一步保证）
    windows, conflict = compute_time_windows(nodes, edges)
    if conflict is not None:
        raise GraphError(_conflict_message(nodes, conflict), "time_conflict", conflict=conflict)

    return nodes, edges


def find_cycle(nodes: dict[str, Node], edges: dict[str, Edge]) -> list[str] | None:
    """DFS 三色标记；返回环上的节点 id 列表（首尾不重复），无环返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for e in edges.values():
            if e.source_id != u:
                continue
            v = e.target_id
            if v not in color:
                continue
            if color[v] == GRAY:
                return stack[stack.index(v):]
            if color[v] == WHITE:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = BLACK
        return None

    for nid in nodes:
        if color[nid] == WHITE:
            found = dfs(nid)
            if found:
                return found
    return None


# ---------- 时间窗传播 ----------
#
# 语义：每条 causes 边表示「源事件不晚于目标事件」(t_source <= t_target)。
# 设事件自报下界 earliest / 上界 latest（均可空），则：
#   有效下界 = max(自身 earliest, 全部祖先的 earliest)
#   有效上界 = min(自身 latest,   全部后代的 latest)
# 字符串比较安全：存储统一为 UTC 且格式固定为 YYYY-MM-DDTHH:MM:SSZ，词序即时间序。

def _cause_topo(nodes: dict[str, Node], edges: dict[str, Edge]):
    """在「事件节点 + causes 边」子图上做 Kahn 拓扑排序，返回 (topo, adj, radj, pair_edge)。"""
    events = [nid for nid, n in nodes.items() if n.type == "event"]
    event_set = set(events)
    adj: dict[str, list[str]] = {nid: [] for nid in events}
    radj: dict[str, list[str]] = {nid: [] for nid in events}
    pair_edge: dict[tuple[str, str], str] = {}
    indeg: dict[str, int] = {nid: 0 for nid in events}
    for e in edges.values():
        if e.kind != "causes" or e.source_id not in event_set or e.target_id not in event_set:
            continue
        adj[e.source_id].append(e.target_id)
        radj[e.target_id].append(e.source_id)
        indeg[e.target_id] += 1
        pair_edge[(e.source_id, e.target_id)] = e.id

    q: deque[str] = deque([nid for nid in events if indeg[nid] == 0])
    topo: list[str] = []
    while q:
        u = q.popleft()
        topo.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    return topo, adj, radj, pair_edge


def compute_time_windows(
    nodes: dict[str, Node], edges: dict[str, Edge]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """在整张图上传播时间上下界。

    返回 (windows, conflict)：
    - windows 仅含事件节点，每项给出自身界限、有效上下界、收紧来源与可追溯链路；
    - conflict 为首个「有效下界晚于有效上界」节点的详情，无冲突时为 None。
    """
    topo, adj, radj, pair_edge = _cause_topo(nodes, edges)

    # 前向：祖先下界沿边向下传播，取最大值
    lb = {nid: nodes[nid].earliest_time for nid in topo}
    lb_src = {nid: (nid if lb[nid] is not None else None) for nid in topo}
    lb_path = {nid: ([nid] if lb[nid] is not None else []) for nid in topo}
    for u in topo:
        if lb[u] is None:
            continue
        for v in adj[u]:
            if lb[v] is None or lb[u] > lb[v]:
                lb[v] = lb[u]
                lb_src[v] = lb_src[u]
                lb_path[v] = lb_path[u] + [v]

    # 后向：后代上界沿边向上传播，取最小值
    ub = {nid: nodes[nid].latest_time for nid in topo}
    ub_src = {nid: (nid if ub[nid] is not None else None) for nid in topo}
    ub_path = {nid: ([nid] if ub[nid] is not None else []) for nid in topo}
    for v in reversed(topo):
        if ub[v] is None:
            continue
        for u in radj[v]:
            if ub[u] is None or ub[v] < ub[u]:
                ub[u] = ub[v]
                ub_src[u] = ub_src[v]
                ub_path[u] = [u] + ub_path[v]

    windows: dict[str, dict[str, Any]] = {}
    conflict: dict[str, Any] | None = None
    for nid in topo:
        n = nodes[nid]
        windows[nid] = {
            "earliest_time": n.earliest_time,
            "latest_time": n.latest_time,
            "effective_lower": lb[nid],
            "effective_upper": ub[nid],
            "lower_source_node_id": lb_src[nid],
            "upper_source_node_id": ub_src[nid],
            "lower_chain": lb_path[nid],
            "upper_chain": ub_path[nid],
        }
        if conflict is None and lb[nid] is not None and ub[nid] is not None and lb[nid] > ub[nid]:
            conflict = _build_conflict(nodes, pair_edge, nid, lb, ub, lb_src, ub_src, lb_path, ub_path)
    return windows, conflict


def _build_conflict(nodes, pair_edge, nid, lb, ub, lb_src, ub_src, lb_path, ub_path) -> dict[str, Any]:
    lo_origin, hi_origin = lb_src[nid], ub_src[nid]
    # 合并下界链路（祖先→本节点）与上界链路（本节点→后代），保序去重
    chain: list[str] = []
    for x in list(lb_path[nid]) + list(ub_path[nid]):
        if x not in chain:
            chain.append(x)
    chain_edges: list[str] = []
    for a, b in zip(chain, chain[1:]):
        eid = pair_edge.get((a, b))
        if eid is not None:
            chain_edges.append(eid)
    return {
        "node_id": nid,
        "node_title": nodes[nid].title,
        "lower_bound": lb[nid],
        "upper_bound": ub[nid],
        "lower_node_id": lo_origin,
        "lower_node_title": nodes[lo_origin].title if lo_origin else None,
        "upper_node_id": hi_origin,
        "upper_node_title": nodes[hi_origin].title if hi_origin else None,
        "chain_node_ids": chain,
        "chain_edge_ids": chain_edges,
    }


def _conflict_message(nodes: dict[str, Node], c: dict[str, Any]) -> str:
    if c["lower_node_id"] == c["node_id"] and c["upper_node_id"] == c["node_id"]:
        return (
            f"时间窗冲突：事件「{c['node_title']}」填写的最早时间 {c['lower_bound']} "
            f"晚于最晚时间 {c['upper_bound']}"
        )
    names = " → ".join(nodes[x].title for x in c["chain_node_ids"])
    return (
        f"时间窗冲突：「{c['lower_node_title']}」给出的下界 {c['lower_bound']} 晚于"
        f"「{c['upper_node_title']}」给出的上界 {c['upper_bound']}，"
        f"但因果链 {names} 要求源事件不晚于目标事件"
    )


def time_window_dict(nodes: dict[str, Node], windows: dict[str, dict[str, Any]], nid: str) -> dict[str, Any] | None:
    """序列化单个事件节点的有效时间窗（证据节点返回 None）。"""
    if nodes[nid].type != "event":
        return None
    w = windows[nid]

    def src(sid: str | None) -> dict[str, str] | None:
        return {"node_id": sid, "title": nodes[sid].title} if sid else None

    return {
        "earliest_time": w["earliest_time"],
        "latest_time": w["latest_time"],
        "effective_lower": w["effective_lower"],
        "effective_upper": w["effective_upper"],
        "lower_source": src(w["lower_source_node_id"]),
        "upper_source": src(w["upper_source_node_id"]),
        "lower_chain": w["lower_chain"],
        "upper_chain": w["upper_chain"],
    }


# ---------- 证据强度审查 ----------
#
# 规则：
# - 证据节点带 reliability（0~1）与 source_group；证据连线 supports=支持，refutes=反驳。
# - 同一 source_group 在每个方向（支持/反驳）只采用 reliability 最高的一条，
#   其余标记为「同源未计权」（same_source）；没有 source_group 的证据各自视为独立组。
# - 各独立入选组按独立事件聚合：S = 1 - ∏(1-r)，R = 1 - ∏(1-r)。
# - 判定（按顺序）：
#     S ≥ DISPUTE_MIN 且 R ≥ DISPUTE_MIN → disputed
#     S ≥ STRENGTH_MIN                   → supported
#     R ≥ STRENGTH_MIN                   → refuted
#     其余                               → insufficient
#   历史快照中 reliability 为 NULL 的证据按 0 参与计算（并在序列化时标注 reliability_set=false）。

DISPUTE_MIN = 0.6
STRENGTH_MIN = 0.7
_EPS = 1e-12


@dataclass(frozen=True)
class _Independent:
    """桶键哨兵：未分组证据各自独立成桶（命名组的桶键恒为字符串，不会与之碰撞）。"""
    node_id: str


def _evidence_reliability(node: Node) -> float:
    return node.reliability if node.reliability is not None else 0.0


def _aggregate(ws: list[float]) -> float:
    v = 1.0
    for r in ws:
        v *= 1.0 - r
    return round(max(0.0, 1.0 - v), 4)


def _classify(s: float, r: float) -> str:
    if s >= DISPUTE_MIN - _EPS and r >= DISPUTE_MIN - _EPS:
        return "disputed"
    if s >= STRENGTH_MIN - _EPS:
        return "supported"
    if r >= STRENGTH_MIN - _EPS:
        return "refuted"
    return "insufficient"


def _pick_winner(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """同组同方向取 reliability 最高者；并列时取较小节点 id，保证结果确定。"""
    return min(entries, key=lambda x: (-x["reliability"], x["node_id"]))


def compute_evidence_review(nodes: dict[str, Node], edges: dict[str, Edge]) -> dict[str, dict[str, Any]]:
    """为每个事件节点计算证据强度审查结果，返回 node_id -> review dict。

    review 给出 S/R、状态，以及每个来源组的入选证据与同源未计权证据明细。
    纯函数：按传入的版本数据重算，不修改、不依赖任何旧版以外的状态。
    """
    # event_id -> direction('supports'|'refutes') -> bucket_key -> [entries]
    # bucket_key：命名组用 source_group 字符串；未分组证据各自独立，用 _Independent 哨兵桶。
    buckets: dict[str, dict[str, dict[Any, list[dict[str, Any]]]]] = {}
    for e in edges.values():
        if e.kind not in ("supports", "refutes"):
            continue
        ev, event = nodes.get(e.source_id), nodes.get(e.target_id)
        if ev is None or event is None or ev.type != "evidence" or event.type != "event":
            continue
        dir_map = buckets.setdefault(event.id, {"supports": {}, "refutes": {}})
        bucket_key = ev.source_group if ev.source_group is not None else _Independent(ev.id)
        group_map = dir_map[e.kind].setdefault(bucket_key, [])
        group_map.append({
            "node_id": ev.id,
            "edge_id": e.id,
            "title": ev.title,
            "reliability": _evidence_reliability(ev),
            "reliability_set": ev.reliability is not None,
            "source_group": ev.source_group,
        })

    reviews: dict[str, dict[str, Any]] = {}
    for nid, n in nodes.items():
        if n.type != "event":
            continue
        dir_map = buckets.get(nid)
        groups_out: list[dict[str, Any]] = []
        support_ws: list[float] = []
        refute_ws: list[float] = []
        if dir_map is not None:
            # 命名组按组名稳定排序；独立证据桶统一排最后（桶内按节点 id 排序）
            for direction in ("supports", "refutes"):
                gm = dir_map[direction]
                named_keys = sorted(k for k in gm if isinstance(k, str))
                indep_keys = sorted((k for k in gm if isinstance(k, _Independent)),
                                    key=lambda k: k.node_id)
                for key in named_keys + indep_keys:
                    entries = gm[key]
                    winner = _pick_winner(entries)
                    dropped = [x for x in entries if x["edge_id"] != winner["edge_id"]]
                    if direction == "supports":
                        support_ws.append(winner["reliability"])
                    else:
                        refute_ws.append(winner["reliability"])
                    groups_out.append({
                        "group": winner["source_group"],  # 独立桶输出为 None
                        "direction": direction,
                        "selected": winner,
                        "unweighted": dropped,            # 同源未计权（same_source）
                    })
        s = _aggregate(support_ws)
        r = _aggregate(refute_ws)
        status = _classify(s, r)
        # 扁平化入选 / 未计权清单，方便前端直接渲染
        selected = [g["selected"] for g in groups_out]
        unweighted = [dict(x, reason="same_source", group=g["group"], direction=g["direction"])
                      for g in groups_out for x in g["unweighted"]]
        reviews[nid] = {
            "support_score": s,
            "refute_score": r,
            "status": status,
            "groups": groups_out,
            "selected_evidence": selected,
            "unweighted_evidence": unweighted,
        }
    return reviews


def evidence_review_dict(nodes: dict[str, Node], reviews: dict[str, dict[str, Any]], nid: str) -> dict[str, Any] | None:
    """序列化单个事件节点的证据强度审查（证据节点返回 None）。"""
    if nodes[nid].type != "event":
        return None
    return reviews[nid]


# ---------- 持久化一次提交（调用方已 BEGIN，出错抛出后整体回滚） ----------

def persist_graph(
    conn: sqlite3.Connection,
    new_version_id: int,
    nodes: dict[str, Node],
    edges: dict[str, Edge],
) -> None:
    conn.executemany(
        "INSERT INTO nodes (id, version_id, type, title, description, pos_x, pos_y, "
        "earliest_time, latest_time, reliability, source_group, trigger_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(n.id, new_version_id, n.type, n.title, n.description, n.pos_x, n.pos_y,
          n.earliest_time, n.latest_time, n.reliability, n.source_group,
          n.trigger_mode if n.type == "event" else None) for n in nodes.values()],
    )
    conn.executemany(
        "INSERT INTO edges (id, version_id, source_id, target_id, kind, label) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(e.id, new_version_id, e.source_id, e.target_id, e.kind, e.label) for e in edges.values()],
    )


# ---------- 下游影响（BFS 按层展开） ----------

def downstream_of(graph: Graph, node_id: str) -> dict[str, Any]:
    node = graph.nodes.get(node_id)
    if node is None:
        raise GraphError(f"节点不存在：{node_id}", "node_not_found")

    out: dict[str, list[Edge]] = {nid: [] for nid in graph.nodes}
    for e in graph.edges.values():
        out.setdefault(e.source_id, []).append(e)

    depth: dict[str, int] = {node_id: 0}
    order: list[str] = [node_id]
    q: deque[str] = deque([node_id])
    path_edges: list[dict[str, Any]] = []
    while q:
        u = q.popleft()
        for e in out.get(u, []):
            path_edges.append({
                "id": e.id,
                "source_id": e.source_id,
                "target_id": e.target_id,
                "kind": e.kind,
                "label": e.label,
                "depth": depth[u],
            })
            if e.target_id not in depth:  # DAG 上首次到达即最短层数
                depth[e.target_id] = depth[u] + 1
                order.append(e.target_id)
                q.append(e.target_id)

    layers: list[list[dict[str, Any]]] = []
    for nid in order:
        d = depth[nid]
        if d == 0:
            continue
        n = graph.nodes[nid]
        while len(layers) < d:
            layers.append([])
        layers[d - 1].append({"id": n.id, "type": n.type, "title": n.title})

    affected = [nid for nid in depth if nid != node_id]
    return {
        "node_id": node_id,
        "node_title": node.title,
        "affected_node_ids": affected,
        "total_affected": len(affected),
        "affected_events": sum(1 for nid in affected if graph.nodes[nid].type == "event"),
        "affected_evidence": sum(1 for nid in affected if graph.nodes[nid].type == "evidence"),
        "affected_edges": len(path_edges),
        "max_depth": max((d for d in depth.values()), default=0),
        "layers": layers,
        "edges": path_edges,
    }


# ---------- 反事实干预沙盘 ----------
#
# 只读推演（不产生 changeset / 版本）：
# - 用户勾选若干事件作为「被阻止事件」（干预点），这些事件强制不发生。
# - 其余事件只看「事件 + causes」子图，按拓扑顺序推演：
#     无前因事件         → 默认发生（still_active）
#     trigger_mode=ANY   → 任一前因仍发生即发生，否则被阻止
#     trigger_mode=ALL   → 全部前因均发生才发生，否则被阻止
#   显式干预的事件恒为 prevented，即使自身无前因。
# - 观察目标返回递归依据树：
#     ANY 发生 → 取 ID 最小的仍发生前因作为唯一子节点；
#     ANY 阻止 → 列出全部前因；
#     ALL 发生 → 列出全部前因；
#     ALL 阻止 → 取 ID 最小的已阻止前因作为唯一子节点；
#   无前因事件的依据树无子节点；显式干预点 reason="intervention"，不再展开。

def _event_parents(nodes: dict[str, Node], edges: dict[str, Edge]):
    """返回 (events, parents, parent_edge)：仅「事件 + causes」子图。

    parents[v] 按节点 id 升序，保证「ID 最小」选择与输出顺序确定。
    """
    events = {nid for nid, n in nodes.items() if n.type == "event"}
    parents: dict[str, list[str]] = {nid: [] for nid in events}
    parent_edge: dict[tuple[str, str], str] = {}
    for e in edges.values():
        if e.kind != "causes" or e.source_id not in events or e.target_id not in events:
            continue
        parents[e.target_id].append(e.source_id)
        parent_edge[(e.source_id, e.target_id)] = e.id
    for v in parents:
        parents[v].sort()
    return events, parents, parent_edge


def _event_topo(events: set[str], parents: dict[str, list[str]]) -> list[str]:
    """Kahn 拓扑排序；初始队列按 id 排序，输出与字典遍历顺序无关、可复现。"""
    children: dict[str, list[str]] = {nid: [] for nid in events}
    indeg: dict[str, int] = {nid: 0 for nid in events}
    for v, ps in parents.items():
        indeg[v] = len(ps)
        for u in ps:
            children[u].append(v)
    for u in children:
        children[u].sort()
    q: deque[str] = deque(sorted(nid for nid in events if indeg[nid] == 0))
    topo: list[str] = []
    while q:
        u = q.popleft()
        topo.append(u)
        for v in children[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    return topo


def simulate_counterfactual(graph: Graph, target_id: str, prevented_ids: list[str]) -> dict[str, Any]:
    """在给定版本快照上做反事实干预推演，返回每个事件的状态与目标的递归依据树。"""
    target = graph.nodes.get(target_id)
    if target is None:
        raise GraphError(f"观察目标不存在：{target_id}", "node_not_found")
    if target.type != "event":
        raise GraphError("观察目标必须是事件节点", "target_not_event")

    prevented_set = set(prevented_ids)
    for nid in prevented_set:
        n = graph.nodes.get(nid)
        if n is None:
            raise GraphError(f"被阻止事件不存在：{nid}", "node_not_found")
        if n.type != "event":
            raise GraphError(f"被阻止节点必须是事件：{nid}", "prevented_not_event")

    events, parents, parent_edge = _event_parents(graph.nodes, graph.edges)
    topo = _event_topo(events, parents)
    if len(topo) != len(events):  # 正常图在提交时已保证无环，防御性处理
        raise GraphError("因果图存在环，无法按拓扑顺序推演", "cycle")

    # intervened = 用户显式勾选的干预点；derived = 推演得出的状态
    intervened = {nid for nid in prevented_set if nid in events}
    status: dict[str, str] = {}
    reason: dict[str, str] = {}
    for nid in topo:
        if nid in intervened:
            status[nid] = "prevented"
            reason[nid] = "intervention"
            continue
        ps = parents[nid]
        if not ps:
            status[nid] = "still_active"
            reason[nid] = "no_causes"
            continue
        mode = graph.nodes[nid].trigger_mode if graph.nodes[nid].trigger_mode in ("ANY", "ALL") else "ANY"
        active_parents = [p for p in ps if status[p] == "still_active"]
        prevented_parents = [p for p in ps if status[p] == "prevented"]
        if mode == "ANY":
            if active_parents:
                status[nid] = "still_active"
                reason[nid] = "any_active"
            else:
                status[nid] = "prevented"
                reason[nid] = "all_prevented"
        else:  # ALL
            if not prevented_parents:
                status[nid] = "still_active"
                reason[nid] = "all_active"
            else:
                status[nid] = "prevented"
                reason[nid] = "some_prevented"

    # 依据树涉及的边（子→父的 causes 边），供前端整棵树高亮
    tree_edge_ids: set[str] = set()

    def build_tree(nid: str) -> dict[str, Any]:
        node = graph.nodes[nid]
        st = status[nid]
        node_out = {
            "node_id": nid,
            "title": node.title,
            "trigger_mode": node.trigger_mode if node.trigger_mode in ("ANY", "ALL") else "ANY",
            "status": st,
            "reason": reason[nid],
        }
        if reason[nid] == "intervention" or not parents[nid]:
            node_out["children"] = []
            return node_out

        ps = parents[nid]
        active_parents = [p for p in ps if status[p] == "still_active"]
        prevented_parents = [p for p in ps if status[p] == "prevented"]
        # 规则：ANY 发生取 ID 最小的仍发生前因；ANY 阻止列全部；
        #       ALL 发生列全部；ALL 阻止取 ID 最小的已阻止前因。
        if reason[nid] in ("any_active", "all_active"):
            child_ids = [active_parents[0]] if reason[nid] == "any_active" else ps
        else:
            child_ids = ps if reason[nid] == "all_prevented" else [prevented_parents[0]]

        children: list[dict[str, Any]] = []
        for p in child_ids:
            eid = parent_edge.get((p, nid))
            if eid is not None:
                tree_edge_ids.add(eid)
            children.append(build_tree(p))
        node_out["children"] = children
        return node_out

    tree = build_tree(target_id)

    return {
        "target_id": target_id,
        "target_title": target.title,
        "target_status": status[target_id],
        "version_id": graph.version_id,
        "intervened_ids": sorted(intervened),
        "prevented_ids": sorted(nid for nid in events if status[nid] == "prevented"),
        "still_active_ids": sorted(nid for nid in events if status[nid] == "still_active"),
        "events": [
            {
                "node_id": nid,
                "title": graph.nodes[nid].title,
                "trigger_mode": graph.nodes[nid].trigger_mode
                if graph.nodes[nid].trigger_mode in ("ANY", "ALL") else "ANY",
                "status": status[nid],
                "reason": reason[nid],
                "intervened": nid in intervened,
            }
            for nid in sorted(events)
        ],
        "tree": tree,
        "tree_edge_ids": sorted(tree_edge_ids),
    }
