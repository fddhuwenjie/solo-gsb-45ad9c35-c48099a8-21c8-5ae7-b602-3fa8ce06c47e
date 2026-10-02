"""可复算复盘结论包：导出自包含 payload + SHA-256 摘要，并对上传的包做只读核验。

导出（build_package）：
- 收集根事件、全部 causes 祖先，以及这些事件关联的 supports/refutes 证据，
  连同版本元数据、按包内子图重算的有效时间窗与证据审查结果组成自包含 payload；
- 对象键按字典序、节点与连线按 id 排序，对 payload 的紧凑 UTF-8 JSON 计算 SHA-256。

核验（verify_package）：
- 先按同一规范化方式重算摘要并与随包 digest 比对；
- 再仅凭包内原始节点与连线重建 DAG，重算时间窗与证据审查，
  与随包 results 逐字段比对，任何差异都列出 JSON Pointer、包内值与重算值；
- 摘要匹配且重算一致时 ok=true。

两个方向都纯只读：不写库、不生成版本、不改变工作图。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app import graph

FORMAT = "incident-conclusion-package"
FORMAT_VERSION = 1
DIGEST_ALGORITHM = "sha256"
CANONICALIZATION = "json:utf8,compact,sort_keys,integral-floats-as-int"
MAX_PACKAGE_BYTES = 10 * 1024 * 1024  # 上传核验的体积上限（防御性）
MAX_DIFFS = 200                       # 差异清单上限，超出部分截断并标注


class PackageError(Exception):
    """结论包结构非法（缺少必需部分，或原始节点/连线无法重建 DAG）。"""


# ---------- 规范化与摘要 ----------

def normalize_numbers(obj: Any) -> Any:
    """整数值的浮点（如 60.0）规范化为整数（60）。

    Python 的 json 把 60.0 序列化为 "60.0"，而 jq / JavaScript 序列化为 "60"；
    统一规范化后，摘要可用任意标准 JSON 工具按同一规则复算。
    """
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: normalize_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_numbers(v) for v in obj]
    return obj


def canonical_json_bytes(obj: Any) -> bytes:
    """紧凑、对象键按字典序、数字规范化的 UTF-8 JSON —— 摘要输入以此为准。"""
    return json.dumps(normalize_numbers(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def package_file_bytes(package: dict[str, Any]) -> bytes:
    """导出文件内容：键按字典序、数字规范化的可读 JSON（核验时重新规范化，与排版无关）。"""
    return json.dumps(normalize_numbers(package), ensure_ascii=False, sort_keys=True,
                      indent=2).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- 导出：收集范围并组成自包含 payload ----------

def _collect_scope(g: graph.Graph, root_id: str) -> tuple[dict[str, graph.Node], dict[str, graph.Edge]]:
    """根事件 + 全部 causes 祖先 + 指向这些事件的 supports/refutes 证据（随边纳入证据节点）。"""
    root = g.nodes.get(root_id)
    if root is None:
        raise graph.GraphError(f"事件不存在：{root_id}", "node_not_found")
    if root.type != "event":
        raise graph.GraphError("结论包的根节点必须是事件节点", "root_not_event")

    # 1) 沿 causes 边反向遍历，收集根事件与全部祖先（提交时已保证无环）
    radj: dict[str, list[str]] = {}
    for e in g.edges.values():
        if e.kind == "causes":
            radj.setdefault(e.target_id, []).append(e.source_id)
    event_ids: set[str] = set()
    stack = [root_id]
    while stack:
        u = stack.pop()
        if u in event_ids:
            continue
        event_ids.add(u)
        stack.extend(p for p in radj.get(u, []) if p not in event_ids)

    # 2) 包内连线：祖先锥内的 causes 边 + 指向这些事件的证据边（证据节点随边纳入）
    node_ids = set(event_ids)
    edge_ids: set[str] = set()
    for e in g.edges.values():
        if e.kind == "causes" and e.source_id in event_ids and e.target_id in event_ids:
            edge_ids.add(e.id)
        elif e.kind in ("supports", "refutes") and e.target_id in event_ids:
            edge_ids.add(e.id)
            node_ids.add(e.source_id)
    nodes = {nid: g.nodes[nid] for nid in node_ids if nid in g.nodes}
    edges = {eid: g.edges[eid] for eid in edge_ids}
    return nodes, edges


def _raw_node(n: graph.Node) -> dict[str, Any]:
    """包内原始节点：只含数据字段，不含任何派生结果（时间窗 / 证据审查归入 results）。"""
    return {
        "id": n.id, "type": n.type, "title": n.title, "description": n.description,
        "pos_x": n.pos_x, "pos_y": n.pos_y, "created_at": n.created_at,
        "earliest_time": n.earliest_time, "latest_time": n.latest_time,
        "reliability": n.reliability, "source_group": n.source_group,
        "trigger_mode": n.trigger_mode if n.type == "event" else None,
    }


def _raw_edge(e: graph.Edge) -> dict[str, Any]:
    return {"id": e.id, "source_id": e.source_id, "target_id": e.target_id,
            "kind": e.kind, "label": e.label, "created_at": e.created_at}


def _compute_results(nodes: dict[str, graph.Node], edges: dict[str, graph.Edge]) -> dict[str, Any]:
    """在包内子图上重算有效时间窗与证据审查（导出与核验共用，保证口径一致）。

    节点 / 连线先按 id 排序再传入计算：拓扑传播与分桶遍历顺序随之确定，
    与 dict 构造路径无关，导出端与核验端必然得到逐字节一致的结果。
    """
    nodes = {nid: nodes[nid] for nid in sorted(nodes)}
    edges = {eid: edges[eid] for eid in sorted(edges)}
    windows, _conflict = graph.compute_time_windows(nodes, edges)
    reviews = graph.compute_evidence_review(nodes, edges)
    event_ids = sorted(nid for nid, n in nodes.items() if n.type == "event")
    return {
        "time_windows": {nid: graph.time_window_dict(nodes, windows, nid) for nid in event_ids},
        "evidence_reviews": {nid: graph.evidence_review_dict(nodes, reviews, nid) for nid in event_ids},
    }


def build_package(g: graph.Graph, version_row, root_event_id: str) -> dict[str, Any]:
    """导出结论包：自包含 payload + 其紧凑 UTF-8 JSON 的 SHA-256 摘要。"""
    nodes, edges = _collect_scope(g, root_event_id)
    payload = {
        "meta": {
            "root_event_id": root_event_id,
            "root_event_title": nodes[root_event_id].title,
            "version_id": g.version_id,
            "version_kind": version_row["kind"],
            "version_message": version_row["message"],
            "version_base_id": version_row["base_id"],
            "version_created_at": version_row["created_at"],
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        "nodes": [_raw_node(nodes[nid]) for nid in sorted(nodes)],
        "edges": [_raw_edge(edges[eid]) for eid in sorted(edges)],
        "results": _compute_results(nodes, edges),
    }
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "digest": {
            "algorithm": DIGEST_ALGORITHM,
            "canonicalization": CANONICALIZATION,
            "value": sha256_hex(canonical_json_bytes(payload)),
        },
        "payload": payload,
    }


# ---------- 核验：仅凭包内数据重建 DAG 并重算比对 ----------

def _opt_str(v: Any, field: str) -> str | None:
    if v is None or isinstance(v, str):
        return v
    raise PackageError(f"节点字段 {field} 必须是字符串或 null")


def _rebuild_graph(payload: dict[str, Any]) -> tuple[dict[str, graph.Node], dict[str, graph.Edge]]:
    """仅凭包内原始节点与连线重建内存图（不接触数据库）。"""
    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise PackageError("payload.nodes / payload.edges 必须是数组")

    nodes: dict[str, graph.Node] = {}
    for i, nd in enumerate(raw_nodes):
        if not isinstance(nd, dict) or not isinstance(nd.get("id"), str) \
                or nd.get("type") not in ("event", "evidence"):
            raise PackageError(f"payload.nodes[{i}] 缺少合法的 id / type")
        nid = nd["id"]
        if nid in nodes:
            raise PackageError(f"payload.nodes 中 id 重复：{nid}")
        rel = nd.get("reliability")
        if rel is not None and (isinstance(rel, bool) or not isinstance(rel, (int, float))):
            raise PackageError(f"节点 {nid} 的 reliability 必须是 0~1 的数值或 null")
        tm = nd.get("trigger_mode")
        nodes[nid] = graph.Node(
            id=nid, type=nd["type"],
            title=str(nd.get("title") or ""), description=str(nd.get("description") or ""),
            pos_x=float(nd.get("pos_x") or 0), pos_y=float(nd.get("pos_y") or 0),
            created_at=str(nd.get("created_at") or ""),
            earliest_time=_opt_str(nd.get("earliest_time"), "earliest_time"),
            latest_time=_opt_str(nd.get("latest_time"), "latest_time"),
            reliability=rel, source_group=_opt_str(nd.get("source_group"), "source_group"),
            trigger_mode=tm if tm in ("ANY", "ALL") else "ANY",
        )

    edges: dict[str, graph.Edge] = {}
    for i, ed in enumerate(raw_edges):
        if not isinstance(ed, dict) or not isinstance(ed.get("id"), str) \
                or ed.get("kind") not in ("causes", "supports", "refutes"):
            raise PackageError(f"payload.edges[{i}] 缺少合法的 id / kind")
        eid = ed["id"]
        if eid in edges:
            raise PackageError(f"payload.edges 中 id 重复：{eid}")
        edges[eid] = graph.Edge(
            eid, str(ed.get("source_id") or ""), str(ed.get("target_id") or ""),
            ed["kind"], str(ed.get("label") or ""), str(ed.get("created_at") or ""),
        )
    return nodes, edges


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _json_pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _diff_value(packaged: Any, recomputed: Any, path: str, out: list[dict[str, Any]]) -> None:
    """递归逐字段比对；任何差异记录 JSON Pointer、包内值与重算值（缺失侧为 None）。"""
    if len(out) >= MAX_DIFFS:
        return
    if isinstance(packaged, dict) and isinstance(recomputed, dict):
        for key in sorted(set(packaged) | set(recomputed)):
            p = f"{path}/{_json_pointer_escape(str(key))}"
            if key not in packaged:
                out.append({"pointer": p, "packaged": None, "recomputed": recomputed[key]})
            elif key not in recomputed:
                out.append({"pointer": p, "packaged": packaged[key], "recomputed": None})
            else:
                _diff_value(packaged[key], recomputed[key], p, out)
        return
    if isinstance(packaged, list) and isinstance(recomputed, list):
        if len(packaged) != len(recomputed):
            out.append({"pointer": path or "/", "packaged": packaged, "recomputed": recomputed})
            return
        for i, (a, b) in enumerate(zip(packaged, recomputed)):
            _diff_value(a, b, f"{path}/{i}", out)
        return
    if _is_number(packaged) and _is_number(recomputed):
        if packaged != recomputed:  # 1 与 1.0 视为相等（JSON 不区分）
            out.append({"pointer": path or "/", "packaged": packaged, "recomputed": recomputed})
        return
    if packaged != recomputed or type(packaged) is not type(recomputed):
        out.append({"pointer": path or "/", "packaged": packaged, "recomputed": recomputed})


def verify_package(doc: Any) -> dict[str, Any]:
    """只读核验上传的结论包：先校验摘要，再重建 DAG 重算并逐字段比对。"""
    if not isinstance(doc, dict):
        raise PackageError("文件内容不是 JSON 对象")
    if doc.get("format") != FORMAT:
        raise PackageError(f"format 应为 {FORMAT!r}")
    if doc.get("format_version") != FORMAT_VERSION:
        raise PackageError(f"不支持的 format_version：{doc.get('format_version')!r}")
    digest = doc.get("digest")
    if not isinstance(digest, dict) or not isinstance(digest.get("value"), str):
        raise PackageError("缺少 digest.value")
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        raise PackageError("缺少 payload 对象")

    # 1) 摘要校验：对 payload 的紧凑 UTF-8 JSON（对象键按字典序）重算 SHA-256
    actual = sha256_hex(canonical_json_bytes(payload))
    expected = digest["value"].strip().lower()
    digest_match = bool(expected) and actual == expected

    # 2) 仅凭包内原始节点与连线重建 DAG，重算时间窗与证据审查并逐字段比对
    diffs: list[dict[str, Any]] = []
    rebuild_error: str | None = None
    event_count = 0
    try:
        nodes, edges = _rebuild_graph(payload)
        event_count = sum(1 for n in nodes.values() if n.type == "event")
        packaged_results = payload.get("results")
        if not isinstance(packaged_results, dict):
            raise PackageError("payload.results 缺失或不是对象")
        recomputed = _compute_results(nodes, edges)
        _diff_value(packaged_results, recomputed, "/results", diffs)
    except PackageError as exc:
        rebuild_error = str(exc)

    consistent = rebuild_error is None and not diffs
    return {
        "ok": digest_match and consistent,
        "digest": {
            "algorithm": DIGEST_ALGORITHM,
            "expected": digest["value"],
            "actual": actual,
            "match": digest_match,
        },
        "recompute": {
            "consistent": consistent,
            "error": rebuild_error,
            "events_checked": event_count,
            "diff_count": len(diffs),
            "diffs_truncated": len(diffs) >= MAX_DIFFS,
            "diffs": diffs,
        },
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else None,
    }
