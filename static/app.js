/* 事故复盘因果图 —— 原生 JS 客户端
 *
 * 本地维护一份"工作图"(S.work)，所有编辑先落在本地并累计到 changeset；
 * 点「提交一组修改」后整包发给 /api/changes/commit，后端在一个事务里
 * 校验整张图（含成环检测），任一不通过则整批拒绝。
 */
'use strict';

const SVGNS = 'http://www.w3.org/2000/svg';
const NODE_W = 176, NODE_H = 56, PORT_R = 6;

const S = {
  committed: null,        // 服务器当前快照 {version_id, nodes:[], edges:[]}
  work: null,             // 工作图 {version_id, nodes:Map, edges:Map}（Map 保序）
  changes: newChangeSet(),
  selected: null,         // {kind:'node'|'edge', id}
  view: { x: 40, y: 30, k: 1 },
  addType: 'event',
  viewingVersion: null,   // 非 null 时处于只读历史浏览
  preview: null,          // 非 null 时处于提案只读预览 {kind:'proposal'|'merge'|'node_merge', ...}
  mergeHits: { nodes: new Set(), edges: new Set() },  // 合并冲突定位高亮
  nodeMerge: null,        // 非 null 时为重复事件归并的冲突路径定位高亮 {nodes,edges}
  affected: { nodes: new Set(), edges: new Set() },
  conflictHits: { nodes: new Set(), edges: new Set(), data: null },  // 服务端返回的时间窗冲突链
  localConflictHits: { nodes: new Set(), edges: new Set(), data: null },  // 未提交工作图上的即时冲突
  localReviews: null,       // 未提交工作图上实时重算的证据强度审查（Map），干净视图用服务端结果
  // 反事实干预沙盘（只读）：active=面板是否已配置推演；result=最近一次推演结果
  sandbox: {
    active: false,
    targetId: null,
    prevented: new Set(),
    result: null,
    local: false,
    loading: false,
    sig: '',
    seq: 0,
  },
};

function emptyHits() { return { nodes: new Set(), edges: new Set(), data: null }; }

/** 规范化节点：旧快照可能没有 trigger_mode，事件节点按默认 ANY。 */
function normalizeNode(n) {
  if (n.type === 'event' && n.trigger_mode !== 'ANY' && n.trigger_mode !== 'ALL') n.trigger_mode = 'ANY';
  if (n.type === 'evidence' && n.trigger_mode === undefined) n.trigger_mode = null;
  return n;
}

function recomputeLocalConflict() {
  if (isReadOnly() || !S.work) { S.localConflictHits = emptyHits(); return; }
  const { conflict } = computeTimeWindows(S.work.nodes, S.work.edges);
  S.localConflictHits = conflict
    ? { nodes: new Set(conflict.chain_node_ids), edges: new Set(conflict.chain_edge_ids), data: conflict }
    : emptyHits();
}

function newChangeSet() {
  return { node_creates: [], node_updates: [], node_deletes: [],
           edge_creates: [], edge_updates: [], edge_deletes: [] };
}

// 只读模式：历史版本浏览，或提案 / 合并结果预览
const isReadOnly = () => !!S.viewingVersion || !!S.preview;
// 本地脏工作图（非只读且有未提交修改）——时间窗 / 证据审查 / 沙盘据此走前端同构重算
const isDirtyEditing = () => !isReadOnly() && changeCount() > 0;

// ---------- 时间窗工具 ----------
// 线与存储统一为 UTC（...Z）；用户可输入任意带时区偏移的 ISO 8601，空串表示不限。
function parseTimeInput(raw) {
  const s = String(raw || '').trim();
  if (!s) return { ok: true, value: null };
  const norm = s.endsWith('Z') || s.endsWith('z') ? s.slice(0, -1) + '+00:00' : s;
  const d = new Date(norm);
  if (isNaN(d.getTime())) return { ok: false, error: '时间格式无法识别，请用如 2026-09-20T10:00:00+08:00' };
  // Date 能解析无时区字符串（按本地时区），这里显式要求带时区
  if (!/([zZ]$|[+-]\d{2}:?\d{2}$)/.test(s)) {
    return { ok: false, error: '必须带时区，例如 2026-09-20T10:00:00+08:00 或 ...Z' };
  }
  return { ok: true, value: d.toISOString().replace(/\.\d{3}Z$/, 'Z') };
}

const fmtTime = (t) => t ? t.replace('T', ' ').replace('Z', ' UTC') : '不限';
const fmtTimeShort = (t) => {
  if (!t) return '';
  const m = t.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : t.slice(11, 16);
};

const $ = (id) => document.getElementById(id);
const svg = $('svg'), viewport = $('viewport');
const nodeLayer = $('nodeLayer'), edgeLayer = $('edgeLayer'), tempLayer = $('tempLayer');
const canvasWrap = $('canvasWrap');

const uid = (p) => p + '-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const trunc = (s, n = 13) => (s.length > n ? s.slice(0, n) + '…' : s);

// ---------- API ----------

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let message = data.detail;
    if (Array.isArray(message)) {
      // Pydantic 422：[{loc, msg, ...}]
      message = message.map((d) => {
        const field = d.loc?.filter((p) => p !== 'changes' && !Number.isInteger(p)).join('.');
        return `${field ? field + '：' : ''}${d.msg}`;
      }).join('；');
    }
    const err = new Error(message || `请求失败 (${res.status})`);
    err.payload = data;
    throw err;
  }
  return data;
}

// ---------- 数据装载 ----------

function snapshotFromServer(g) {
  return {
    version_id: g.version_id,
    nodes: new Map(g.nodes.map((n) => [n.id, normalizeNode({ ...n })])),
    edges: new Map(g.edges.map((e) => [e.id, { ...e }])),
  };
}

async function loadState() {
  const g = await api('/api/state');
  S.committed = g;
  S.work = snapshotFromServer(g);
  S.changes = newChangeSet();
  S.selected = null;
  S.viewingVersion = null;
  S.preview = null;
  S.mergeHits = { nodes: new Set(), edges: new Set() };
  S.nodeMerge = null;
  S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
  clearAffected();
  resetSandbox();
  renderAll();
  await refreshVersions();
  void loadReviews();
  void loadProposals();
  nodeMergeUI.reset();
}

// ---------- changeset 记账 ----------

const isCreated = (id) => S.changes.node_creates.some((n) => n.id === id)
  || S.changes.edge_creates.some((e) => e.id === id);

function stageNodeCreate(node) {
  S.changes.node_creates.push({
    id: node.id, type: node.type, title: node.title,
    description: node.description, pos_x: node.pos_x, pos_y: node.pos_y,
    earliest_time: node.earliest_time, latest_time: node.latest_time,
    reliability: node.reliability, source_group: node.source_group,
    trigger_mode: node.type === 'event' ? node.trigger_mode : undefined,
  });
}

function stageNodeUpdate(id, patch) {
  const cr = S.changes.node_creates.find((n) => n.id === id);
  if (cr) { Object.assign(cr, patch); return; }
  let upd = S.changes.node_updates.find((n) => n.id === id);
  if (!upd) { upd = { id }; S.changes.node_updates.push(upd); }
  Object.assign(upd, patch);
}

/** 修改时间界限：null 表示清除该界限（需显式传给后端）。 */
function stageTimeField(id, field, value) {
  const n = S.work.nodes.get(id);
  n[field] = value;
  if (S.changes.node_creates.some((c) => c.id === id)) {
    const cr = S.changes.node_creates.find((c) => c.id === id);
    cr[field] = value;
  } else {
    let upd = S.changes.node_updates.find((u) => u.id === id);
    if (!upd) { upd = { id }; S.changes.node_updates.push(upd); }
    upd[field] = value;  // 显式 null 也会序列化，后端据 fields_set 清除
  }
}

function stageNodeDelete(id) {
  const ci = S.changes.node_creates.findIndex((n) => n.id === id);
  if (ci >= 0) { S.changes.node_creates.splice(ci, 1); return; } // 本次新建：直接丢弃
  S.changes.node_updates = S.changes.node_updates.filter((n) => n.id !== id);
  if (!S.changes.node_deletes.includes(id)) S.changes.node_deletes.push(id);
  // 级联记账：相连边（同时清掉对这些边的更新记录）
  for (const [eid, e] of S.work.edges) {
    if (e.source_id === id || e.target_id === id) {
      S.changes.edge_updates = S.changes.edge_updates.filter((u) => u.id !== eid);
      stageEdgeDelete(eid, true);
    }
  }
}

function stageEdgeCreate(edge) {
  S.changes.edge_creates.push({
    id: edge.id, source_id: edge.source_id, target_id: edge.target_id,
    kind: edge.kind, label: edge.label,
  });
}

function stageEdgeDelete(id, cascading = false) {
  const ci = S.changes.edge_creates.findIndex((e) => e.id === id);
  if (ci >= 0) { S.changes.edge_creates.splice(ci, 1); return; }
  S.changes.edge_updates = S.changes.edge_updates.filter((e) => e.id !== id);
  if (!cascading && !S.changes.edge_deletes.includes(id)) S.changes.edge_deletes.push(id);
  else if (cascading) {
    // 节点删除引起：只在该边原本属于旧快照时才需要告诉后端
    const existedInBase = S.committed.edges.some((e) => e.id === id)
      && !S.changes.edge_deletes.includes(id);
    if (existedInBase) S.changes.edge_deletes.push(id);
  }
}

function stageEdgeUpdate(id, patch) {
  const cr = S.changes.edge_creates.find((e) => e.id === id);
  if (cr) { Object.assign(cr, patch); return; }
  let upd = S.changes.edge_updates.find((e) => e.id === id);
  if (!upd) { upd = { id }; S.changes.edge_updates.push(upd); }
  Object.assign(upd, patch);
}

function changeCount() {
  return S.changes.node_creates.length + S.changes.node_updates.length + S.changes.node_deletes.length
    + S.changes.edge_creates.length + S.changes.edge_updates.length + S.changes.edge_deletes.length;
}

// ---------- 本地图操作 ----------

function addNodeLocal(type, title, description, reliability, sourceGroup) {
  const rect = canvasWrap.getBoundingClientRect();
  const p = screenToWorld(rect.width / 2 - NODE_W / 2, rect.height / 2 - NODE_H / 2);
  const node = {
    id: uid(type === 'event' ? 'ev' : 'evd'),
    type, title, description: description || '',
    pos_x: Math.round(p.x), pos_y: Math.round(p.y), created_at: '',
    earliest_time: null, latest_time: null,
    reliability: type === 'evidence' ? reliability : null,
    source_group: type === 'evidence' ? (sourceGroup || null) : null,
    trigger_mode: type === 'event' ? 'ANY' : null,
  };
  S.work.nodes.set(node.id, node);
  stageNodeCreate(node);
  S.selected = { kind: 'node', id: node.id };
  afterLocalEdit();
}

function moveNodeLocal(id, x, y) {
  const n = S.work.nodes.get(id);
  n.pos_x = Math.round(x); n.pos_y = Math.round(y);
  stageNodeUpdate(id, { pos_x: n.pos_x, pos_y: n.pos_y });
}

function deleteSelected() {
  if (!S.selected || isReadOnly()) return;
  if (S.selected.kind === 'node') {
    stageNodeDelete(S.selected.id);
    // 同步工作图（级联删边）
    for (const [eid, e] of Array.from(S.work.edges)) {
      if (e.source_id === S.selected.id || e.target_id === S.selected.id) S.work.edges.delete(eid);
    }
    S.work.nodes.delete(S.selected.id);
  } else {
    stageEdgeDelete(S.selected.id);
    S.work.edges.delete(S.selected.id);
  }
  S.selected = null;
  afterLocalEdit();
}

function afterLocalEdit() {
  clearAffected();
  S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
  renderAll();
}

function discardChanges() {
  if (!changeCount()) return;
  S.work = snapshotFromServer(S.committed);
  S.changes = newChangeSet();
  S.selected = null;
  clearAffected();
  hideError();
  renderAll();
  // 放弃未提交后沙盘需回到「按当前版本服务端推演」的结果
  if (S.sandbox.active && S.sandbox.targetId) void syncSandbox();
  toast('已放弃全部未提交修改');
}

// ---------- 坐标 / 几何 ----------

function toWorld(clientX, clientY) {
  const r = svg.getBoundingClientRect();
  return { x: (clientX - r.left - S.view.x) / S.view.k, y: (clientY - r.top - S.view.y) / S.view.k };
}
const screenToWorld = toWorld;

function anchorPoint(node, side) {
  return side === 'right'
    ? { x: node.pos_x + NODE_W, y: node.pos_y + NODE_H / 2 }
    : { x: node.pos_x, y: node.pos_y + NODE_H / 2 };
}

/** 计算从源节点右边缘到目标节点矩形边缘的起止点 */
function edgeEndpoints(e) {
  const src = S.work.nodes.get(e.source_id);
  const dst = S.work.nodes.get(e.target_id);
  if (!src || !dst) return null;
  const sc = { x: src.pos_x + NODE_W / 2, y: src.pos_y + NODE_H / 2 };
  const dc = { x: dst.pos_x + NODE_W / 2, y: dst.pos_y + NODE_H / 2 };
  return { p1: borderPoint(src, Math.atan2(dc.y - sc.y, dc.x - sc.x)),
           p2: borderPoint(dst, Math.atan2(sc.y - dc.y, sc.x - dc.x)) };
}

/** 从节点中心沿角度 theta 与矩形边框的交点 */
function borderPoint(n, theta) {
  const hw = NODE_W / 2, hh = NODE_H / 2;
  const c = Math.abs(Math.cos(theta)), s = Math.abs(Math.sin(theta));
  const scale = Math.min(hw / (c || 1e-9), hh / (s || 1e-9));
  return { x: n.pos_x + NODE_W / 2 + Math.cos(theta) * scale,
           y: n.pos_y + NODE_H / 2 + Math.sin(theta) * scale };
}

function edgePath(e) {
  const ep = edgeEndpoints(e);
  if (!ep) return '';
  const { p1, p2 } = ep;
  const dx = Math.max(60, Math.abs(p2.x - p1.x) * 0.5);
  return `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
}

// ---------- 时间窗传播（与后端 graph.compute_time_windows 同构） ----------
// 每条 causes 边表示源事件不晚于目标事件：
//   有效下界 = max(自身与全部祖先的 earliest)，有效上界 = min(自身与全部后代的 latest)。
function computeTimeWindows(nodes, edges) {
  const events = [...nodes.values()].filter((n) => n.type === 'event').map((n) => n.id);
  const evSet = new Set(events);
  const adj = new Map(events.map((id) => [id, []]));
  const radj = new Map(events.map((id) => [id, []]));
  const pairEdge = new Map();
  const indeg = new Map(events.map((id) => [id, 0]));
  for (const e of edges.values()) {
    if (e.kind !== 'causes' || !evSet.has(e.source_id) || !evSet.has(e.target_id)) continue;
    adj.get(e.source_id).push(e.target_id);
    radj.get(e.target_id).push(e.source_id);
    indeg.set(e.target_id, indeg.get(e.target_id) + 1);
    pairEdge.set(`${e.source_id}->${e.target_id}`, e.id);
  }
  const q0 = events.filter((id) => indeg.get(id) === 0);
  const topo = [];
  while (q0.length) {
    const u = q0.shift(); topo.push(u);
    for (const v of adj.get(u)) {
      indeg.set(v, indeg.get(v) - 1);
      if (indeg.get(v) === 0) q0.push(v);
    }
  }
  const hasCycle = topo.length !== events.length;

  const lb = new Map(), lbSrc = new Map(), lbPath = new Map();
  for (const id of topo) {
    const t = nodes.get(id).earliest_time || null;
    lb.set(id, t); lbSrc.set(id, t ? id : null); lbPath.set(id, t ? [id] : []);
  }
  for (const u of topo) {
    if (!lb.get(u)) continue;
    for (const v of adj.get(u)) {
      if (lb.get(v) == null || lb.get(u) > lb.get(v)) {
        lb.set(v, lb.get(u)); lbSrc.set(v, lbSrc.get(u));
        lbPath.set(v, [...lbPath.get(u), v]);
      }
    }
  }
  const ub = new Map(), ubSrc = new Map(), ubPath = new Map();
  for (const id of topo) {
    const t = nodes.get(id).latest_time || null;
    ub.set(id, t); ubSrc.set(id, t ? id : null); ubPath.set(id, t ? [id] : []);
  }
  for (let i = topo.length - 1; i >= 0; i--) {
    const v = topo[i];
    if (!ub.get(v)) continue;
    for (const u of radj.get(v)) {
      if (ub.get(u) == null || ub.get(v) < ub.get(u)) {
        ub.set(u, ub.get(v)); ubSrc.set(u, ubSrc.get(v));
        ubPath.set(u, [u, ...ubPath.get(v)]);
      }
    }
  }

  const windows = new Map();
  let conflict = null;
  for (const id of topo) {
    windows.set(id, {
      lower: lb.get(id), upper: ub.get(id),
      lowerSrc: lbSrc.get(id), upperSrc: ubSrc.get(id),
      lowerChain: lbPath.get(id), upperChain: ubPath.get(id),
    });
    if (!conflict && lb.get(id) != null && ub.get(id) != null && lb.get(id) > ub.get(id)) {
      const chain = [];
      for (const x of [...lbPath.get(id), ...ubPath.get(id)]) if (!chain.includes(x)) chain.push(x);
      const chainEdges = [];
      for (let k = 0; k < chain.length - 1; k++) {
        const eid = pairEdge.get(`${chain[k]}->${chain[k + 1]}`);
        if (eid) chainEdges.push(eid);
      }
      conflict = {
        node_id: id, lower_bound: lb.get(id), upper_bound: ub.get(id),
        lower_node_id: lbSrc.get(id), upper_node_id: ubSrc.get(id),
        chain_node_ids: chain, chain_edge_ids: chainEdges,
      };
    }
  }
  return { windows, conflict, hasCycle };
}

/** 当前工作图上的传播结果（未提交时实时反映本地编辑）。 */
function currentTimeInfo() {
  return computeTimeWindows(S.work.nodes, S.work.edges);
}

/** 选中事件实际展示的有效时间窗：历史/干净视图用服务端，脏工作图用本地重算。 */
function eventWindowFor(nodeId) {
  const n = S.work.nodes.get(nodeId);
  if (!n || n.type !== 'event') return null;
  if (isDirtyEditing()) {
    return currentTimeInfo().windows.get(nodeId) || null;
  }
  return n.time_window || null;  // 历史版本 / 提案预览均由服务端按所看图重算
}

// ---------- 证据强度审查（与后端 graph.compute_evidence_review 同构） ----------
// 同一 source_group 在每个方向只采用 reliability 最高的一条，其余同源未计权；
// 独立组分别按 S=1-∏(1-r)、R=1-∏(1-r) 聚合；S/R≥0.6 判争议，S≥0.7 判支持，R≥0.7 判反驳。
const DISPUTE_MIN = 0.6, STRENGTH_MIN = 0.7, REV_EPS = 1e-12;

function computeEvidenceReview(nodes, edges) {
  // eventId -> { supports: Map<bucketKey, entries>, refutes: Map<bucketKey, entries> }
  // bucketKey：命名组用 source_group；未分组证据各自独立，用节点 id 作为桶键。
  const buckets = new Map();
  for (const e of edges.values()) {
    if (e.kind !== 'supports' && e.kind !== 'refutes') continue;
    const ev = nodes.get(e.source_id), event = nodes.get(e.target_id);
    if (!ev || !event || ev.type !== 'evidence' || event.type !== 'event') continue;
    if (!buckets.has(event.id)) buckets.set(event.id, { supports: new Map(), refutes: new Map() });
    const gm = buckets.get(event.id)[e.kind];
    // 命名组桶键为字符串；未分组证据各自独立，用对象哨兵作桶键（与字符串不会碰撞）
    const key = ev.source_group || { independent: ev.id };
    if (!gm.has(key)) gm.set(key, []);
    gm.get(key).push({
      node_id: ev.id, edge_id: e.id, title: ev.title,
      reliability: ev.reliability ?? 0,
      reliability_set: ev.reliability != null,
      source_group: ev.source_group || null,
    });
  }
  const agg = (ws) => Math.round((1 - ws.reduce((v, r) => v * (1 - r), 1)) * 10000) / 10000;
  const reviews = new Map();
  for (const n of nodes.values()) {
    if (n.type !== 'event') continue;
    const dirMap = buckets.get(n.id);
    const groups = [], sWs = [], rWs = [];
    if (dirMap) {
      for (const direction of ['supports', 'refutes']) {
        const gm = dirMap[direction];
        // 命名组按组名排序，独立证据桶按节点 id 排序并统一排最后，与后端输出顺序一致
        const namedKeys = [...gm.keys()].filter((k) => typeof k === 'string').sort();
        const indepKeys = [...gm.keys()].filter((k) => typeof k === 'object')
          .sort((a, b) => (a.independent < b.independent ? -1 : a.independent > b.independent ? 1 : 0));
        for (const key of [...namedKeys, ...indepKeys]) {
          const entries = gm.get(key);
          // 组内取 reliability 最高者；并列取较小节点 id，保证与后端一致
          const winner = entries.reduce((a, b) =>
            (b.reliability > a.reliability
              || (b.reliability === a.reliability && b.node_id < a.node_id)) ? b : a);
          const dropped = entries.filter((x) => x.edge_id !== winner.edge_id);
          (direction === 'supports' ? sWs : rWs).push(winner.reliability);
          groups.push({ group: winner.source_group, direction, selected: winner, unweighted: dropped });
        }
      }
    }
    const s = agg(sWs), r = agg(rWs);
    const status = (s >= DISPUTE_MIN - REV_EPS && r >= DISPUTE_MIN - REV_EPS) ? 'disputed'
      : s >= STRENGTH_MIN - REV_EPS ? 'supported'
      : r >= STRENGTH_MIN - REV_EPS ? 'refuted' : 'insufficient';
    reviews.set(n.id, {
      support_score: s, refute_score: r, status, groups,
      selected_evidence: groups.map((g) => g.selected),
      unweighted_evidence: groups.flatMap((g) =>
        g.unweighted.map((x) => ({ ...x, reason: 'same_source', group: g.group, direction: g.direction }))),
    });
  }
  return reviews;
}

/** 选中事件实际展示的审查结果：脏工作图用本地重算，否则用服务端按所查看版本算好的。 */
function reviewFor(nodeId) {
  const n = S.work.nodes.get(nodeId);
  if (!n || n.type !== 'event') return null;
  if (S.localReviews) return S.localReviews.get(nodeId) || null;
  return n.evidence_review || null;
}

const STATUS_META = {
  supported: ['已支持', 'supported'],
  refuted: ['已反驳', 'refuted'],
  disputed: ['存在争议', 'disputed'],
  insufficient: ['证据不足', 'insufficient'],
};

// ---------- 渲染 ----------

function applyView() {
  viewport.setAttribute('transform', `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
}

function renderAll() {
  recomputeLocalConflict();
  // 未提交工作图：证据强度审查实时重算；干净视图 / 历史快照用服务端按各自版本算好的结果
  S.localReviews = isDirtyEditing()
    ? computeEvidenceReview(S.work.nodes, S.work.edges) : null;
  // 先同步沙盘控件（结构变化时重建列表并丢弃过期依据树），再按需本地即时推演
  pruneSandboxSelection();
  renderSandboxControls();
  renderPkgControls();
  renderNodeMergeControls();
  if (!S.sandbox.active) {
    $('sandboxResult').innerHTML =
      '<p class="muted small">选择目标并勾选干预点后即时推演；未提交工作图即时生效。</p>';
  } else if (sandboxIsLocal() && S.sandbox.targetId) {
    try {
      S.sandbox.result = computeCounterfactual(S.work.nodes, S.work.edges,
        S.sandbox.targetId, [...S.sandbox.prevented]);
      S.sandbox.local = true;
    } catch (_) { /* 非法结构（如成环）时沿用上一次结果 */ }
  }
  renderHeader();
  renderEdges();
  renderNodes();
  renderDetail();
  renderSandboxResult();
  applyView();
}

function renderHeader() {
  const label = S.viewingVersion ? `只读 v${S.viewingVersion}`
    : S.preview ? S.preview.badge || '提案预览'
    : `v${S.work.version_id}`;
  $('versionBadge').textContent = label;
  const n = changeCount();
  $('dirtyCount').textContent = (!isReadOnly() && n) ? `● ${n} 项未提交` : '';
  $('btnCommit').disabled = isReadOnly() || !n || !$('commitMessage').value.trim();
  $('btnDiscard').disabled = isReadOnly() || !n;
  const btnPpSave = document.getElementById('btnPpSave');
  if (btnPpSave) btnPpSave.disabled = isReadOnly() || !n;
  $('viewBanner').hidden = !S.viewingVersion && !S.preview;
  if (S.viewingVersion) {
    $('viewBannerText').textContent = `正在浏览历史快照 v${S.viewingVersion}（只读）。当前最新版本为 v${S.committed.version_id}。`;
    $('btnBackToCurrent').textContent = '返回当前版';
    $('btnRollbackView').hidden = false;
    $('btnBackToMerge').hidden = true;
  } else if (S.preview) {
    const reopen = (S.preview.kind === 'merge' && proposalUI.detail)
      || (S.preview.kind === 'node_merge' && nodeMergeUI.detail);
    $('viewBannerText').textContent = S.preview.bannerText || '正在预览提案结果（只读，未落库）。';
    $('btnBackToCurrent').textContent = '退出预览';
    $('btnRollbackView').hidden = true;
    $('btnBackToMerge').hidden = !reopen;
    $('btnBackToMerge').textContent = S.preview.kind === 'node_merge' ? '返回归并预览' : '返回裁决';
  }
}

function renderNodes() {
  nodeLayer.innerHTML = '';
  const local = isDirtyEditing() ? currentTimeInfo() : null;
  // 沙盘依据树节点集合整帧只算一次
  const cfTree = (S.sandbox.active && S.sandbox.result) ? cfTreeNodeIds() : null;
  for (const n of S.work.nodes.values()) {
    const g = el('g', { class: `node ${n.type}`, transform: `translate(${n.pos_x},${n.pos_y})` });
    g.dataset.nodeId = n.id;
    if (S.selected?.kind === 'node' && S.selected.id === n.id) g.classList.add('selected');
    if (dirtyNodeIds().has(n.id)) g.classList.add('dirty');
    if (S.affected.nodes.has(n.id)) g.classList.add('affected');
    if (S.mergeHits.nodes.has(n.id)) g.classList.add('merge-hit');
    if (S.nodeMerge?.nodes?.has(n.id)) g.classList.add('merge-hit');
    if (window._cycleHits?.has(n.id)) g.classList.add('cycle-hit');
    if (S.conflictHits.nodes.has(n.id) || S.localConflictHits.nodes.has(n.id)) {
      g.classList.add('conflict-hit');
    }
    // 反事实沙盘：干预点 / 被阻止（含推演得出）/ 依据树节点
    const sb = S.sandbox;
    if (cfTree && n.type === 'event') {
      if (sb.result.intervened_ids.includes(n.id)) g.classList.add('cf-intervention');
      if (sb.result.prevented_ids.includes(n.id)) g.classList.add('cf-prevented');
      if (cfTree.has(n.id)) g.classList.add('cf-tree');
    }

    g.appendChild(el('rect', { class: 'node-rect', width: NODE_W, height: NODE_H }));
    g.appendChild(el('text', { class: 'node-badge', x: 10, y: 16 },
      n.type === 'event' ? `事件·${n.trigger_mode || 'ANY'}` : '证据'));
    g.appendChild(el('text', { class: 'node-title', x: 10, y: 34 }, trunc(n.title, 14)));

    if (n.type === 'event') {
      const tw = (local ? local.windows.get(n.id) : n.time_window) || {};
      const lo = local ? tw.lower : tw.effective_lower;
      const hi = local ? tw.upper : tw.effective_upper;
      if (lo || hi) {
        g.appendChild(el('text', { class: 'node-time', x: 10, y: 52 },
          `${fmtTimeShort(lo)}–${fmtTimeShort(hi)} UTC`));
      }
      // 证据强度状态点（证据不足不显示）
      const rv = reviewFor(n.id);
      if (rv && rv.status !== 'insufficient') {
        const dot = el('circle', { class: `status-dot ${rv.status}`, cx: NODE_W - 12, cy: 12, r: 5 });
        dot.appendChild(el('title', {}, `${STATUS_META[rv.status]?.[0] || rv.status} S=${rv.support_score} R=${rv.refute_score}`));
        g.appendChild(dot);
      }
    } else {
      // 证据节点：可靠度 + 来源组
      const rel = n.reliability;
      const relTxt = rel == null ? 'r 未设置' : `r=${Number(rel).toFixed(2)}`;
      const grpTxt = n.source_group ? ` · ${trunc(n.source_group, 6)}` : '';
      g.appendChild(el('text', { class: 'node-rel', x: 10, y: 52 }, relTxt + grpTxt));
    }

    g.appendChild(el('circle', {
      class: 'port', dataPort: 'out', cx: NODE_W, cy: NODE_H / 2, r: PORT_R,
    }));
    // 沙盘干预点：右下角禁止标记
    if (S.sandbox.active && S.sandbox.result && n.type === 'event'
        && S.sandbox.result.intervened_ids.includes(n.id)) {
      const tag = el('text', { class: 'cf-flag', x: NODE_W - 6, y: NODE_H + 2, 'text-anchor': 'end' }, '⛔ 干预');
      tag.appendChild(el('title', {}, '该事件被沙盘显式阻止（干预点）'));
      g.appendChild(tag);
    }
    nodeLayer.appendChild(g);
  }
}

function renderEdges() {
  edgeLayer.innerHTML = '';
  const dirty = dirtyEdgeIds();
  for (const e of S.work.edges.values()) {
    const g = el('g', { class: `edge ${e.kind}` });
    g.dataset.edgeId = e.id;
    if (S.selected?.kind === 'edge' && S.selected.id === e.id) g.classList.add('selected');
    if (dirty.has(e.id)) g.classList.add('dirty');
    if (S.affected.edges.has(e.id)) g.classList.add('affected');
    if (S.mergeHits.edges.has(e.id)) g.classList.add('merge-hit');
    if (S.nodeMerge?.edges?.has(e.id)) g.classList.add('merge-hit');
    if (S.conflictHits.edges.has(e.id) || S.localConflictHits.edges.has(e.id)) {
      g.classList.add('conflict-hit');
    }
    const treeEdge = S.sandbox.active && S.sandbox.result
      && S.sandbox.result.tree_edge_ids.includes(e.id);
    if (treeEdge) g.classList.add('cf-tree-edge');

    const d = edgePath(e);
    const marker = e.kind === 'causes' ? 'url(#arrow)'
      : e.kind === 'refutes' ? 'url(#arrow-refutes)' : 'url(#arrow-evidence)';
    let endMarker = marker;
    if (S.nodeMerge?.edges?.has(e.id)) {
      endMarker = 'url(#arrow-tree)';
    } else if (S.conflictHits.edges.has(e.id) || S.localConflictHits.edges.has(e.id)) {
      endMarker = 'url(#arrow-conflict)';
    } else if (treeEdge) {
      endMarker = 'url(#arrow-tree)';
    } else if (S.affected.edges.has(e.id)) endMarker = 'url(#arrow-affected)';
    g.appendChild(el('path', { class: 'edge-path', d, 'marker-end': endMarker }));
    g.appendChild(el('path', { class: 'edge-hit', d }));
    if (e.label) {
      const ep = edgeEndpoints(e);
      const mx = (ep.p1.x + ep.p2.x) / 2, my = (ep.p1.y + ep.p2.y) / 2 - 6;
      const w = e.label.length * 6 + 8;
      g.appendChild(el('rect', { class: 'edge-label-bg', x: mx - w / 2, y: my - 9, width: w, height: 13, rx: 3 }));
      g.appendChild(el('text', { class: 'edge-label', x: mx, y: my + 1, 'text-anchor': 'middle' }, e.label));
    }
    edgeLayer.appendChild(g);
  }
}

function dirtyNodeIds() {
  const s = new Set([
    ...S.changes.node_creates.map((n) => n.id),
    ...S.changes.node_updates.map((n) => n.id),
    ...S.changes.node_deletes,
  ]);
  return s;
}
function dirtyEdgeIds() {
  return new Set([
    ...S.changes.edge_creates.map((e) => e.id),
    ...S.changes.edge_updates.map((e) => e.id),
    ...S.changes.edge_deletes,
  ]);
}

function el(tag, attrs = {}, text) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'dataPort') node.dataset.port = v;
    else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function hEl(tag, attrs = {}) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// ---------- 详情面板 ----------

function timeEditorHtml(n, readOnly) {
  return `
    <div class="detail-row time-edit">
      <label>最早发生时间（带时区，可留空）</label>
      <div class="time-input">
        <input id="dEarliest" value="${esc(n.earliest_time || '')}" placeholder="2026-09-20T10:00:00+08:00"
               ${readOnly ? 'disabled' : ''}/>
        <button type="button" class="btn btn-small time-clear" data-field="earliest_time"
                ${readOnly || !n.earliest_time ? 'disabled' : ''}>清除</button>
      </div>
    </div>
    <div class="detail-row time-edit">
      <label>最晚发生时间（带时区，可留空）</label>
      <div class="time-input">
        <input id="dLatest" value="${esc(n.latest_time || '')}" placeholder="2026-09-20T10:30:00+08:00"
               ${readOnly ? 'disabled' : ''}/>
        <button type="button" class="btn btn-small time-clear" data-field="latest_time"
                ${readOnly || !n.latest_time ? 'disabled' : ''}>清除</button>
      </div>
    </div>`;
}

function bindTimeEditor(nodeId) {
  if (isReadOnly()) return;
  for (const [field, inputId] of [['earliest_time', 'dEarliest'], ['latest_time', 'dLatest']]) {
    const input = $(inputId);
    input?.addEventListener('change', () => {
      const r = parseTimeInput(input.value);
      if (!r.ok) {
        input.classList.add('invalid');
        input.title = r.error;
        input.value = S.work.nodes.get(nodeId)[field] || '';
        flashBanner(r.error);
        return;
      }
      input.classList.remove('invalid');
      input.value = r.value || '';
      stageTimeField(nodeId, field, r.value);
      afterLocalEdit();
    });
  }
  document.querySelectorAll('.time-clear').forEach((btn) => btn.addEventListener('click', () => {
    stageTimeField(nodeId, btn.dataset.field, null);
    afterLocalEdit();
  }));
}

// ---------- 证据字段编辑 ----------

function triggerModeEditorHtml(n, readOnly) {
  return `
    <div class="detail-row">
      <label>触发模式 trigger_mode（反事实推演使用）</label>
      <select id="dTriggerMode" ${readOnly ? 'disabled' : ''}>
        <option value="ANY" ${n.trigger_mode !== 'ALL' ? 'selected' : ''}>ANY · 任一前因发生即触发</option>
        <option value="ALL" ${n.trigger_mode === 'ALL' ? 'selected' : ''}>ALL · 全部前因均发生才触发</option>
      </select>
      <p class="muted small" style="margin:3px 0 0">没有前因的事件默认发生；该模式只影响反事实干预沙盘的推演。</p>
    </div>`;
}

function bindTriggerModeEditor(nodeId) {
  if (isReadOnly()) return;
  $('dTriggerMode')?.addEventListener('change', (ev) => {
    const mode = ev.target.value;
    S.work.nodes.get(nodeId).trigger_mode = mode;
    stageNodeUpdate(nodeId, { trigger_mode: mode });
    afterLocalEdit();
  });
}

// ---------- 证据字段编辑 ----------

function evidenceEditorHtml(n, readOnly) {
  return `
    <div class="detail-row">
      <label>可靠度 reliability（0~1，留空表示未设置，计权按 0）</label>
      <input id="dReliability" type="number" min="0" max="1" step="0.05"
             value="${n.reliability ?? ''}" placeholder="0.50"
             ${readOnly ? 'disabled' : ''}/>
    </div>
    <div class="detail-row">
      <label>来源组 source_group（留空为独立来源）</label>
      <input id="dSourceGroup" value="${esc(n.source_group || '')}" placeholder="如：监控平台"
             ${readOnly ? 'disabled' : ''}/>
    </div>`;
}

function bindEvidenceEditor(nodeId) {
  if (isReadOnly()) return;
  $('dReliability')?.addEventListener('change', (ev) => {
    const raw = ev.target.value.trim();
    let v = null;
    if (raw !== '') {
      v = Number(raw);
      if (!(v >= 0 && v <= 1)) {
        ev.target.classList.add('invalid');
        flashBanner('可靠度需在 0~1 之间');
        ev.target.value = S.work.nodes.get(nodeId).reliability ?? '';
        return;
      }
    }
    ev.target.classList.remove('invalid');
    ev.target.value = v == null ? '' : String(v);
    S.work.nodes.get(nodeId).reliability = v;
    stageNodeUpdate(nodeId, { reliability: v });  // 显式 null 也会序列化，后端据此清除
    afterLocalEdit();
  });
  $('dSourceGroup')?.addEventListener('change', (ev) => {
    const v = ev.target.value.trim() || null;
    S.work.nodes.get(nodeId).source_group = v;
    stageNodeUpdate(nodeId, { source_group: v });
    afterLocalEdit();
  });
}

/** 有效时间窗展示；tw 可能是服务端结构（effective_lower/lower_source/lower_chain）
 *  或本地结构（lower/lowerSrc/lowerChain）。 */
function effectiveWindowHtml(nodeId, tw) {
  const local = isDirtyEditing();
  const lo = local ? tw.lower : tw.effective_lower;
  const hi = local ? tw.upper : tw.effective_upper;
  const loSrcId = local ? tw.lowerSrc : tw.lower_source?.node_id;
  const hiSrcId = local ? tw.upperSrc : tw.upper_source?.node_id;
  const loSrcTitle = local
    ? (loSrcId ? S.work.nodes.get(loSrcId)?.title : null)
    : tw.lower_source?.title;
  const hiSrcTitle = local
    ? (hiSrcId ? S.work.nodes.get(hiSrcId)?.title : null)
    : tw.upper_source?.title;
  const note = local
    ? '<p class="muted small">基于未提交工作图实时重算。</p>'
    : (isReadOnly() ? '<p class="muted small">按所查看的只读图（历史版本 / 提案预览）重算。</p>'
                        : '<p class="muted small">已按整图传播：下界取自身与祖先最大，上界取自身与后代最小。</p>');
  const localConflict = local && lo != null && hi != null && lo > hi
    ? S.localConflictHits.data : null;
  const conflictHtml = localConflict ? `
    <div class="window-conflict">
      ⚠ 下界晚于上界，提交将被整批回滚。冲突链：
      ${localConflict.chain_node_ids.map((id) => esc(S.work.nodes.get(id)?.title || id)).join(' → ')}
    </div>` : '';
  const boundLine = (label, val, srcId, srcTitle) => {
    if (val == null) return `<div class="kv">${label}：<b>不限</b></div>`;
    const self = srcId === nodeId;
    const src = self
      ? '自身填写'
      : `由 <a href="#" class="src-link" data-node="${esc(srcId)}">「${esc(srcTitle)}」</a> 沿因果链收紧`;
    return `<div class="kv">${label}：<b>${fmtTime(val)}</b><br/><span class="bound-src">${src}</span></div>`;
  };
  return `
    <div class="window-box${localConflict ? ' conflict' : ''}">
      <div class="window-title">有效时间窗</div>
      ${boundLine('有效下界', lo, loSrcId, loSrcTitle)}
      ${boundLine('有效上界', hi, hiSrcId, hiSrcTitle)}
      ${conflictHtml}
      ${note}
    </div>`;
}

/** 证据强度审查展示；rv 为服务端或本地 computeEvidenceReview 的同构结果。 */
function evidenceReviewHtml(rv) {
  const local = isDirtyEditing();
  const [label, cls] = STATUS_META[rv.status] || [rv.status, 'insufficient'];
  const relTxt = (x) => (x.reliability_set ? x.reliability.toFixed(2) : '未设置（按 0 计权）');
  const groupHtml = (g) => {
    const dirLabel = g.direction === 'supports' ? '支持' : '反驳';
    const sel = g.selected;
    const others = g.unweighted.map((u) => `
      <div class="ev-line unweighted">✗ <a href="#" class="src-link" data-node="${esc(u.node_id)}">「${esc(u.title)}」</a>
        <span class="muted">r=${relTxt(u)} · 同源未计权</span></div>`).join('');
    return `
      <div class="ev-group">
        <div class="ev-group-head">${g.group ? `来源组「${esc(g.group)}」` : '独立来源（未分组）'} · ${dirLabel}方向</div>
        <div class="ev-line selected ${g.direction}">✓ <a href="#" class="src-link" data-node="${esc(sel.node_id)}">「${esc(sel.title)}」</a>
          <span class="muted">r=${relTxt(sel)} · 入选计权</span></div>
        ${others}
      </div>`;
  };
  const note = local
    ? '<p class="muted small">基于未提交工作图实时重算。</p>'
    : (isReadOnly() ? '<p class="muted small">按所查看的只读图（历史版本 / 提案预览）重算。</p>'
                        : '<p class="muted small">按当前版本计算。</p>');
  return `
    <div class="review-box status-${cls}">
      <div class="window-title">证据强度审查</div>
      <div class="review-scores">
        <span class="stat-chip">支持强度 S <b>${rv.support_score.toFixed(2)}</b></span>
        <span class="stat-chip">反驳强度 R <b>${rv.refute_score.toFixed(2)}</b></span>
        <span class="status-chip ${cls}">${label}</span>
      </div>
      ${rv.groups.length ? rv.groups.map(groupHtml).join('') : '<p class="muted small">该事件暂无证据连线。</p>'}
      <p class="muted small">规则：同一来源组在每个方向只采用可靠度最高的一条，其余同源未计权；独立组按
        S=1-∏(1-r)、R=1-∏(1-r) 聚合。先判 S/R 均 ≥0.6 为存在争议，再判 S≥0.7 已支持 / R≥0.7 已反驳，否则证据不足。</p>
      ${note}
    </div>`;
}

function renderDetail() {
  const body = $('detailBody');
  const h = $('detailTitle');
  if (!S.selected) {
    h.textContent = '未选中元素';
    body.innerHTML = '<p class="muted">点击节点或连线查看详情，可编辑标题/描述/标签。</p>';
    return;
  }
  const readOnly = isReadOnly();
  if (S.selected.kind === 'node') {
    const n = S.work.nodes.get(S.selected.id);
    if (!n) { S.selected = null; return renderDetail(); }
    h.textContent = '节点详情';
    const isEvent = n.type === 'event';
    const tw = eventWindowFor(n.id);
    const rv = reviewFor(n.id);
    body.innerHTML = `
      <span class="detail-type ${n.type}">${n.type === 'event' ? '事件 event' : '证据 evidence'}</span>
      <div class="detail-row"><label>标题</label><input id="dTitle" value="${esc(n.title)}" ${readOnly ? 'disabled' : ''}/></div>
      <div class="detail-row"><label>描述 / 材料</label><textarea rows="3" id="dDesc" ${readOnly ? 'disabled' : ''}>${esc(n.description)}</textarea></div>
      ${isEvent ? timeEditorHtml(n, readOnly) : evidenceEditorHtml(n, readOnly)}
      ${isEvent ? triggerModeEditorHtml(n, readOnly) : ''}
      ${isEvent && tw ? effectiveWindowHtml(n.id, tw) : ''}
      ${isEvent ? evidenceReviewHtml(rv || { support_score: 0, refute_score: 0, status: 'insufficient', groups: [], selected_evidence: [], unweighted_evidence: [] }) : ''}
      <div class="kv">id: <b>${esc(n.id)}</b></div>
      <div class="detail-actions">
        <button class="btn btn-small" id="btnImpact" ${readOnly ? 'disabled' : ''}>重算下游影响</button>
        <button class="btn btn-small btn-danger" id="btnDelete" ${readOnly ? 'disabled' : ''}>删除节点</button>
      </div>`;
    $('dTitle').addEventListener('change', (ev) => {
      const v = ev.target.value.trim();
      if (!v) { ev.target.value = n.title; return; }
      n.title = v; stageNodeUpdate(n.id, { title: v }); afterLocalEdit();
    });
    $('dDesc').addEventListener('change', (ev) => {
      n.description = ev.target.value; stageNodeUpdate(n.id, { description: ev.target.value }); afterLocalEdit();
    });
    if (isEvent) { bindTimeEditor(n.id); bindTriggerModeEditor(n.id); }
    else bindEvidenceEditor(n.id);
    body.querySelectorAll('.src-link').forEach((a) => a.addEventListener('click', (ev) => {
      ev.preventDefault();
      S.selected = { kind: 'node', id: a.dataset.node };
      renderAll();
    }));
    $('btnDelete')?.addEventListener('click', deleteSelected);
    $('btnImpact')?.addEventListener('click', () => computeImpact(n.id));
  } else {
    const e = S.work.edges.get(S.selected.id);
    if (!e) { S.selected = null; return renderDetail(); }
    const src = S.work.nodes.get(e.source_id), dst = S.work.nodes.get(e.target_id);
    const isEvidenceEdge = e.kind === 'supports' || e.kind === 'refutes';
    const kindLabel = e.kind === 'causes' ? '因果 causes'
      : e.kind === 'supports' ? '证据支持 supports' : '证据反驳 refutes';
    h.textContent = '连线详情';
    body.innerHTML = `
      <div class="detail-row"><span class="detail-type ${isEvidenceEdge ? 'evidence' : 'event'}">${kindLabel}</span></div>
      <div class="kv">${esc(src?.title)} → ${esc(dst?.title)}</div>
      ${isEvidenceEdge ? `
      <div class="detail-row" style="margin-top:8px"><label>证据立场（supports / refutes 可切换）</label>
        <select id="dKind" ${readOnly ? 'disabled' : ''}>
          <option value="supports" ${e.kind === 'supports' ? 'selected' : ''}>支持 supports</option>
          <option value="refutes" ${e.kind === 'refutes' ? 'selected' : ''}>反驳 refutes</option>
        </select></div>`
        : '<p class="muted small">因果连线（causes）方向固定，不能修改类型。</p>'}
      <div class="detail-row" style="margin-top:8px"><label>连线说明</label>
        <input id="dLabel" value="${esc(e.label)}" ${readOnly ? 'disabled' : ''}/></div>
      <div class="kv">id: <b>${esc(e.id)}</b></div>
      <div class="detail-actions">
        <button class="btn btn-small btn-danger" id="btnDelete" ${readOnly ? 'disabled' : ''}>删除连线</button>
      </div>`;
    $('dKind')?.addEventListener('change', (ev) => {
      const kind = ev.target.value;
      if ([...S.work.edges.values()].some((x) => x.id !== e.id
        && x.source_id === e.source_id && x.target_id === e.target_id && x.kind === kind)) {
        flashBanner('切换立场后与既有同类型连线重复。');
        ev.target.value = e.kind;
        return;
      }
      e.kind = kind;
      stageEdgeUpdate(e.id, { kind });
      afterLocalEdit();
    });
    $('dLabel').addEventListener('change', (ev) => {
      e.label = ev.target.value; stageEdgeUpdate(e.id, { label: ev.target.value }); afterLocalEdit();
    });
    $('btnDelete').addEventListener('click', deleteSelected);
  }
}

// ---------- 提交 ----------

async function commit() {
  const message = $('commitMessage').value.trim();
  if (!message || !changeCount() || isReadOnly()) return;
  $('btnCommit').disabled = true;
  try {
    const res = await api('/api/changes/commit', {
      method: 'POST',
      body: JSON.stringify({ message, changes: { ...S.changes, base_version_id: S.work.version_id } }),
    });
    $('commitMessage').value = '';
    hideError();
    await loadState();
    toast(`已生成版本快照 v${res.version_id}`);
  } catch (err) {
    showError(err);
    $('btnCommit').disabled = false;
  }
}

function showError(err) {
  const c = err.payload?.conflict;
  if (c) {
    // 时间窗冲突：高亮提供两端界限的节点与可追溯因果链
    S.conflictHits = {
      nodes: new Set(c.chain_node_ids || []),
      edges: new Set(c.chain_edge_ids || []),
      data: c,
    };
    const chain = (c.chain_node_ids || [])
      .map((id) => S.work.nodes.get(id)?.title || id).join(' → ');
    $('errorText').textContent =
      `事务已回滚：${err.message}（下界 ${c.lower_bound} ↔ 上界 ${c.upper_bound}；冲突链：${chain}）。未提交内容已保留。`;
  } else {
    $('errorText').textContent = '事务已回滚：' + err.message;
    S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
  }
  $('errorBanner').hidden = false;
  // 成环：高亮环上节点
  window._cycleHits = new Set(err.payload?.cycle || []);
  renderNodes();
  renderEdges();
}
function hideError() {
  $('errorBanner').hidden = true;
  window._cycleHits = new Set();
  S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
}

// ---------- 版本 / 回滚 ----------

async function refreshVersions() {
  const data = await api('/api/versions');
  const ul = $('versionList');
  ul.innerHTML = '';
  for (const v of data.versions) {
    const li = hEl('li', { class: 'version-item' });
    if (v.id === data.current_version_id) li.classList.add('current');
    if (v.id === S.viewingVersion) li.classList.add('viewing');
    const kindLabel = v.kind === 'commit' ? '提交' : v.kind === 'rollback' ? '回滚' : '合并';
    const mergeMeta = (v.merged_proposal_ids && v.merged_proposal_ids.length)
      ? `<span class="muted">合并提案 #${v.merged_proposal_ids.join(' / #')}</span>` : '';
    li.innerHTML = `
      <div class="version-head"><span>v${v.id}</span>
        <span class="tag ${v.kind}">${kindLabel}</span></div>
      <div class="version-msg">${esc(v.message)}</div>
      <div class="version-meta">
        <span>${v.node_count} 节点 / ${v.edge_count} 连线</span>
        <span>${esc(v.created_at.replace('T', ' ').replace('Z', ' UTC'))}</span>
        ${mergeMeta}
        ${v.id === data.current_version_id ? '<span class="tag current">当前</span>' : ''}
      </div>`;
    li.addEventListener('click', () => viewVersion(v.id));
    ul.appendChild(li);
  }
}

async function viewVersion(vid) {
  const g = await api(`/api/versions/${vid}`);
  S.work = snapshotFromServer(g);
  S.selected = null;
  S.viewingVersion = vid;
  S.preview = null;
  S.mergeHits = { nodes: new Set(), edges: new Set() };
  S.nodeMerge = null;
  $('nodeMergeModal').hidden = true;
  S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
  clearAffected();
  resetSandbox();
  hideError();
  renderAll();
  refreshVersions();
}

async function backToCurrent() {
  await loadState();
}

async function rollbackViewing() {
  if (!S.viewingVersion) return;
  if (!confirm(`确定以 v${S.viewingVersion} 的内容生成一个新版本（回滚）？当前未提交修改与最新版将成为历史。`)) return;
  try {
    const res = await api('/api/versions/rollback', {
      method: 'POST',
      body: JSON.stringify({ target_version_id: S.viewingVersion }),
    });
    toast(`已回滚，生成新版本 v${res.version_id}`);
    await loadState();
  } catch (err) {
    alert('回滚失败：' + err.message);
  }
}

// ---------- 下游影响 ----------

function localDownstream(nodeId) {
  // 本地 BFS，供未提交的图也能即时预览
  const out = new Map();
  for (const e of S.work.edges.values()) {
    if (!out.has(e.source_id)) out.set(e.source_id, []);
    out.get(e.source_id).push(e);
  }
  const depth = new Map([[nodeId, 0]]);
  const q = [nodeId];
  const edgeIds = new Set();
  while (q.length) {
    const u = q.shift();
    for (const e of out.get(u) || []) {
      edgeIds.add(e.id);
      if (!depth.has(e.target_id)) { depth.set(e.target_id, depth.get(u) + 1); q.push(e.target_id); }
    }
  }
  const layers = [];
  for (const [nid, d] of depth) {
    if (d === 0) continue;
    while (layers.length < d) layers.push([]);
    layers[d - 1].push(S.work.nodes.get(nid));
  }
  return { depth, edgeIds, layers };
}

async function computeImpact(nodeId) {
  const body = $('impactBody');
  body.innerHTML = '<p class="muted">计算中…</p>';
  try {
    let data;
    if (S.viewingVersion) {
      data = await api(`/api/nodes/${encodeURIComponent(nodeId)}/downstream?version_id=${S.viewingVersion}`);
    } else if (isReadOnly() || changeCount() > 0) {
      // 提案 / 合并预览图与未提交工作图都只在本地：直接对当前画布图做 BFS
      data = localImpactData(nodeId);
      data.local = true;
    } else {
      data = await api(`/api/nodes/${encodeURIComponent(nodeId)}/downstream`);
    }
    renderImpact(nodeId, data);
  } catch (err) {
    body.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
}

function localImpactData(nodeId) {
  const { depth, edgeIds, layers } = localDownstream(nodeId);
  const affected = [...depth.keys()].filter((id) => id !== nodeId);
  return {
    node_id: nodeId,
    node_title: S.work.nodes.get(nodeId).title,
    affected_node_ids: affected,
    total_affected: affected.length,
    affected_events: affected.filter((id) => S.work.nodes.get(id)?.type === 'event').length,
    affected_evidence: affected.filter((id) => S.work.nodes.get(id)?.type === 'evidence').length,
    affected_edges: edgeIds.size,
    max_depth: Math.max(0, ...depth.values()),
    layers: layers.map((l) => l.filter(Boolean).map((n) => ({ id: n.id, type: n.type, title: n.title }))),
    edges: [...edgeIds].map((id) => ({ id })),
  };
}

function renderImpact(nodeId, d) {
  clearAffected();
  d.affected_node_ids.forEach((id) => S.affected.nodes.add(id));
  d.edges.forEach((e) => S.affected.edges.add(e.id));
  renderNodes();
  renderEdges();

  $('impactBody').innerHTML = `
    <div class="kv">根节点：<b>${esc(d.node_title)}</b>${d.local ? ' <span style="color:var(--evidence)">（基于未提交工作图）</span>' : ''}</div>
    <div class="impact-stat">
      <span class="stat-chip">受影响节点 <b>${d.total_affected}</b></span>
      <span class="stat-chip">事件 <b>${d.affected_events}</b></span>
      <span class="stat-chip">证据 <b>${d.affected_evidence}</b></span>
      <span class="stat-chip">路径边 <b>${d.affected_edges}</b></span>
      <span class="stat-chip">最大层级 <b>${d.max_depth}</b></span>
    </div>
    ${d.layers.length ? d.layers.map((layer, i) => `
      <div class="impact-layer">
        <div class="impact-layer-title">第 ${i + 1} 层下游</div>
        ${layer.map((n) => `<span class="impact-node ${n.type}">${n.type === 'evidence' ? '📎 ' : ''}${esc(n.title)}</span>`).join('')}
      </div>`).join('') : '<p class="muted">该节点没有下游。</p>'}
    ${!d.local ? `<button class="btn btn-small btn-block" id="btnRecompute">重新请求重算</button>` : ''}`;
  $('btnRecompute')?.addEventListener('click', () => computeImpact(nodeId));
}

function clearAffected() {
  S.affected = { nodes: new Set(), edges: new Set() };
}

// ---------- 反事实干预沙盘（与后端 graph.simulate_counterfactual 同构） ----------
//
// 只看「事件 + causes」子图：无前因事件默认发生；被显式勾选的干预点恒为 prevented；
// ANY 门任一前因仍发生即发生，否则阻止；ALL 门全部前因均发生才发生。
// 依据树：ANY 发生取 ID 最小仍发生前因 / 阻止列全部；
//         ALL 发生列全部 / 阻止取 ID 最小已阻止前因；干预点不展开。
function computeCounterfactual(nodes, edges, targetId, preventedIds) {
  const events = new Set([...nodes.values()].filter((n) => n.type === 'event').map((n) => n.id));
  const target = nodes.get(targetId);
  if (!target) throw new Error(`观察目标不存在：${targetId}`);
  if (target.type !== 'event') throw new Error('观察目标必须是事件节点');
  const preventedSet = new Set(preventedIds);
  for (const pid of preventedSet) {
    const n = nodes.get(pid);
    if (!n) throw new Error(`被阻止事件不存在：${pid}`);
    if (n.type !== 'event') throw new Error(`被阻止节点必须是事件：${pid}`);
  }

  const parents = new Map([...events].map((id) => [id, []]));
  const parentEdge = new Map();
  for (const e of edges.values()) {
    if (e.kind !== 'causes' || !events.has(e.source_id) || !events.has(e.target_id)) continue;
    parents.get(e.target_id).push(e.source_id);
    parentEdge.set(`${e.source_id}->${e.target_id}`, e.id);
  }
  for (const arr of parents.values()) arr.sort();

  // Kahn 拓扑排序，初始队列按 id 排序，结果与 Map 遍历顺序无关
  const children = new Map([...events].map((id) => [id, []]));
  const indeg = new Map();
  for (const [v, ps] of parents) {
    indeg.set(v, ps.length);
    for (const u of ps) children.get(u).push(v);
  }
  for (const arr of children.values()) arr.sort();
  const q = [...events].filter((id) => indeg.get(id) === 0).sort();
  const topo = [];
  while (q.length) {
    const u = q.shift(); topo.push(u);
    for (const v of children.get(u)) {
      indeg.set(v, indeg.get(v) - 1);
      if (indeg.get(v) === 0) q.push(v);
    }
  }
  if (topo.length !== events.size) throw new Error('因果图存在环，无法按拓扑顺序推演');

  const intervened = new Set(preventedSet);
  const status = new Map(), reason = new Map();
  for (const id of topo) {
    if (intervened.has(id)) { status.set(id, 'prevented'); reason.set(id, 'intervention'); continue; }
    const ps = parents.get(id);
    if (!ps.length) { status.set(id, 'still_active'); reason.set(id, 'no_causes'); continue; }
    const mode = nodes.get(id).trigger_mode === 'ALL' ? 'ALL' : 'ANY';
    const active = ps.filter((p) => status.get(p) === 'still_active');
    const blocked = ps.filter((p) => status.get(p) === 'prevented');
    if (mode === 'ANY') {
      if (active.length) { status.set(id, 'still_active'); reason.set(id, 'any_active'); }
      else { status.set(id, 'prevented'); reason.set(id, 'all_prevented'); }
    } else {
      if (!blocked.length) { status.set(id, 'still_active'); reason.set(id, 'all_active'); }
      else { status.set(id, 'prevented'); reason.set(id, 'some_prevented'); }
    }
  }

  const treeEdgeIds = new Set();
  const buildTree = (id) => {
    const out = {
      node_id: id, title: nodes.get(id).title,
      trigger_mode: nodes.get(id).trigger_mode === 'ALL' ? 'ALL' : 'ANY',
      status: status.get(id), reason: reason.get(id), children: [],
    };
    const ps = parents.get(id);
    if (reason.get(id) === 'intervention' || !ps.length) return out;
    const active = ps.filter((p) => status.get(p) === 'still_active');
    const blocked = ps.filter((p) => status.get(p) === 'prevented');
    let childIds;
    if (reason.get(id) === 'any_active') childIds = [active[0]];
    else if (reason.get(id) === 'all_active') childIds = ps;
    else if (reason.get(id) === 'all_prevented') childIds = ps;
    else childIds = [blocked[0]];  // some_prevented
    for (const p of childIds) {
      const eid = parentEdge.get(`${p}->${id}`);
      if (eid) treeEdgeIds.add(eid);
      out.children.push(buildTree(p));
    }
    return out;
  };

  const evSorted = [...events].sort();
  return {
    target_id: targetId,
    target_title: target.title,
    target_status: status.get(targetId),
    version_id: S.work.version_id,
    intervened_ids: [...intervened].sort(),
    prevented_ids: evSorted.filter((id) => status.get(id) === 'prevented'),
    still_active_ids: evSorted.filter((id) => status.get(id) === 'still_active'),
    events: evSorted.map((id) => ({
      node_id: id, title: nodes.get(id).title,
      trigger_mode: nodes.get(id).trigger_mode === 'ALL' ? 'ALL' : 'ANY',
      status: status.get(id), reason: reason.get(id),
      intervened: intervened.has(id),
    })),
    tree: buildTree(targetId),
    tree_edge_ids: [...treeEdgeIds].sort(),
  };
}

function sandboxEventNodes() {
  return [...S.work.nodes.values()].filter((n) => n.type === 'event');
}

/** 收集沙盘依据树中的全部节点 id（递归）。 */
function cfTreeNodeIds() {
  const ids = new Set();
  const walk = (t) => {
    if (!t) return;
    ids.add(t.node_id);
    (t.children || []).forEach(walk);
  };
  walk(S.sandbox?.result?.tree);
  return ids;
}

function resetSandbox() {
  S.sandbox = {
    active: false, targetId: null, prevented: new Set(),
    result: null, local: false, loading: false, sig: '', seq: 0,
  };
}

/** 结构变化（删除事件、切换版本）时丢弃依据树，避免面板引用已不存在的节点。 */
function clearSandboxResult() {
  S.sandbox.result = null;
  S.sandbox.loading = false;
  S.sandbox.seq++;
}

/** 剔除已不存在 / 已变为证据的勾选项与目标。 */
function pruneSandboxSelection() {
  const ids = new Set(sandboxEventNodes().map((n) => n.id));
  let changed = false;
  for (const id of [...S.sandbox.prevented]) {
    if (!ids.has(id)) { S.sandbox.prevented.delete(id); changed = true; }
  }
  if (S.sandbox.targetId && !ids.has(S.sandbox.targetId)) {
    S.sandbox.targetId = null;
    clearSandboxResult();
    return;
  }
  if (changed) clearSandboxResult();
}

/** 关闭沙盘高亮并还原占位提示。 */
function toggleSandboxOff() {
  S.sandbox.active = false;
  clearSandboxResult();
  $('sandboxResult').innerHTML =
    '<p class="muted small">选择目标并勾选干预点后即时推演；未提交工作图即时生效。</p>';
  renderNodes();
  renderEdges();
}

/** 当前沙盘是否应采用本地推演：未提交工作图（含未提交的 trigger_mode/拓扑变化）即时生效。 */
function sandboxIsLocal() {
  return isDirtyEditing();
}

async function syncSandbox() {
  if (!S.sandbox.active) return;
  pruneSandboxSelection();
  const sb = S.sandbox;
  if (!sb.targetId) { sb.result = null; return; }
  // 未提交工作图 / 提案与合并预览：本地即时推演；历史快照 / 干净当前版：调服务端
  if (sandboxIsLocal() || S.preview) {
    try {
      sb.result = computeCounterfactual(S.work.nodes, S.work.edges, sb.targetId, [...sb.prevented]);
      sb.local = true;
    } catch (err) {
      sb.result = null;
      $('sandboxResult').innerHTML = `<p class="muted small">${esc(err.message)}</p>`;
      return;
    }
    renderSandboxResult();
    return;
  }
  const seq = ++sb.seq;
  sb.loading = true;
  renderSandboxResult();
  try {
    const res = await api('/api/counterfactual/simulate', {
      method: 'POST',
      body: JSON.stringify({
        target_id: sb.targetId,
        prevented_ids: [...sb.prevented],
        ...(S.viewingVersion ? { version_id: S.viewingVersion } : {}),
      }),
    });
    if (seq === sb.seq) {
      sb.result = res;
      sb.local = false;
      sb.loading = false;
      renderSandboxResult();
    }
  } catch (err) {
    if (seq === sb.seq) {
      sb.loading = false;
      sb.result = null;
      $('sandboxResult').innerHTML = `<p class="muted small">${esc(err.message)}</p>`;
    }
  }
}

// ---------- 沙盘面板渲染 ----------

const REASON_META = {
  intervention: ['干预阻止', 'intervention'],
  no_causes: ['无前因 · 默认发生', 'no-causes'],
  any_active: ['ANY · 存在仍发生前因', 'active'],
  all_prevented: ['ANY · 前因全部被阻止', 'blocked'],
  all_active: ['ALL · 前因全部发生', 'active'],
  some_prevented: ['ALL · 存在被阻止前因', 'blocked'],
};

function renderSandboxControls() {
  const sb = S.sandbox;
  const events = sandboxEventNodes();
  const sig = events.map((n) => `${n.id}:${n.trigger_mode}`).join('|');
  // 事件集合 / 模式未变时只更新勾选状态，避免重建列表打断交互
  if (sb.sig !== sig) {
    sb.sig = sig;
    // 干净视图 / 历史快照下结构已变：服务端旧结果立即作废（本地模式随后会即时重算）
    if (sb.active && !sandboxIsLocal()) clearSandboxResult();
    const sel = $('sandboxTarget');
    sel.innerHTML = '';
    sel.appendChild(hEl('option', { value: '' }));
    sel.options[0].textContent = '— 请选择观察目标 —';
    for (const n of events) {
      const o = hEl('option', { value: n.id });
      o.textContent = `[${n.trigger_mode}] ${n.title}`;
      sel.appendChild(o);
    }
    if (sb.targetId && events.some((n) => n.id === sb.targetId)) sel.value = sb.targetId;

    const box = $('sandboxPrevented');
    box.innerHTML = '';
    if (!events.length) {
      box.innerHTML = '<p class="muted small">图中暂无事件节点。</p>';
    }
    for (const n of events) {
      const label = document.createElement('label');
      label.className = 'sandbox-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = n.id;
      cb.checked = sb.prevented.has(n.id);
      cb.addEventListener('change', () => {
        if (cb.checked) sb.prevented.add(n.id); else sb.prevented.delete(n.id);
        if (!sb.active) sb.active = true;
        void syncSandbox().then(() => { renderNodes(); renderEdges(); });
      });
      const span = document.createElement('span');
      span.className = `mode-tag mode-${n.trigger_mode.toLowerCase()}`;
      span.textContent = n.trigger_mode;
      label.appendChild(cb);
      label.appendChild(span);
      label.appendChild(document.createTextNode(n.title));
      box.appendChild(label);
    }
  }
  $('sandboxTarget').value = sb.targetId || '';
  $('sandboxPrevented').querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.checked = sb.prevented.has(cb.value);
  });
}

function sandboxTreeHtml(node) {
  const [reasonLabel, reasonCls] = REASON_META[node.reason] || [node.reason, ''];
  const stCls = node.status === 'still_active' ? 'active' : 'prevented';
  const stLabel = node.status === 'still_active' ? '仍发生' : '被阻止';
  return `
    <li class="tree-node">
      <div class="tree-line ${stCls}">
        <a href="#" class="src-link" data-node="${esc(node.node_id)}">「${esc(node.title)}」</a>
        <span class="mode-tag mode-${node.trigger_mode.toLowerCase()}">${node.trigger_mode}</span>
        <span class="sandbox-chip ${stCls}">${stLabel}</span>
        <span class="tree-reason ${reasonCls}">${esc(reasonLabel)}</span>
      </div>
      ${node.children.length ? `<ul>${node.children.map(sandboxTreeHtml).join('')}</ul>` : ''}
    </li>`;
}

function renderSandboxResult() {
  const sb = S.sandbox;
  const box = $('sandboxResult');
  if (!sb.active) {
    box.innerHTML = '<p class="muted small">选择目标并勾选干预点后即时推演；未提交工作图即时生效。</p>';
    return;
  }
  if (!sb.targetId) {
    box.innerHTML = '<p class="muted small">请先选择观察目标。</p>';
    return;
  }
  if (sb.loading) {
    box.innerHTML = '<p class="muted small">推演中…</p>';
    return;
  }
  const r = sb.result;
  if (!r) return;
  const targetCls = r.target_status === 'still_active' ? 'active' : 'prevented';
  const targetLabel = r.target_status === 'still_active' ? '仍发生 still_active' : '被阻止 prevented';
  box.innerHTML = `
    <div class="sandbox-verdict ${targetCls}">
      目标 <b>「${esc(r.target_title)}」</b>：${targetLabel}
    </div>
    <div class="sandbox-stat">
      <span class="stat-chip">干预点 <b>${r.intervened_ids.length}</b></span>
      <span class="stat-chip">被阻止事件 <b>${r.prevented_ids.length}</b></span>
      <span class="stat-chip">仍发生事件 <b>${r.still_active_ids.length}</b></span>
    </div>
    ${r.intervened_ids.length ? `<div class="muted small">干预点：
      ${r.intervened_ids.map((id) => esc(S.work.nodes.get(id)?.title || id)).join('、')}</div>` : ''}
    <div class="tree-title">目标递归依据树${sb.local ? '（基于未提交工作图）' : ''}</div>
    <ul class="reason-tree">${sandboxTreeHtml(r.tree)}</ul>
    <p class="muted small">依据选择：ANY 发生取 ID 最小仍发生前因、阻止时列全部前因；
      ALL 发生列全部前因、阻止时取 ID 最小已阻止前因；无前因事件默认发生。</p>`;
  box.querySelectorAll('.src-link').forEach((a) => a.addEventListener('click', (ev) => {
    ev.preventDefault();
    S.selected = { kind: 'node', id: a.dataset.node };
    renderNodes();
    renderEdges();
    renderDetail();
  }));
}

// ---------- 可复算复盘结论包（导出 / 核验均只读：不写库、不生成版本、不改变工作图） ----------
//
// 导出：服务端收集根事件、全部 causes 祖先与关联 supports/refutes 证据，连同版本元数据、
//       按包内子图重算的时间窗与证据审查结果组成自包含 payload，并对紧凑 UTF-8 JSON
//       计算 SHA-256；页面下载含 digest 的文件（文件内容即服务端原字节）。
// 核验：上传文件 → 服务端先校验 digest，再仅凭包内原始节点与连线重建 DAG，
//       重算时间窗与证据审查并逐字段比对；摘要匹配且重算一致才显示「通过」。
const pkg = { sig: '' };

/** 导出可选的根事件：历史 / 提案预览的只读图取画布；否则取已提交当前版（未提交修改不纳入）。 */
function pkgEventNodes() {
  const src = isReadOnly() ? [...S.work.nodes.values()] : (S.committed?.nodes || []);
  return src.filter((n) => n.type === 'event')
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

function renderPkgControls() {
  const events = pkgEventNodes();
  const sig = (S.viewingVersion || (S.preview ? 'pv' : 'cur')) + '|' + events.map((n) => n.id).join('|');
  const sel = $('pkgEvent');
  if (pkg.sig !== sig) {
    const prev = sel.value;
    pkg.sig = sig;
    sel.innerHTML = '';
    if (!events.length) {
      const o = hEl('option', { value: '' });
      o.textContent = '— 暂无事件 —';
      sel.appendChild(o);
    } else {
      for (const n of events) {
        const o = hEl('option', { value: n.id });
        o.textContent = `${n.id} · ${trunc(n.title, 12)}`;
        sel.appendChild(o);
      }
      if (events.some((n) => n.id === prev)) sel.value = prev;
    }
  }
  $('btnPkgExport').disabled = !events.length || !!S.preview;
  if (S.preview) {
    $('pkgBasis').textContent = '提案预览图未落库，结论包导出仅支持已提交版本；请退出预览。';
  } else if (S.viewingVersion) {
    $('pkgBasis').textContent = `导出基于所浏览的历史快照 v${S.viewingVersion}（只读）。`;
  } else {
    $('pkgBasis').textContent = `导出基于当前版 v${S.committed?.version_id ?? '-'}` +
      (changeCount() ? '；存在未提交修改，不包含在导出中。' : '。');
  }
}

async function exportConclusionPackage() {
  const eventId = $('pkgEvent').value;
  if (!eventId) { flashBanner('请先选择要导出的根事件。'); return; }
  if (S.preview) { flashBanner('提案预览图未落库，不能导出结论包。'); return; }
  const qs = S.viewingVersion ? `?version_id=${S.viewingVersion}` : '';
  try {
    const res = await fetch(`/api/conclusion/export/${encodeURIComponent(eventId)}${qs}`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `导出失败 (${res.status})`);
    }
    const blob = await res.blob();  // 原样保存服务端字节，保证摘要可核验
    const m = (res.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)"?/);
    const name = m ? m[1] : `conclusion-${eventId}.json`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    $('pkgResult').innerHTML =
      `<p class="muted small">已导出 <b>${esc(name)}</b>（含 SHA-256 摘要）。导出不写库、不生成版本。</p>`;
    toast('结论包已导出');
  } catch (err) {
    flashBanner(err.message);
  }
}

async function verifyConclusionPackage(file) {
  const box = $('pkgResult');
  box.innerHTML = '<p class="muted">核验中…</p>';
  try {
    const text = await file.text();  // 原样上传文件内容，由服务端解析与规范化
    const r = await api('/api/conclusion/verify', { method: 'POST', body: text });
    renderPkgVerify(file.name, r);
  } catch (err) {
    box.innerHTML = `<div class="pkg-verdict fail">✘ 核验失败：${esc(err.message)}</div>`;
  }
}

const fmtPkgVal = (v) => (v === undefined ? '（缺失）' : JSON.stringify(v));

function renderPkgVerify(fname, r) {
  const rec = r.recompute;
  const meta = r.meta || {};
  const diffsHtml = rec.diffs.length ? `
    <table class="diff-table">
      <thead><tr><th>JSON Pointer</th><th>包内值</th><th>重算值</th></tr></thead>
      <tbody>${rec.diffs.map((d) => `<tr>
        <td class="mono">${esc(d.pointer)}</td>
        <td class="mono">${esc(fmtPkgVal(d.packaged))}</td>
        <td class="mono">${esc(fmtPkgVal(d.recomputed))}</td>
      </tr>`).join('')}</tbody>
    </table>
    ${rec.diffs_truncated ? '<p class="muted small">差异过多，仅列出前 200 条。</p>' : ''}` : '';
  $('pkgResult').innerHTML = `
    <div class="pkg-verdict ${r.ok ? 'ok' : 'fail'}">
      ${r.ok ? '✔ 通过：摘要匹配，时间窗与证据审查重算一致' : '✘ 未通过'}
    </div>
    <div class="kv">文件：<b>${esc(fname)}</b></div>
    ${meta.root_event_id ? `<div class="kv">根事件：<b>${esc(meta.root_event_title || meta.root_event_id)}</b>
      （${esc(meta.root_event_id)}）· 来源版本 v${esc(String(meta.version_id ?? '-'))}
      · ${esc(String(meta.node_count ?? '-'))} 节点 / ${esc(String(meta.edge_count ?? '-'))} 连线</div>` : ''}
    <div class="pkg-checks">
      <span class="pkg-check ${r.digest.match ? 'ok' : 'bad'}">${r.digest.match ? '✓ 摘要匹配' : '✗ 摘要不匹配'}</span>
      <span class="pkg-check ${rec.consistent ? 'ok' : 'bad'}">${rec.consistent ? '✓ 重算一致' : '✗ 重算不一致'}</span>
    </div>
    <div class="kv mono tiny">包内摘要 ${esc(r.digest.expected)}<br/>实算摘要 ${esc(r.digest.actual)}</div>
    ${rec.error ? `<div class="pkg-check bad">重建 / 比对失败：${esc(rec.error)}</div>` : ''}
    ${diffsHtml}
    <p class="muted small">只读核验：不写库、不生成版本、不改变工作图。</p>`;
}

// ---------- 根因共识盲评 ----------
// 评审场绑定某个不可变版本快照：建场选 3~20 个事件、登记唯一评审人、设最低票数；
// 封榜前只显示已提交名单与数量（不泄露任何排序），封榜（校验 session_revision）后
// 按 Schulze 法计票并公开全部选票。本模块不触碰工作图 / changeset / 版本。
const reviewUI = {
  sessions: [],       // 评审场列表
  detail: null,       // 当前打开的评审场详情
  tab: 'vote',        // vote | ranks | matrix | issuance
  voter: '',          // 投票页选择的评审人
  ranking: [],        // 正在编辑的严格排序（node_id 数组）
  createEvents: [],   // 建场表单所选版本的事件列表
  // 根因结论签发页
  issueSel: null,     // 勾选的最终根因集合 Set（首次进入按最高并列名次初始化）
  issueNote: '',      // 签发说明
  issueReport: null,  // 最近一次只读预检 / 失败响应的结构化报告
  issueLoading: false,
};

async function loadReviews() {
  try {
    const data = await api('/api/reviews');
    reviewUI.sessions = data.sessions;
    renderReviewList();
  } catch (_) { /* 面板加载失败不阻断主流程 */ }
}

function renderReviewList() {
  const ul = $('reviewList');
  ul.innerHTML = '';
  if (!reviewUI.sessions.length) {
    const li = hEl('li');
    li.className = 'muted small';
    li.style.listStyle = 'none';
    li.textContent = '暂无评审场，点击「新建评审场」发起。';
    ul.appendChild(li);
    return;
  }
  for (const s of reviewUI.sessions) {
    const li = hEl('li', { class: 'review-item' });
    const issueTag = s.issuance
      ? `<span class="tag issuance">已签发 🔒</span>` : '';
    li.innerHTML = `
      <div class="version-head"><span>${esc(s.title)}</span>
        <span class="tag-row">
          ${issueTag}
          <span class="tag ${s.status === 'open' ? 'commit' : 'current'}">${s.status === 'open' ? '开放中' : '已封榜'}</span>
        </span></div>
      <div class="version-meta">
        <span>绑定 v${s.version_id}</span>
        <span>候选 ${s.candidate_count}</span>
        <span>已提交 ${s.submitted_count}/${s.reviewer_count}</span>
        <span>最低 ${s.min_votes} 票</span>
        ${s.issuance ? `<span class="issuance-meta">签发 #${s.issuance.id} · ${esc((s.issuance.issued_at || '').replace('T', ' ').replace('Z', ' UTC'))}</span>` : ''}
      </div>`;
    li.addEventListener('click', () => openReview(s.id));
    ul.appendChild(li);
  }
}

async function toggleReviewCreate() {
  const form = $('reviewCreate');
  form.hidden = !form.hidden;
  if (form.hidden) return;
  try {
    const data = await api('/api/versions');
    const sel = $('rvVersion');
    const prev = sel.value;
    sel.innerHTML = '';
    for (const v of [...data.versions].sort((a, b) => a.id - b.id)) {
      const o = hEl('option', { value: v.id });
      o.textContent = `v${v.id} · ${trunc(v.message, 16)}`;
      sel.appendChild(o);
    }
    sel.value = prev && data.versions.some((v) => String(v.id) === prev)
      ? prev : String(data.current_version_id);
    await loadRvEvents(Number(sel.value));
  } catch (err) {
    $('rvCreateMsg').textContent = err.message;
  }
}

async function loadRvEvents(vid) {
  const box = $('rvCandidates');
  box.innerHTML = '<p class="muted small">加载中…</p>';
  try {
    const g = await api(`/api/versions/${vid}`);
    reviewUI.createEvents = g.nodes.filter((n) => n.type === 'event');
    box.innerHTML = '';
    if (!reviewUI.createEvents.length) {
      box.innerHTML = '<p class="muted small">该版本没有事件节点。</p>';
      return;
    }
    for (const n of reviewUI.createEvents) {
      const label = document.createElement('label');
      label.className = 'sandbox-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = n.id;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(n.title));
      box.appendChild(label);
    }
  } catch (err) {
    box.innerHTML = `<p class="muted small">${esc(err.message)}</p>`;
  }
}

async function createReview() {
  const msg = $('rvCreateMsg');
  const title = $('rvTitle').value.trim();
  const version_id = Number($('rvVersion').value);
  const candidate_ids = [...$('rvCandidates').querySelectorAll('input:checked')].map((c) => c.value);
  const reviewers = $('rvReviewers').value.split('\n').map((s) => s.trim()).filter(Boolean);
  const min_votes = Number($('rvMinVotes').value);
  if (!title) { msg.textContent = '请填写评审场名称。'; return; }
  if (candidate_ids.length < 3 || candidate_ids.length > 20) {
    msg.textContent = `候选事件需 3~20 个（当前 ${candidate_ids.length} 个）。`;
    return;
  }
  if (!reviewers.length) { msg.textContent = '请至少登记一名评审人（每行一个）。'; return; }
  if (new Set(reviewers).size !== reviewers.length) { msg.textContent = '评审人存在重复。'; return; }
  if (!(min_votes >= 1 && min_votes <= reviewers.length)) {
    msg.textContent = `最低票数需在 1~${reviewers.length} 之间。`;
    return;
  }
  msg.textContent = '';
  try {
    const d = await api('/api/reviews', {
      method: 'POST',
      body: JSON.stringify({ title, version_id, candidate_ids, reviewers, min_votes }),
    });
    $('reviewCreate').hidden = true;
    $('rvTitle').value = '';
    $('rvReviewers').value = '';
    await loadReviews();
    toast('评审场已创建');
    openReview(d.id);
  } catch (err) {
    msg.textContent = err.message;
  }
}

async function openReview(id) {
  try {
    reviewUI.detail = await api(`/api/reviews/${id}`);
    reviewUI.tab = reviewUI.detail.status === 'closed' ? 'ranks' : 'vote';
    reviewUI.ranking = [];
    reviewUI.issueSel = null;
    reviewUI.issueNote = '';
    reviewUI.issueReport = null;
    $('reviewModal').hidden = false;
    renderReviewModal();
  } catch (err) {
    flashBanner(err.message);
  }
}

function closeReviewModal() {
  $('reviewModal').hidden = true;
  reviewUI.detail = null;
  reviewUI.ranking = [];
  reviewUI.issueSel = null;
  reviewUI.issueNote = '';
  reviewUI.issueReport = null;
}

async function reloadReviewDetail() {
  if (!reviewUI.detail) return;
  reviewUI.detail = await api(`/api/reviews/${reviewUI.detail.id}`);
}

function renderReviewModal() {
  const d = reviewUI.detail;
  if (!d) return;
  $('reviewModalTitle').textContent = d.title;
  $('reviewMeta').innerHTML = `
    <span class="tag ${d.status === 'open' ? 'commit' : 'current'}">${d.status === 'open' ? '开放中' : '已封榜'}</span>
    绑定版本 <b>v${d.version_id}</b>（不可变快照） · 候选 <b>${d.candidates.length}</b> ·
    评审人 <b>${d.reviewers.length}</b> · 最低票数 <b>${d.min_votes}</b> ·
    已提交 <b>${d.submitted_count}</b> · revision <b>r${d.revision}</b>
    ${d.closed_at ? ` · 封榜于 ${esc(d.closed_at.replace('T', ' ').replace('Z', ' UTC'))}` : ''}`;
  document.querySelectorAll('.review-tabs .seg-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.rtab === reviewUI.tab);
  });
  const body = $('reviewBody');
  if (reviewUI.tab === 'vote') renderReviewVoteTab(body, d);
  else if (reviewUI.tab === 'ranks') renderReviewRanksTab(body, d);
  else if (reviewUI.tab === 'matrix') renderReviewMatrixTab(body, d);
  else renderReviewIssuanceTab(body, d);
}

function renderReviewVoteTab(body, d) {
  if (d.status === 'closed') {
    body.innerHTML = `
      <div class="pkg-verdict ok">✔ 已封榜，选票已冻结（共 ${d.submitted_count} 票）。</div>
      <p class="muted small">名次与两两矩阵请切换到对应页签；全部选票已在「名次」页公开。</p>`;
    return;
  }
  const remaining = d.reviewers.filter((r) => !d.submitted.includes(r));
  body.innerHTML = `
    <div class="detail-row">
      <label>我是评审人（已登记 ${d.reviewers.length} 人）</label>
      <select id="rvVoter"></select>
    </div>
    <div class="detail-row">
      <label>点击候选，按根因责任从强到弱依次加入排序（须覆盖全部 ${d.candidates.length} 个候选，严格排序、不重复）</label>
      <div class="rv-chips" id="rvChips"></div>
    </div>
    <ol class="rv-ranking" id="rvRanking"></ol>
    <div class="sandbox-actions">
      <button class="btn btn-small" id="btnRvUndo">撤销末位</button>
      <button class="btn btn-small" id="btnRvClear">清空</button>
      <button class="btn btn-small btn-primary" id="btnRvSubmit">提交选票</button>
    </div>
    <p class="muted small" id="rvVoteMsg"></p>
    <div class="rv-submitted">
      <b>已提交 ${d.submitted_count} / ${d.reviewers.length}</b>（封榜前不公开任何排序）：
      ${d.submitted.length
        ? d.submitted.map((r) => `<span class="rv-name">${esc(r)}</span>`).join('')
        : '<span class="muted small">暂无</span>'}
      ${remaining.length ? `<div class="muted small">未提交：${remaining.map(esc).join('、')}</div>` : ''}
    </div>
    <div class="rv-close-box">
      <div class="kv">封榜将校验 revision 并冻结全部选票：当前 <b>r${d.revision}</b> ·
        已提交 <b>${d.submitted_count}</b> / 最低 <b>${d.min_votes}</b> 票</div>
      <button class="btn btn-small btn-warn" id="btnRvClose"
              ${d.submitted_count < d.min_votes ? 'disabled' : ''}>封榜并计票</button>
      <p class="muted small" id="rvCloseMsg"></p>
    </div>`;
  const sel = $('rvVoter');
  for (const name of d.reviewers) {
    const o = hEl('option', { value: name });
    o.textContent = name + (d.submitted.includes(name) ? '（已提交，可覆盖）' : '');
    sel.appendChild(o);
  }
  if (reviewUI.voter && d.reviewers.includes(reviewUI.voter)) sel.value = reviewUI.voter;
  reviewUI.voter = sel.value;
  sel.addEventListener('change', () => { reviewUI.voter = sel.value; });

  const chips = $('rvChips');
  for (const c of d.candidates) {
    const b = hEl('button', { class: 'rv-chip', type: 'button' });
    b.textContent = c.title;
    b.addEventListener('click', () => {
      if (reviewUI.ranking.includes(c.node_id)) return;
      reviewUI.ranking.push(c.node_id);
      renderRvRankingEditor(d);
    });
    chips.appendChild(b);
  }
  renderRvRankingEditor(d);

  $('btnRvUndo').addEventListener('click', () => { reviewUI.ranking.pop(); renderRvRankingEditor(d); });
  $('btnRvClear').addEventListener('click', () => { reviewUI.ranking = []; renderRvRankingEditor(d); });
  $('btnRvSubmit').addEventListener('click', () => submitRvBallot(d));
  $('btnRvClose').addEventListener('click', () => closeRvSession(d));
}

function renderRvRankingEditor(d) {
  const titles = Object.fromEntries(d.candidates.map((c) => [c.node_id, c.title]));
  $('rvChips').querySelectorAll('.rv-chip').forEach((chip, i) => {
    const id = d.candidates[i].node_id;
    const pos = reviewUI.ranking.indexOf(id);
    chip.classList.toggle('used', pos >= 0);
    chip.textContent = pos >= 0 ? `${pos + 1}. ${titles[id]}` : titles[id];
  });
  const ol = $('rvRanking');
  ol.innerHTML = '';
  for (const id of reviewUI.ranking) {
    const li = hEl('li');
    li.textContent = titles[id] || id;
    ol.appendChild(li);
  }
  const left = d.candidates.length - reviewUI.ranking.length;
  $('rvVoteMsg').textContent = left > 0
    ? `还需加入 ${left} 个候选才能提交。`
    : '排序已覆盖全部候选，可提交；封榜前再次提交即覆盖自己的选票。';
  $('btnRvSubmit').disabled = left > 0;
}

async function submitRvBallot(d) {
  if (!reviewUI.voter) { $('rvVoteMsg').textContent = '请先选择评审人。'; return; }
  if (reviewUI.ranking.length !== d.candidates.length) return;
  try {
    await api(`/api/reviews/${d.id}/ballots`, {
      method: 'POST',
      body: JSON.stringify({ reviewer: reviewUI.voter, ranking: reviewUI.ranking }),
    });
    toast('选票已提交');
    reviewUI.ranking = [];
    await reloadReviewDetail();
    renderReviewModal();
    loadReviews();
  } catch (err) {
    if (err.payload?.code === 'session_closed') {
      await reloadReviewDetail();
      renderReviewModal();
      toast(err.message);
      return;
    }
    $('rvVoteMsg').textContent = err.message;
  }
}

async function closeRvSession(d) {
  if (!confirm(`确定封榜？封榜后选票冻结、按 Schulze 法计票，且不可再投票。（当前 revision r${d.revision}）`)) return;
  try {
    reviewUI.detail = await api(`/api/reviews/${d.id}/close`, {
      method: 'POST',
      body: JSON.stringify({ expected_revision: d.revision }),
    });
    toast('已封榜，Schulze 计票完成');
    reviewUI.tab = 'ranks';
    renderReviewModal();
    loadReviews();
  } catch (err) {
    // 409：revision 过期 / 重复封榜 / 未达最低票数 —— 刷新详情后展示原因
    await reloadReviewDetail();
    renderReviewModal();
    const msgEl = $('rvCloseMsg');
    if (msgEl) msgEl.textContent = err.message;
    else toast(err.message);
  }
}

function renderReviewRanksTab(body, d) {
  if (d.status !== 'closed' || !d.result) {
    body.innerHTML = '<p class="muted">封榜后在此按 Schulze 胜出关系逐层生成并列名次，并公开全部选票。</p>';
    return;
  }
  const titles = Object.fromEntries(d.candidates.map((c) => [c.node_id, c.title]));
  const nameOf = (id) => titles[id] || id;
  const layers = d.result.ranks.map((layer) => `
    <div class="rank-layer">
      <span class="rank-no">第 ${layer.rank} 名</span>
      <span>${layer.node_ids.map((id) =>
        `<span class="rank-node" title="${esc(id)}">${esc(nameOf(id))}</span>`).join('<span class="rank-tie">＝</span>')}</span>
      ${layer.node_ids.length > 1 ? '<span class="muted small">（算法并列，顺序仅按 ID 稳定展示）</span>' : ''}
    </div>`).join('');
  const ballots = d.result.ballots.map((b) => `
    <div class="rv-ballot"><b>${esc(b.reviewer)}</b>
      <span class="muted small"> · ${esc((b.submitted_at || '').replace('T', ' ').replace('Z', ' UTC'))}</span><br/>
      ${b.ranking.map((id) => esc(nameOf(id))).join(' ＞ ')}</div>`).join('');
  body.innerHTML = `
    <div class="window-title">Schulze 名次（按胜出关系逐层生成，同层即算法并列）</div>
    ${layers}
    <div class="window-title" style="margin-top:10px">已公开选票（${d.result.ballots.length}）</div>
    ${ballots}`;
}

function renderReviewMatrixTab(body, d) {
  if (d.status !== 'closed' || !d.result) {
    body.innerHTML = '<p class="muted">封榜后在此展示两两偏好矩阵与最强路径矩阵。</p>';
    return;
  }
  const r = d.result;
  const order = r.candidate_order;
  const titles = Object.fromEntries(d.candidates.map((c) => [c.node_id, c.title]));
  const table = (data, cls) => {
    const head = `<tr><th></th>${order.map((id) =>
      `<th title="${esc(id)}">${esc(trunc(titles[id] || id, 4))}</th>`).join('')}</tr>`;
    const rows = order.map((i) => `<tr>
      <th title="${esc(i)}">${esc(trunc(titles[i] || i, 6))}</th>
      ${order.map((j) => {
        if (i === j) return '<td class="diag">—</td>';
        const v = data[i][j];
        const win = v > data[j][i];
        return `<td class="${win ? 'win' : 'lose'} clickable" data-from="${esc(i)}" data-to="${esc(j)}">${v}</td>`;
      }).join('')}
    </tr>`).join('');
    return `<div class="matrix-wrap"><table class="matrix-table ${cls}">${head}${rows}</table></div>`;
  };
  body.innerHTML = `
    <div class="window-title">两两偏好 d：行候选严格排在列候选之前的选票数</div>
    ${table(r.pairwise, 'pw')}
    <div class="window-title" style="margin-top:10px">最强路径强度 p（Schulze）</div>
    ${table(r.strongest, 'sp')}
    <p class="muted small">绿色 = 行候选在该对胜负中胜出（严格大于反向值）。
      点击「最强路径」矩阵单元格可查看该方向的确定性最强路径见证。</p>
    <div class="witness-box" id="rvWitness" hidden></div>`;
  body.querySelectorAll('.matrix-table.sp td.clickable').forEach((td) => {
    td.addEventListener('click', () => {
      const from = td.dataset.from;
      const to = td.dataset.to;
      const w = r.witnesses[from]?.[to];
      const box = $('rvWitness');
      box.hidden = false;
      box.innerHTML = w
        ? `<b>${esc(titles[from] || from)} → ${esc(titles[to] || to)}</b>：最强路径强度 <b>${w.strength}</b><br/>
           见证路径：${w.path.map((id) => `「${esc(titles[id] || id)}」`).join(' → ')}<br/>
           <span class="muted small">路径上最弱一环的强度即 ${w.strength}；路径按候选 ID 确定性选取，可复现。</span>`
        : `<b>${esc(titles[from] || from)} → ${esc(titles[to] || to)}</b>：无最强路径（p=0，该方向不构成胜出）。`;
    });
  });
}

// ---------- 根因结论签发 ----------
//
// 从已封榜盲评场提交一个不可变版本的最终根因集合与签发说明：服务端在同一个 SQLite
// 写事务中重新读取评审绑定版本、Schulze 名次、证据审查与有效时间窗，逐项校验
// （恰好覆盖最高并列名次、候选间无 causes 祖先关系、证据状态不得为 insufficient /
// refuted）；任一失败逐项拒绝。通过后写入只追加的签发记录（规范化 payload + SHA-256），
// 同场唯一，后续图版本变化不影响记录；不改写图、评审票或历史快照。

const ISSUE_CHECK_LABELS = {
  session_closed: '评审场已封榜',
  not_already_issued: '同场尚未签发',
  set_well_formed: '集合合法（非空 / 不重复 / 均为候选）',
  covers_top_rank_exactly: '恰好覆盖最高并列名次',
  no_candidate_ancestor_relation: '候选间无 causes 祖先关系',
  evidence_admissible: '证据状态均可采信（非不足 / 反驳）',
  time_window_ok: '有效时间窗无冲突',
};
const ISSUE_EV_CLASS = {
  supported: 'ok', disputed: 'warn', refuted: 'bad', insufficient: 'bad',
};
const ISSUE_EV_TEXT = {
  supported: '已支持', disputed: '存在争议', refuted: '已反驳', insufficient: '证据不足',
};

function issueSelectedIds(d) {
  // 首次进入：默认勾选最高并列名次
  if (reviewUI.issueSel === null) {
    const top = d.result?.ranks?.[0]?.node_ids || [];
    reviewUI.issueSel = new Set(top);
  }
  return reviewUI.issueSel;
}

function renderReviewIssuanceTab(body, d) {
  const issued = d.issuance;
  if (issued) {
    // 评审场详情只挂接签发摘要；完整 payload（候选状态快照等）按需拉取
    if (issued.payload) { renderIssuedRecord(body, d, issued); return; }
    body.innerHTML = '<p class="muted small">正在加载签发记录…</p>';
    api(`/api/reviews/${d.id}/issuance`).then((full) => {
      reviewUI.detail.issuance = full;
      if (reviewUI.tab === 'issuance' && reviewUI.detail?.id === d.id) {
        renderIssuedRecord(body, d, full);
      }
    }).catch((err) => {
      body.innerHTML = `<div class="pkg-verdict fail">签发记录加载失败：${esc(err.message)}</div>`;
    });
    return;
  }
  if (d.status !== 'closed' || !d.result) {
    body.innerHTML = `
      <div class="pkg-verdict fail">评审场尚未封榜：只有封榜并冻结选票后，才能签发不可变的最终根因集合。</div>
      <p class="muted small">请先在「投票」页完成投票并封榜；封榜后本页将按 Schulze 最高并列名次
        重新校验覆盖性、候选间因果祖先关系与每个候选的证据状态。</p>`;
    return;
  }
  const sel = issueSelectedIds(d);
  const topIds = new Set(d.result.ranks[0].node_ids);
  const rows = d.candidates.map((c) => {
    const checked = sel.has(c.node_id) ? 'checked' : '';
    return `
      <tr class="${topIds.has(c.node_id) ? 'is-top' : ''}">
        <td><label class="issue-pick"><input type="checkbox" value="${esc(c.node_id)}" ${checked}/></label></td>
        <td>${c.rank ? `第 ${c.rank} 名${topIds.has(c.node_id) ? ' ★' : ''}` : '—'}</td>
        <td title="${esc(c.node_id)}">${esc(c.title)}</td>
      </tr>`;
  }).join('');
  body.innerHTML = `
    <p class="muted small">
      从本场<b>已冻结</b>的盲评结果签发<b>不可变</b>根因结论。提交后服务端会在同一个写事务中
      重新读取绑定版本 v${d.version_id} 的图、Schulze 名次、证据审查与有效时间窗并逐项校验；
      通过即写入只追加记录（规范化 payload + SHA-256），<b>同场不可重复签发</b>，
      后续图版本变化不影响记录，且不会改写图、选票或历史快照。
    </p>
    <div class="detail-row">
      <label>最终根因集合（须恰好覆盖最高并列名次：${d.result.ranks[0].node_ids
        .map((id) => `「${esc(d.candidates.find((c) => c.node_id === id)?.title || id)}」`).join('、')}）</label>
      <div class="matrix-wrap"><table class="issue-table">
        <thead><tr><th style="width:36px">选</th><th style="width:90px">Schulze 名次</th><th>候选事件</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>
    <div class="detail-row">
      <label>签发说明（随记录不可变保存）</label>
      <textarea id="issueNote" rows="3" maxlength="4000"
        placeholder="如：经 3 名评审人盲评封榜，确认空指针异常为本次支付链路中断的最终根因……">${esc(reviewUI.issueNote)}</textarea>
    </div>
    <div class="sandbox-actions">
      <button class="btn btn-small" id="btnIssueValidate">重新校验</button>
      <button class="btn btn-small btn-primary" id="btnIssueSubmit">签发不可变根因结论</button>
    </div>
    <div id="issueReport"><p class="muted small">点击「重新校验」可在不写库的情况下查看逐项检查结果。</p></div>`;

  body.querySelectorAll('.issue-pick input').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) sel.add(cb.value); else sel.delete(cb.value);
    });
  });
  $('issueNote').addEventListener('input', (ev) => { reviewUI.issueNote = ev.target.value; });
  $('btnIssueValidate').addEventListener('click', () => { void runIssueValidate(d); });
  $('btnIssueSubmit').addEventListener('click', () => { void submitIssuance(d); });
  // 进入页签自动预检一次（默认勾选最高并列名次）
  if (!reviewUI.issueReport) void runIssueValidate(d);
  else renderIssueReport($('issueReport'), reviewUI.issueReport, d);
}

async function runIssueValidate(d, nodeIds) {
  const ids = nodeIds || [...issueSelectedIds(d)].sort();
  const box = $('issueReport');
  if (box) box.innerHTML = '<p class="muted small">校验中…</p>';
  try {
    const rep = await api(`/api/reviews/${d.id}/issuance/validate`, {
      method: 'POST',
      body: JSON.stringify({ node_ids: ids, note: reviewUI.issueNote || null }),
    });
    reviewUI.issueReport = rep;
    if (reviewUI.tab === 'issuance' && reviewUI.detail?.id === d.id) {
      renderIssueReport($('issueReport'), rep, d);
    }
    return rep;
  } catch (err) {
    if (box) box.innerHTML = `<div class="pkg-verdict fail">校验请求失败：${esc(err.message)}</div>`;
    return null;
  }
}

function renderIssueReport(box, rep, d) {
  if (!box) return;
  const checkRows = Object.entries(ISSUE_CHECK_LABELS).map(([key, label]) => {
    const v = rep.checks?.[key];
    if (v === null || v === undefined) return '';
    const cls = v ? 'ok' : 'bad';
    const icon = v ? '✔' : '✘';
    return `<span class="pkg-check ${cls}">${icon} ${esc(label)}</span>`;
  }).join('');
  const candRows = (rep.candidates || []).map((c) => {
    const picked = c.in_submitted_set ? '<b>在集合内</b>' : '';
    const evCls = ISSUE_EV_CLASS[c.evidence_status] || '';
    const tw = c.time_window;
    const twText = tw
      ? `${fmtTimeShort(tw.effective_lower)} ~ ${fmtTimeShort(tw.effective_upper)}`
      : '—';
    return `<tr class="${c.in_submitted_set ? 'picked' : ''} ${c.is_top_rank ? 'is-top' : ''}">
      <td>${c.rank ? `第 ${c.rank} 名${c.is_top_rank ? ' ★' : ''}` : '—'}</td>
      <td title="${esc(c.node_id)}">${esc(c.title)}</td>
      <td><span class="status-chip ${c.evidence_status}">${esc(c.evidence_status_label)}</span>
        <span class="muted tiny">S=${c.support_score} R=${c.refute_score}</span></td>
      <td class="muted tiny">${esc(twText)}</td>
      <td class="muted tiny">${picked}</td>
    </tr>`;
  }).join('');
  const violationsHtml = rep.violations?.length ? `
    <div class="pkg-verdict fail">校验未通过（${rep.violations.length} 项），签发将被拒绝：</div>
    <ul class="issue-violations">
      ${rep.violations.map((v) => `<li><code>${esc(v.code)}</code> · ${esc(v.message)}</li>`).join('')}
    </ul>` : `
    <div class="pkg-verdict ok">✔ 全部检查通过，可以签发（写入后不可修改、同场不可重复签发）。</div>`;
  box.innerHTML = `
    <div class="pkg-checks">${checkRows}</div>
    ${violationsHtml}
    <div class="window-title" style="margin-top:8px">候选逐项明细（按绑定版本重算证据审查与有效时间窗）</div>
    <div class="matrix-wrap"><table class="issue-table detail">
      <thead><tr><th>名次</th><th>候选</th><th>证据状态</th><th>有效时间窗(UTC)</th><th>提交集合</th></tr></thead>
      <tbody>${candRows}</tbody>
    </table></div>
    <p class="muted tiny">★ 最高并列名次；争议（disputed）允许签发，证据不足（insufficient）与已反驳（refuted）拒绝。</p>`;
}

async function submitIssuance(d) {
  const ids = [...issueSelectedIds(d)].sort();
  const note = $('issueNote').value.trim();
  if (!ids.length) { flashBanner('最终根因集合不能为空。'); return; }
  if (!note) { flashBanner('请填写签发说明。'); return; }
  if (!confirm('确认签发？签发记录不可修改、不可删除，且同场不可重复签发。')) return;
  try {
    await api(`/api/reviews/${d.id}/issuance`, {
      method: 'POST',
      body: JSON.stringify({ node_ids: ids, note }),
    });
    toast('根因结论已签发（不可变记录）');
    await reloadReviewDetail();
    reviewUI.tab = 'issuance';
    renderReviewModal();
    loadReviews();
  } catch (err) {
    // 400：逐项校验失败，服务端随响应返回 report；409：同场已签发
    if (err.payload?.report) {
      reviewUI.issueReport = err.payload.report;
      renderIssueReport($('issueReport'), err.payload.report, d);
    }
    flashBanner('签发被拒绝：' + err.message);
    if (err.payload?.code === 'already_issued') {
      await reloadReviewDetail();
      renderReviewModal();
    }
  }
}

function renderIssuedRecord(body, d, issued) {
  const rootChips = (issued.root_cause_titles || []).map((t) =>
    `<span class="rank-node">${esc(t)}</span>`).join(' ');
  const rows = issued.payload?.candidates?.map((c) => `
    <tr class="${(issued.root_cause_node_ids || []).includes(c.node_id) ? 'picked' : ''}">
      <td>${c.rank ? `第 ${c.rank} 名` : '—'}</td>
      <td title="${esc(c.node_id)}">${esc(c.title)}</td>
      <td><span class="status-chip ${c.evidence_status}">${esc(ISSUE_EV_TEXT[c.evidence_status] || c.evidence_status)}</span></td>
    </tr>`).join('') || '';
  body.innerHTML = `
    <div class="pkg-verdict ok">
      🔒 根因结论已于 ${esc(fmtTime(issued.issued_at))} 签发（记录 #${issued.id}）。
      记录只追加、不可修改 / 删除，同场唯一；关联评审场 #${issued.session_id} 与不可变版本 v${issued.version_id}，
      后续图版本变化不影响本记录。
    </div>
    <div class="window-title">最终根因集合</div>
    <div class="rv-chips">${rootChips}</div>
    <div class="detail-row" style="margin-top:8px">
      <label>签发说明</label>
      <div class="window-box">${esc(issued.note)}</div>
    </div>
    <div class="detail-row">
      <label>SHA-256 摘要（对规范化结论 payload 的紧凑 UTF-8 JSON 计算）</label>
      <div class="mono tiny">${esc(issued.digest_algorithm)}：${esc(issued.digest)}</div>
    </div>
    <div class="sandbox-actions">
      <a class="btn btn-small" href="/api/reviews/${d.id}/issuance/download"
         download="root-cause-issuance-r${d.id}-v${issued.version_id}-${esc(issued.digest.slice(0, 8))}.json">下载签发文件（规范化 JSON）</a>
    </div>
    ${rows ? `
      <div class="window-title" style="margin-top:6px">签发时各候选状态快照（取自绑定版本 v${issued.version_id}）</div>
      <div class="matrix-wrap"><table class="issue-table detail">
        <thead><tr><th>名次</th><th>候选</th><th>证据状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : ''}
    <p class="muted tiny">本次签发未改写因果图、评审选票或任何历史快照。</p>`;
}

// ---------- 变更提案：保存 / 预览 / 三方比较裁决合并 ----------
//
// 提案保存：把当前未提交 changeset 绑定 base_version_id 存到服务端，只在对应基线上复用
//   整图校验，不生成版本（本地未提交修改保留）。
// 三方合并：勾选两份同基线、未合并提案 → merge-preview 返回自动采纳项 + 冲突清单 +
//   合并预览图；冲突逐项选 A / B / 基线 / 丢弃，提交时服务端在写事务中重合并、复查
//   提案状态与基线版本并重跑成环 / 时间窗等整图校验，全通过才生成一个 merge 版本并
//   同时标记两份提案已合并；任一失败不留版本或已合并状态。
const proposalUI = {
  list: [],
  selected: new Set(),   // 勾选参与合并的提案 id
  detail: null,          // 最近一次 merge-preview 结果
  graph: null,           // 合并预览图（snapshot 结构）
  resolutions: {},       // conflict.key -> 'a'|'b'|'base'|'drop'
  pair: null,            // [idA, idB]
};

async function loadProposals() {
  try {
    const data = await api('/api/proposals');
    proposalUI.list = data.proposals;
    renderProposalList();
  } catch (_) { /* 面板加载失败不阻断主流程 */ }
}

function renderProposalList() {
  const ul = $('proposalList');
  ul.innerHTML = '';
  if (!proposalUI.list.length) {
    const li = hEl('li');
    li.className = 'muted small';
    li.style.listStyle = 'none';
    li.textContent = '暂无提案：在画布上做出未提交修改后，填写标题并保存。';
    ul.appendChild(li);
    return;
  }
  for (const p of proposalUI.list) {
    const li = hEl('li', { class: 'review-item proposal-item' });
    if (p.status === 'merged') li.classList.add('merged');
    const checked = proposalUI.selected.has(p.id) ? 'checked' : '';
    const disabled = p.status === 'merged' ? 'disabled' : '';
    const mergedLine = p.status === 'merged'
      ? `<span class="muted small">已合并入 v${p.merged_version_id}（${esc((p.merged_at || '').replace('T', ' ').replace('Z', ' UTC'))}）</span>`
      : '';
    li.innerHTML = `
      <div class="version-head">
        <label class="pp-pick"><input type="checkbox" value="${p.id}" ${checked} ${disabled}/> #${p.id} ${esc(p.title)}</label>
        <span class="tag ${p.status === 'open' ? 'commit' : 'current'}">${p.status === 'open' ? '未合并' : '已合并'}</span>
      </div>
      <div class="version-meta">
        <span>基线 v${p.base_version_id}</span>
        <span>${p.change_count} 项修改</span>
        <span>${esc((p.created_at || '').replace('T', ' ').replace('Z', ' UTC'))}</span>
      </div>
      ${mergedLine}
      <div class="sandbox-actions" style="margin:4px 0 0">
        <button class="btn btn-small" data-act="preview">预览</button>
      </div>`;
    li.querySelector('input[type=checkbox]').addEventListener('change', (ev) => {
      const id = Number(ev.target.value);
      if (ev.target.checked) {
        proposalUI.selected.add(id);
        if (proposalUI.selected.size > 2) {
          // 只保留最近勾选的两个
          const all = [...proposalUI.selected];
          proposalUI.selected.delete(all[0]);
        }
      } else proposalUI.selected.delete(id);
      renderProposalList();
      updateMergeButtons();
    });
    li.querySelector('[data-act=preview]').addEventListener('click', () => previewProposal(p.id));
    ul.appendChild(li);
  }
  updateMergeButtons();
}

function updateMergeButtons() {
  const picks = [...proposalUI.selected];
  let btn = document.getElementById('btnPpMerge');
  if (!btn) return;
  btn.disabled = picks.length !== 2;
  btn.textContent = picks.length === 2 ? '比较这两份提案（三方合并）'
    : `勾选两份提案后比较（已选 ${picks.length}/2）`;
}

async function saveProposal() {
  const title = $('ppTitle').value.trim();
  const msg = $('ppSaveMsg');
  if (!title) { msg.textContent = '请先填写提案标题。'; msg.className = 'muted small err'; return; }
  if (!changeCount()) { msg.textContent = '画布上没有未提交修改，先编辑后再保存。'; msg.className = 'muted small err'; return; }
  try {
    const res = await api('/api/proposals', {
      method: 'POST',
      body: JSON.stringify({ title, changes: { ...S.changes, base_version_id: S.work.version_id } }),
    });
    msg.textContent = '';
    $('ppTitle').value = '';
    toast(`提案 #${res.id} 已保存（绑定 v${res.base_version_id}，未生成版本），未提交修改仍保留`);
    await loadProposals();
  } catch (err) {
    msg.textContent = '保存失败：' + err.message;
    msg.className = 'muted small err';
    // 复用错误高亮（成环 / 时间窗）
    showError(err);
  }
}

/** 单提案只读预览：画布载入「基线 + 该提案」的图。 */
async function previewProposal(id) {
  try {
    proposalUI.detail = null;
    proposalUI.pair = null;
    const data = await api(`/api/proposals/${id}/preview`);
    enterPreview(data.graph, {
      kind: 'proposal',
      badge: `提案 #${id} 预览`,
      bannerText: `正在预览提案 #${id}「${data.proposal.title}」应用到基线 v${data.base_version_id} 的结果（只读，未落库）。`,
      id,
    });
    toast(`提案 #${id} 预览（不写库、不生成版本）`);
  } catch (err) {
    flashBanner('预览失败：' + err.message);
  }
}

function enterPreview(graphData, state) {
  S.work = snapshotFromServer(graphData);
  S.selected = null;
  S.viewingVersion = null;
  S.preview = state;
  S.mergeHits = { nodes: new Set(), edges: new Set() };
  if (state.kind !== 'node_merge') S.nodeMerge = null;
  S.conflictHits = { nodes: new Set(), edges: new Set(), data: null };
  S.view = { ...S.view };  // 保留当前平移缩放，便于与编辑视图对照
  clearAffected();
  resetSandbox();
  hideError();
  renderAll();
  refreshVersions();
}

async function exitPreview() {
  if (!S.preview) return;
  $('mergeModal').hidden = true;
  $('nodeMergeModal').hidden = true;
  proposalUI.detail = null;
  nodeMergeUI.detail = null;
  await loadState();
}

/** 只收起裁决弹窗，保留画布上的合并预览（可点「返回裁决」继续）。 */
function hideMergeModalOnly() {
  $('mergeModal').hidden = true;
}

/** 从画布重新打开裁决弹窗（沿用最近一次 merge-preview 与已选裁决）。 */
function reopenMergeDialog() {
  if (!proposalUI.detail) return;
  renderMergeDialog();
  $('mergeModal').hidden = false;
}

// ---------- 三方合并弹窗 ----------

async function openMergeDialog() {
  const picks = [...proposalUI.selected];
  if (picks.length !== 2) { flashBanner('请勾选恰好两份未合并提案。'); return; }
  const [a, b] = picks;
  proposalUI.pair = [a, b];
  proposalUI.resolutions = {};
  $('mergeMessage').value = '';
  $('mergeMsg').textContent = '';
  $('mergeModal').hidden = false;
  await loadMergePreview();
}

async function loadMergePreview() {
  const [a, b] = proposalUI.pair;
  const msg = $('mergeMsg');
  msg.textContent = '三方比较中…';
  msg.className = 'muted small';
  try {
    const data = await api('/api/proposals/merge-preview', {
      method: 'POST',
      body: JSON.stringify({ proposal_a_id: a, proposal_b_id: b }),
    });
    proposalUI.detail = data;
    proposalUI.graph = data.merged_graph_full;
    renderMergeDialog();
    // 同时在画布载入合并预览图（未裁决时冲突项保持基线值），便于定位
    enterPreview(data.merged_graph_full, {
      kind: 'merge',
      badge: '合并预览',
      bannerText: `合并预览：提案 #${a} × #${b}（基线 v${data.base_version_id}）。冲突项在裁决前暂取基线值；点击冲突行可定位。`,
      id: null,
      pair: [a, b],
    });
    $('mergeModal').hidden = false;
  } catch (err) {
    msg.textContent = '比较失败：' + err.message;
    msg.className = 'muted small err';
  }
}

const FIELD_LABELS = {
  title: '标题', description: '描述', pos_x: '横坐标', pos_y: '纵坐标',
  earliest_time: '最早发生时间', latest_time: '最晚发生时间',
  reliability: '可靠度', source_group: '来源组', trigger_mode: '触发模式',
  kind: '连线类型', label: '连线说明', source_id: '起点', target_id: '终点',
};
const SIDE_META = {
  a: ['提案 A', 'side-a'], b: ['提案 B', 'side-b'],
  base: ['基线', 'side-base'], both_same: ['双方相同', 'side-both'],
};

function fmtVal(v) {
  if (v === null || v === undefined) return '（空 / 删除）';
  if (typeof v === 'object') {
    if ('kind' in v && 'source_id' in v) return `${v.source_id} → ${v.target_id} · ${v.kind}${v.label ? ' · ' + v.label : ''}`;
    return Object.entries(v).filter(([, x]) => x !== null && x !== '')
      .map(([k, x]) => `${FIELD_LABELS[k] || k}=${x}`).join('，') || '（空）';
  }
  return String(v);
}

function entityName(c) {
  if (c.subject === 'edge') return c.title || `${c.source_id || '?'} → ${c.target_id || '?'}`;
  return c.title || c.id;
}

function renderMergeDialog() {
  const d = proposalUI.detail;
  if (!d) return;
  const pA = d.proposal_a, pB = d.proposal_b;
  $('mergeModalTitle').textContent = `三方合并：#${pA.id} × #${pB.id}`;
  $('mergeMeta').innerHTML = `
    基线 <b>v${d.base_version_id}</b> ·
    <span class="merge-side side-a">提案 A：#${pA.id} ${esc(pA.title)}</span> ·
    <span class="merge-side side-b">提案 B：#${pB.id} ${esc(pB.title)}</span>
    · 当前最新版本 <b>v${d.current_version_id}</b>
    ${d.base_is_current ? '' : '<span class="merge-warn">⚠ 基线已不是当前版本，提交将被拒绝</span>'}`;

  // 警告：成环 / 时间窗（裁决后整图校验仍会拦截）
  const warns = [];
  if (d.cycle) warns.push(`⚠ 预览图当前成环：${d.cycle.join(' → ')}（落库前的整图校验会拒绝）`);
  if (d.time_conflict) warns.push(`⚠ 预览图存在时间窗冲突：${esc(d.time_conflict.node_title)}（落库将被拒绝）`);
  if (d.dangling && d.dangling.length) warns.push(`⚠ 有 ${d.dangling.length} 条连线端点已不存在，需要先裁决相关节点冲突`);
  $('mergeWarnings').innerHTML = warns.length
    ? warns.map((w) => `<div class="merge-warn">${w}</div>`).join('') : '';

  // 自动采纳
  $('mergeAutoCount').textContent = d.auto_resolved.length;
  $('mergeAuto').innerHTML = d.auto_resolved.length
    ? d.auto_resolved.map(autoRow).join('') : '<p class="muted small">无自动采纳项。</p>';

  // 冲突
  $('mergeConflictCount').textContent = d.conflicts.length;
  $('mergeConflicts').innerHTML = d.conflicts.length
    ? d.conflicts.map(conflictRow).join('')
    : '<p class="muted small">没有冲突，全部修改可自动合并，直接填写说明后生成合并版本即可。</p>';

  d.conflicts.forEach((c) => {
    const chosen = proposalUI.resolutions[c.key];
    document.querySelectorAll(`input[name="${cssName(c.key)}"]`).forEach((radio) => {
      radio.checked = (radio.value === chosen) || (!chosen && radio.value === 'base');
      radio.addEventListener('change', () => {
        proposalUI.resolutions[c.key] = radio.value;
        updateMergeCommitState();
      });
    });
    document.querySelectorAll(`[data-locate="${esc(c.key)}"]`).forEach((btn) => {
      btn.addEventListener('click', () => locateConflict(c));
    });
  });
  updateMergeCommitState();
}

function cssName(key) { return 'cf-' + key.replace(/[^A-Za-z0-9_-]/g, '_'); }

function autoRow(x) {
  const [label, cls] = SIDE_META[x.side] || [x.side, ''];
  const action = { create: '新建', delete: '删除', field_change: '修改' }[x.action] || x.action;
  const field = x.field ? ` · ${FIELD_LABELS[x.field] || x.field}` : '';
  const subj = x.subject === 'edge' ? '连线' : '节点';
  return `<div class="merge-row auto ${cls}">
    <span class="merge-kind">${action}</span>
    <span class="merge-name">${subj} ${esc(x.title || x.id)}${field}</span>
    <span class="merge-side-tag ${cls}">${label}</span>
  </div>`;
}

function conflictRow(c) {
  const subj = c.subject === 'edge' ? '连线' : '节点';
  let head;
  if (c.type === 'value_diverge') {
    head = `${subj}「${esc(entityName(c))}」的 ${esc(c.field_label || c.field)} 被改成不同值`;
  } else if (c.type === 'delete_modify') {
    const del = c.deleted_side === 'a' ? 'A' : 'B';
    const kept = c.kept_side === 'a' ? 'A' : 'B';
    head = `${subj}「${esc(entityName(c))}」：提案 ${del} 删除，提案 ${kept} 修改`;
  } else {
    head = `${subj}「${esc(entityName(c))}」被双方以相同 id 新建，但内容不同`;
  }
  const radios = c.options.map((opt) => {
    const [label, cls] = SIDE_META[opt] || [opt, ''];
    const valText = opt === 'a' ? fmtVal(c.a_value) : opt === 'b' ? fmtVal(c.b_value)
      : c.type === 'create_create' ? '双方都不采纳（丢弃该新建）' : `保留基线：${fmtVal(c.base_value)}`;
    return `<label class="cf-choice ${cls}">
      <input type="radio" name="${cssName(c.key)}" value="${opt}"/>
      <span class="merge-side-tag ${cls}">${label}</span>
      <span class="cf-val">${esc(valText.length > 120 ? valText.slice(0, 120) + '…' : valText)}</span>
    </label>`;
  }).join('');
  return `<div class="merge-row conflict" data-conflict="${esc(c.key)}">
    <div class="cf-head">
      <span>${head}</span>
      <button type="button" class="btn btn-small" data-locate="${esc(c.key)}">定位</button>
    </div>
    <div class="cf-options">${radios}</div>
  </div>`;
}

function updateMergeCommitState() {
  const d = proposalUI.detail;
  if (!d) return;
  const allChosen = d.conflicts.every((c) => proposalUI.resolutions[c.key]);
  const stale = !d.base_is_current;
  $('btnMergeCommit').disabled = !allChosen || stale || !$('mergeMessage').value.trim();
  $('mergeMsg').textContent = d.conflicts.length
    ? (allChosen ? '全部冲突已裁决，可以生成合并版本。'
      : `还有 ${d.conflicts.filter((c) => !proposalUI.resolutions[c.key]).length} 项冲突未裁决。`)
    : '无冲突。';
  $('mergeMsg').className = 'muted small';
}

/** 冲突定位：收起弹窗，在画布上居中高亮对应节点 / 连线并选中。 */
function locateConflict(c) {
  const hits = { nodes: new Set(), edges: new Set() };
  let center = null;
  if (c.subject === 'node') {
    if (S.work.nodes.has(c.id)) {
      hits.nodes.add(c.id);
      S.selected = { kind: 'node', id: c.id };
      const n = S.work.nodes.get(c.id);
      center = { x: n.pos_x + NODE_W / 2, y: n.pos_y + NODE_H / 2 };
    }
  } else {
    if (S.work.edges.has(c.id)) {
      hits.edges.add(c.id);
      S.selected = { kind: 'edge', id: c.id };
      const e = S.work.edges.get(c.id);
      const s = S.work.nodes.get(e.source_id), t = S.work.nodes.get(e.target_id);
      if (s && t) center = { x: (s.pos_x + t.pos_x) / 2 + NODE_W / 2, y: (s.pos_y + t.pos_y) / 2 };
    }
    if (c.source_id && S.work.nodes.has(c.source_id)) hits.nodes.add(c.source_id);
    if (c.target_id && S.work.nodes.has(c.target_id)) hits.nodes.add(c.target_id);
  }
  S.mergeHits = hits;
  if (center) {
    const r = svg.getBoundingClientRect();
    S.view.x = r.width / 2 - center.x * S.view.k;
    S.view.y = r.height / 2 - center.y * S.view.k;
  }
  hideMergeModalOnly();
  applyView();
  renderNodes();
  renderEdges();
  renderDetail();
  toast(`已定位冲突：${c.field_label ? entityName(c) + ' · ' + c.field_label : entityName(c)}（裁决弹窗已收起，点「返回裁决」继续）`);
}

async function previewMergeResult() {
  // 当前的客户端裁决即时映射到预览图（服务端最终仍会重新合并校验）
  const d = proposalUI.detail;
  if (!d) return;
  try {
    const g = applyResolutionsLocally(d);
    proposalUI.graph = g;
    enterPreview(g, {
      kind: 'merge',
      badge: '合并预览（已裁决）',
      bannerText: `按当前裁决生成的合并预览（仍未落库；提交时服务端会重新合并并做整图校验）。`,
      id: null, pair: proposalUI.pair,
    });
    $('mergeModal').hidden = false;
    toast('已按裁决刷新合并预览');
  } catch (err) {
    flashBanner(err.message);
  }
}

/** 轻量本地映射：基于最近一次 merge-preview 的三方图与裁决，直接改出预览图。
 *  实现上再次请求服务端代价大；这里只处理冲突节点字段 / 实体取舍，自动项已在预览图里。 */
function applyResolutionsLocally(d) {
  const g = d.merged_graph_full;
  const nodes = new Map(g.nodes.map((n) => [n.id, structuredClone(n)]));
  const edges = new Map(g.edges.map((e) => [e.id, structuredClone(e)]));
  for (const c of d.conflicts) {
    const choice = proposalUI.resolutions[c.key] || 'base';
    const pickVal = choice === 'a' ? c.a_value : choice === 'b' ? c.b_value : c.base_value;
    if (c.type === 'value_diverge') {
      const obj = c.subject === 'node' ? nodes.get(c.id) : edges.get(c.id);
      if (obj && pickVal !== null && typeof pickVal !== 'object') obj[c.field] = pickVal;
    } else if (c.type === 'create_create') {
      if (choice === 'drop') {
        if (c.subject === 'node') { nodes.delete(c.id); [...edges.values()].forEach((e) => {
          if (e.source_id === c.id || e.target_id === c.id) edges.delete(e.id);
        }); }
        else edges.delete(c.id);
      } else {
        const vals = pickVal;
        if (c.subject === 'node' && vals) {
          nodes.set(c.id, { ...nodes.get(c.id), ...vals, id: c.id });
        } else if (vals) {
          edges.set(c.id, { ...edges.get(c.id), ...vals, id: c.id });
        }
      }
    } else if (c.type === 'delete_modify') {
      if (choice === 'a' || choice === 'b') {
        if (pickVal === null) {
          // 选中删除侧
          if (c.subject === 'node') {
            nodes.delete(c.id);
            [...edges.values()].forEach((e) => {
              if (e.source_id === c.id || e.target_id === c.id) edges.delete(e.id);
            });
          } else edges.delete(c.id);
        } else if (c.subject === 'node') {
          nodes.set(c.id, { ...nodes.get(c.id), ...pickVal, id: c.id });
        } else if (pickVal && typeof pickVal === 'object') {
          edges.set(c.id, { ...edges.get(c.id), ...pickVal, id: c.id });
        }
      }
      // base：预览图（未裁决即基线占位）本身已保留基线，无需改动
    }
  }
  return { version_id: d.base_version_id, nodes: [...nodes.values()], edges: [...edges.values()] };
}

async function commitMerge() {
  const d = proposalUI.detail;
  if (!d) return;
  const message = $('mergeMessage').value.trim();
  if (!message) { flashBanner('请填写合并版本说明。'); return; }
  const missing = d.conflicts.filter((c) => !proposalUI.resolutions[c.key]);
  if (missing.length) { flashBanner(`还有 ${missing.length} 项冲突未裁决。`); return; }
  const resolutions = Object.entries(proposalUI.resolutions).map(([key, choice]) => ({ key, choice }));
  $('btnMergeCommit').disabled = true;
  try {
    const res = await api('/api/proposals/merge', {
      method: 'POST',
      body: JSON.stringify({
        proposal_a_id: proposalUI.pair[0], proposal_b_id: proposalUI.pair[1],
        message, resolutions,
      }),
    });
    toast(`已生成合并版本 v${res.version_id}，两份提案已同时标记为已合并`);
    $('mergeModal').hidden = true;
    proposalUI.selected.clear();
    await loadState();
  } catch (err) {
    $('mergeMsg').textContent = '合并被拒绝（未留下版本或已合并状态）：' + err.message;
    $('mergeMsg').className = 'muted small err';
    // 成环 / 时间窗 / 基线过期等：刷新预览与提案状态
    await loadProposals();
    if (err.payload?.code === 'stale_conflict' || err.payload?.code === 'proposal_already_merged') {
      await loadMergePreview();
    }
    $('btnMergeCommit').disabled = false;
  }
}

// ---------- 重复事件归并（当前工作图：预览 → 字段冲突逐项取值 → 确认生成 merge 版本） ----------
//
// 选择一个保留事件与一个或多个同一事故的重复事件：
// - 预览（只读，不写库）返回将被重接的 causes / supports / refutes 连线、同端点同类型
//   连线的标签合并、字段冲突（标题 / 描述 / 时间窗 / trigger_mode）与确定性拒绝路径
//   （候选互为祖先 / 自环 / 成环 / 时间窗无交集 / ALL 前因丢失 / 时间窗传播冲突）；
// - 未提交 changeset 不参与归并（面板只基于已提交当前版；存在未提交修改时禁用并提示先提交 / 放弃）；
// - 确认时服务端在单个写事务内重读当前版本、重算整份计划（不信任预览），全部通过才生成
//   kind='merge' 的新版本并返回旧节点 → 保留节点映射；既有版本 / 评审 / 提案 / 签发记录不改写。
const NM_FIELDS = ['title', 'description', 'earliest_time', 'latest_time', 'trigger_mode'];
const nodeMergeUI = {
  detail: null,          // 最近一次 node-merge/preview 响应
  resolutions: {},       // field -> 取值来源候选节点 id
  keepId: null,
  dupIds: [],
  sig: '',
  reset() {
    this.detail = null;
    this.resolutions = {};
    this.keepId = null;
    this.dupIds = [];
    this.sig = '';
    S.nodeMerge = null;
  },
};

function nodeMergeEventNodes() {
  // 归并只针对已提交当前版：历史浏览 / 提案预览时返回空（控件禁用）
  if (isReadOnly()) return [];
  return [...(S.committed?.nodes || [])].filter((n) => n.type === 'event')
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

function renderNodeMergeControls() {
  const keepSel = $('nmKeep');
  const dupBox = $('nmDups');
  const hint = $('nmHint');
  if (!keepSel || !dupBox) return;
  const readOnly = isReadOnly();

  // 只读视图（历史浏览 / 预览）不重建、不清空已选候选，只把控件禁用
  if (!readOnly) {
    const events = nodeMergeEventNodes();
    const sig = `${S.committed?.version_id ?? '-'}|${events.map((n) => n.id).join('|')}|${nodeMergeUI.keepId || ''}`;
    if (nodeMergeUI.sig !== sig) {
      nodeMergeUI.sig = sig;
      // 保留事件下拉（结构变化时重建，保留当前选择；首次默认选第一个事件）
      if (!events.some((n) => n.id === nodeMergeUI.keepId)) {
        nodeMergeUI.keepId = events.length ? events[0].id : null;
      }
      keepSel.innerHTML = '';
      for (const n of events) {
        const o = hEl('option', { value: n.id });
        o.textContent = `[${n.trigger_mode || 'ANY'}] ${n.title}（${n.id}）`;
        keepSel.appendChild(o);
      }
      keepSel.value = nodeMergeUI.keepId || '';

      // 重复事件勾选列表：排除当前保留事件与已不存在的节点
      const prevDups = new Set(nodeMergeUI.dupIds);
      nodeMergeUI.dupIds = [];
      dupBox.innerHTML = '';
      for (const n of events) {
        if (n.id === nodeMergeUI.keepId) continue;
        const label = document.createElement('label');
        label.className = 'sandbox-item';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = n.id;
        cb.checked = prevDups.has(n.id);
        if (cb.checked) nodeMergeUI.dupIds.push(n.id);
        cb.addEventListener('change', () => {
          if (cb.checked) {
            if (!nodeMergeUI.dupIds.includes(cb.value)) nodeMergeUI.dupIds.push(cb.value);
          } else {
            nodeMergeUI.dupIds = nodeMergeUI.dupIds.filter((id) => id !== cb.value);
          }
          updateNodeMergeButton();
        });
        const span = document.createElement('span');
        span.className = `mode-tag mode-${(n.trigger_mode || 'any').toLowerCase()}`;
        span.textContent = n.trigger_mode || 'ANY';
        label.appendChild(cb);
        label.appendChild(span);
        label.appendChild(document.createTextNode(`${n.title}（${n.id}）`));
        dupBox.appendChild(label);
      }
    }
  }

  keepSel.value = nodeMergeUI.keepId || '';
  keepSel.disabled = readOnly;
  dupBox.querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.checked = nodeMergeUI.dupIds.includes(cb.value);
    cb.disabled = readOnly;
  });
  updateNodeMergeButton();

  if (readOnly) {
    hint.textContent = '当前为只读视图（历史版本 / 预览）：请返回当前版后再做重复事件归并。';
    hint.className = 'muted small err';
  } else if (changeCount() > 0) {
    hint.textContent = `存在 ${changeCount()} 项未提交修改：归并只针对已提交当前版，请先提交或放弃未提交修改。`;
    hint.className = 'muted small err';
  } else {
    hint.textContent = '归并不混入任何未提交修改；确认后生成不可变新版本，旧节点 id 映射到保留节点。';
    hint.className = 'muted small';
  }
}

/** 保留事件切换：从重复列表中剔除自身并重建勾选列表。 */
function onNodeMergeKeepChange(ev) {
  nodeMergeUI.keepId = ev.target.value || null;
  nodeMergeUI.dupIds = nodeMergeUI.dupIds.filter((id) => id !== nodeMergeUI.keepId);
  nodeMergeUI.sig = '';  // 强制重建（sig 含 keepId）
  renderNodeMergeControls();
}

function updateNodeMergeButton() {
  const btn = $('btnNmPreview');
  if (!btn) return;
  const blocked = isReadOnly() || changeCount() > 0;
  btn.disabled = blocked || !nodeMergeUI.keepId || !nodeMergeUI.dupIds.length;
  btn.textContent = nodeMergeUI.dupIds.length
    ? `预览归并（保留 1 + 重复 ${nodeMergeUI.dupIds.length}）`
    : '预览归并（只读）';
}

function nodeMergeFmtVal(v) {
  if (v === null || v === undefined || v === '') return '（空 / 未设置）';
  return String(v);
}

function nodeMergeName(id) {
  const n = S.work.nodes.get(id);
  return n ? n.title : (nodeMergeUI.detail?.candidate_titles?.[id] || id);
}

function nodeMergeMoveRows(d) {
  const moves = d.edge_moves;
  const rows = [];
  const kindText = { causes: '因果 causes', supports: '证据支持 supports', refutes: '证据反驳 refutes' };
  const endText = (s, t) => `${nodeMergeName(s)} → ${nodeMergeName(t)}`;
  for (const m of moves.rewired) {
    rows.push(`<div class="merge-row auto">
      <span class="merge-kind">重接</span>
      <span class="merge-name">${kindText[m.kind] || m.kind} 连线 <code>${esc(m.edge_id)}</code></span>
      <div class="muted tiny">${esc(endText(m.old_source_id, m.old_target_id))}
        ⇒ <b>${esc(endText(m.new_source_id, m.new_target_id))}</b></div>
      ${m.label ? `<div class="muted tiny">标签：${esc(m.label)}</div>` : ''}
    </div>`);
  }
  for (const c of moves.collapsed) {
    rows.push(`<div class="merge-row conflict">
      <span class="merge-kind">合并</span>
      <span class="merge-name">${kindText[c.kind] || c.kind}：${esc(endText(c.source_id, c.target_id))}</span>
      <div class="muted tiny">保留连线 <code>${esc(c.surviving_edge_id)}</code>，
        去掉重复 ${c.removed_edge_ids.map((id) => `<code>${esc(id)}</code>`).join('、')}</div>
      <div class="muted tiny">非空标签去重合并：<b>${esc(c.label || '（空）')}</b></div>
    </div>`);
  }
  return rows.join('') || '<p class="muted small">候选事件没有需要迁移 / 合并的连线。</p>';
}

const NM_FIELD_LABELS = {
  title: '标题', description: '描述',
  earliest_time: '最早发生时间', latest_time: '最晚发生时间', trigger_mode: '触发模式',
};

function nodeMergeConflictRows(d) {
  return d.field_conflicts.map((c) => {
    const radios = c.options.map((o) => {
      const chosen = nodeMergeUI.resolutions[c.field] === o.source_node_id;
      const also = o.equivalent_node_ids.length > 1
        ? `<span class="muted tiny">（等值：${o.equivalent_node_ids.map((id) => esc(nodeMergeName(id))).join('、')}）</span>` : '';
      return `<label class="cf-choice">
        <input type="radio" name="nmf-${esc(c.field)}" value="${esc(o.source_node_id)}" ${chosen ? 'checked' : ''}/>
        <span class="merge-side-tag side-b">${esc(o.node_title || nodeMergeName(o.source_node_id))}</span>
        <span class="cf-val">${esc(nodeMergeFmtVal(o.value))}</span>${also}
      </label>`;
    }).join('');
    return `<div class="merge-row conflict" data-nmfield="${esc(c.field)}">
      <div class="cf-head"><span>保留事件的${esc(c.field_label)}取哪个候选的值？</span>
        <button type="button" class="btn btn-small" data-nm-locate-field="${esc(c.field)}">定位保留事件</button>
      </div>
      <div class="cf-options">${radios}</div>
    </div>`;
  }).join('');
}

const NM_BLOCKER_META = {
  candidate_ancestor_relation: ['候选互为原因祖先', 'bad'],
  self_loop: ['迁移形成自环', 'bad'],
  cycle: ['迁移后成环', 'bad'],
  time_window_disjoint: ['事件时间窗无交集', 'warn'],
  all_prerequisite_lost: ['ALL 节点前因丢失', 'bad'],
  time_conflict: ['归并后时间窗冲突', 'bad'],
};

function nodeMergeBlockerBox(d) {
  if (!d.blockers.length) {
    return '<div class="pkg-verdict ok">✔ 结构校验通过：不存在祖先 / 自环 / 成环 / 时间窗无交集 / ALL 前因丢失 / 传播冲突。</div>';
  }
  return '<div class="pkg-verdict fail">整次归并将被拒绝（' + d.blockers.length + ' 项确定性冲突）：</div>'
    + d.blockers.map((b, i) => {
      const [label, cls] = NM_BLOCKER_META[b.code] || [b.code, 'bad'];
      let detail = '';
      if (b.code === 'candidate_ancestor_relation') {
        detail = `见证路径：${b.path.map((id) => `「${esc(nodeMergeName(id))}」`).join(' → ')}`;
      } else if (b.code === 'self_loop') {
        detail = `连线 <code>${esc(b.edge_id)}</code>（${esc(b.kind)}）迁移后两端都指向保留事件。`;
      } else if (b.code === 'cycle') {
        detail = `环：${b.cycle.map((id) => `「${esc(nodeMergeName(id))}」`).join(' → ')}
          →「${esc(nodeMergeName(b.cycle[0]))}」`;
      } else if (b.code === 'time_window_disjoint') {
        detail = `最晚上界 ${esc(b.upper_bound)}（「${esc(nodeMergeName(b.upper_node_id))}」）
          早于最早下界 ${esc(b.lower_bound)}（「${esc(nodeMergeName(b.lower_node_id))}」）`;
      } else if (b.code === 'all_prerequisite_lost') {
        detail = `ALL 节点「${esc(b.node_title)}」前因 ${b.parent_ids_before.length} → ${b.parent_ids_after.length}：`
          + b.collapsed_parent_groups.map((g0) => `[${g0.map((id) => esc(nodeMergeName(id))).join(' + ')}]`).join('，');
      } else if (b.code === 'time_conflict') {
        detail = `冲突链：${b.chain_node_ids.map((id) => `「${esc(nodeMergeName(id))}」`).join(' → ')}`
          + `（下界 ${esc(b.lower_bound)} ↔ 上界 ${esc(b.upper_bound)}）`;
      }
      return `<div class="nm-blocker ${cls}">
        <code>${esc(b.code)}</code> <b>${label}</b>
        <button type="button" class="btn btn-small" data-nm-locate="${i}">定位</button>
        <div class="muted tiny">${esc(b.message)}</div>
        <div class="muted tiny">${detail}</div>
      </div>`;
    }).join('');
}

async function openNodeMergePreview() {
  if (isReadOnly() || changeCount() > 0) {
    flashBanner('请先返回当前版并提交 / 放弃未提交修改，再做重复事件归并。');
    return;
  }
  if (!nodeMergeUI.keepId) { flashBanner('请选择保留事件。'); return; }
  if (!nodeMergeUI.dupIds.length) { flashBanner('请至少勾选一个重复事件。'); return; }
  // 每次重新预览都清空旧裁决，避免沿用过期取值
  nodeMergeUI.resolutions = {};
  const btn = $('btnNmPreview');
  btn.disabled = true;
  try {
    const d = await api('/api/node-merge/preview', {
      method: 'POST',
      body: JSON.stringify({
        keep_id: nodeMergeUI.keepId,
        duplicate_ids: nodeMergeUI.dupIds,
        base_version_id: S.committed.version_id,
      }),
    });
    nodeMergeUI.detail = d;
    renderNodeMergeModal();
    showNodeMergePreviewGraph(d);
    $('nodeMergeModal').hidden = false;
  } catch (err) {
    flashBanner('归并预览失败：' + err.message);
  } finally {
    updateNodeMergeButton();
  }
}

function renderNodeMergeModal() {
  const d = nodeMergeUI.detail;
  if (!d) return;
  $('nodeMergeModalTitle').textContent =
    `重复事件归并：保留「${d.keep_title}」+ 重复 ${d.duplicates.length} 个`;
  $('nodeMergeMeta').innerHTML = `
    基线 <b>v${d.base_version_id}</b> · 当前最新版本 <b>v${d.current_version_id}</b>
    ${d.base_is_current ? '' : '<span class="merge-warn">⚠ 基线已过期，落库将被拒绝</span>'}
    · 保留事件 <b>${esc(d.keep_title)}</b>（${esc(d.keep_id)}）；
    重复：${d.duplicates.map((x) => esc(x.title)).join('、')}
    · 归并后返回旧节点 → 保留节点映射`;
  $('nodeMergeBlockers').innerHTML = nodeMergeBlockerBox(d);
  $('nodeMergeMoves').innerHTML = nodeMergeMoveRows(d);
  $('nodeMergeConflictCount').textContent = d.field_conflicts.length;
  $('nodeMergeConflicts').innerHTML = d.field_conflicts.length
    ? nodeMergeConflictRows(d) : '<p class="muted small">候选间所有字段取值一致，无字段冲突。</p>';

  // 单选取值
  $('nodeMergeConflicts').querySelectorAll('input[type=radio]').forEach((radio) => {
    radio.addEventListener('change', () => {
      const row = radio.closest('[data-nmfield]');
      nodeMergeUI.resolutions[row.dataset.nmfield] = radio.value;
      void refreshNodeMergePreviewGraph();
      updateNodeMergeCommitState();
    });
  });
  $('nodeMergeConflicts').querySelectorAll('[data-nm-locate-field]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = nodeMergeUI.keepId;
      S.selected = { kind: 'node', id };
      centerOnNode(id);
      renderAll();
    });
  });
  $('nodeMergeBlockers').querySelectorAll('[data-nm-locate]').forEach((btn) => {
    btn.addEventListener('click', () => locateNodeMergeBlocker(Number(btn.dataset.nmLocate)));
  });
  updateNodeMergeCommitState();
}

function updateNodeMergeCommitState() {
  const d = nodeMergeUI.detail;
  if (!d) return;
  const missing = d.field_conflicts.filter((c) => !nodeMergeUI.resolutions[c.field]).length;
  const blocked = !!d.blockers.length || missing > 0 || !d.base_is_current;
  $('btnNodeMergeCommit').disabled = blocked || !$('nodeMergeMessage').value.trim();
  let msg;
  if (d.blockers.length) msg = `存在 ${d.blockers.length} 项确定性冲突路径，整次归并将被拒绝，无法确认。`;
  else if (missing) msg = `还有 ${missing} 项字段冲突未明确取值。`;
  else if (!d.base_is_current) msg = '基线已过期，请退出预览刷新后重新归并。';
  else msg = '冲突已全部裁决，可以确认归并（写事务内服务端会再次重算全部校验）。';
  const elx = $('nodeMergeMsg');
  elx.textContent = msg;
  elx.className = d.blockers.length || missing ? 'muted small err' : 'muted small';
  $('btnNodeMergeLocateBlocker').disabled = !d.blockers.length;
}

/** 用当前字段裁决重新请求预览（仅刷新预览图与 blockers，不重置裁决）。 */
async function refreshNodeMergePreviewGraph() {
  const d0 = nodeMergeUI.detail;
  if (!d0) return;
  try {
    const d = await api('/api/node-merge/preview', {
      method: 'POST',
      body: JSON.stringify({
        keep_id: d0.keep_id,
        duplicate_ids: d0.duplicates.map((x) => x.node_id),
        field_resolutions: nodeMergeUI.resolutions,
        base_version_id: d0.base_version_id,
      }),
    });
    nodeMergeUI.detail = d;
    $('nodeMergeBlockers').innerHTML = nodeMergeBlockerBox(d);
    $('nodeMergeMoves').innerHTML = nodeMergeMoveRows(d);
    d.blockers.forEach((b, i) => {
      const btn = document.querySelector(`[data-nm-locate="${i}"]`);
      if (btn) btn.addEventListener('click', () => locateNodeMergeBlocker(i));
    });
    showNodeMergePreviewGraph(d);
    updateNodeMergeCommitState();
  } catch (err) {
    flashBanner('刷新归并预览失败：' + err.message);
  }
}

function showNodeMergePreviewGraph(d) {
  enterPreview(d.merged_graph_full, {
    kind: 'node_merge',
    badge: '归并预览（只读）',
    bannerText: `重复事件归并预览：保留「${d.keep_title}」，合并 ${d.duplicates.length} 个重复事件`
      + `（基线 v${d.base_version_id}，未落库；点「返回归并预览」继续裁决）。`,
  });
}

function centerOnNode(id) {
  const n = S.work.nodes.get(id);
  if (!n) return;
  const r = svg.getBoundingClientRect();
  S.view.x = r.width / 2 - (n.pos_x + NODE_W / 2) * S.view.k;
  S.view.y = r.height / 2 - (n.pos_y + NODE_H / 2) * S.view.k;
  applyView();
}

/** 冲突路径定位：根据 blocker 类型在预览图上高亮确定性路径。 */
function locateNodeMergeBlocker(index) {
  const d = nodeMergeUI.detail;
  if (!d || !d.blockers[index]) return;
  const b = d.blockers[index];
  const nodes = new Set();
  const edges = new Set();
  if (b.code === 'candidate_ancestor_relation') {
    b.path.forEach((id) => nodes.add(id));
    b.edge_ids.forEach((id) => edges.add(id));
  } else if (b.code === 'self_loop') {
    b.endpoints?.forEach((id) => nodes.add(id));
    nodes.add(b.node_id);
  } else if (b.code === 'cycle') {
    b.cycle.forEach((id) => nodes.add(id));
    b.edge_ids?.forEach((id) => edges.add(id));
  } else if (b.code === 'time_window_disjoint') {
    (b.node_ids || [b.lower_node_id, b.upper_node_id]).forEach((id) => nodes.add(id));
  } else if (b.code === 'all_prerequisite_lost') {
    nodes.add(b.node_id);
    b.parent_ids_before?.forEach((id) => nodes.add(id));
    b.edge_ids?.forEach((id) => edges.add(id));
  } else if (b.code === 'time_conflict') {
    b.chain_node_ids?.forEach((id) => nodes.add(id));
    b.chain_edge_ids?.forEach((id) => edges.add(id));
  }
  // 预览图里已删除的重复节点不可见，过滤掉以免中心定位落空
  const visible = [...nodes].filter((id) => S.work.nodes.has(id));
  S.nodeMerge = { nodes: new Set(visible), edges };
  const first = visible[0];
  if (first) {
    centerOnNode(first);
    S.selected = { kind: 'node', id: first };
  }
  hideNodeMergeModalOnly();
  renderAll();
  toast(`已定位冲突路径：${NM_BLOCKER_META[b.code]?.[0] || b.code}（弹窗已收起，点「返回归并预览」继续）`);
}

async function commitNodeMerge() {
  const d = nodeMergeUI.detail;
  if (!d) return;
  const message = $('nodeMergeMessage').value.trim();
  if (!message) { flashBanner('请填写归并版本说明。'); return; }
  const missing = d.field_conflicts.filter((c) => !nodeMergeUI.resolutions[c.field]).map((c) => c.field);
  if (missing.length) { flashBanner(`还有字段冲突未取值：${missing.map((f) => NM_FIELD_LABELS[f]).join('、')}`); return; }
  if (d.blockers.length) { flashBanner('存在确定性冲突路径，整次归并将被拒绝。'); return; }
  $('btnNodeMergeCommit').disabled = true;
  try {
    const res = await api('/api/node-merge/commit', {
      method: 'POST',
      body: JSON.stringify({
        keep_id: d.keep_id,
        duplicate_ids: d.duplicates.map((x) => x.node_id),
        field_resolutions: nodeMergeUI.resolutions,
        base_version_id: d.base_version_id,
        message,
      }),
    });
    toast(`已归并并生成新版本 v${res.version_id}；旧节点已映射到保留节点`);
    $('nodeMergeModal').hidden = true;
    await loadState();
  } catch (err) {
    // 落库被拒绝（事务已回滚）：展示服务端重算的确定性冲突路径
    $('nodeMergeMsg').textContent = '归并被拒绝（未生成新版本）：' + err.message;
    $('nodeMergeMsg').className = 'muted small err';
    if (err.payload?.blockers?.length) {
      nodeMergeUI.detail = { ...nodeMergeUI.detail, blockers: err.payload.blockers,
        cycle: err.payload.cycle, time_conflict: err.payload.conflict };
      $('nodeMergeBlockers').innerHTML = nodeMergeBlockerBox(nodeMergeUI.detail);
      err.payload.blockers.forEach((b, i) => {
        document.querySelector(`[data-nm-locate="${i}"]`)
          ?.addEventListener('click', () => locateNodeMergeBlocker(i));
      });
      updateNodeMergeCommitState();
    }
    $('btnNodeMergeCommit').disabled = false;
  }
}

function hideNodeMergeModalOnly() {
  $('nodeMergeModal').hidden = true;
}

function reopenNodeMergeDialog() {
  if (!nodeMergeUI.detail) return;
  renderNodeMergeModal();
  $('nodeMergeModal').hidden = false;
}

// ---------- 指针交互（平移 / 拖节点 / 拉线） ----------

const drag = { mode: null };

svg.addEventListener('pointerdown', (ev) => {
  if (ev.button !== 0) return;
  const port = ev.target.closest?.('.port');
  const nodeG = ev.target.closest?.('.node');
  const edgeG = ev.target.closest?.('.edge');

  if (port && !isReadOnly()) {
    const g = port.closest('.node');
    startConnect(g.dataset.nodeId, ev);
  } else if (nodeG) {
    startNodeDrag(nodeG.dataset.nodeId, ev);
  } else if (edgeG) {
    S.selected = { kind: 'edge', id: edgeG.dataset.edgeId };
    renderAll();
  } else {
    startPan(ev);
  }
});

function startNodeDrag(nodeId, ev) {
  if (isReadOnly()) { S.selected = { kind: 'node', id: nodeId }; renderAll(); return; }
  const w0 = toWorld(ev.clientX, ev.clientY);
  const n = S.work.nodes.get(nodeId);
  drag.mode = 'node';
  drag.nodeId = nodeId;
  drag.dx = w0.x - n.pos_x;
  drag.dy = w0.y - n.pos_y;
  drag.moved = false;
  S.selected = { kind: 'node', id: nodeId };
  svg.setPointerCapture(ev.pointerId);
  renderAll();
}

function startPan(ev) {
  drag.mode = 'pan';
  drag.startX = ev.clientX; drag.startY = ev.clientY;
  drag.vx = S.view.x; drag.vy = S.view.y;
  svg.classList.add('panning');
  svg.setPointerCapture(ev.pointerId);
}

function startConnect(sourceId, ev) {
  drag.mode = 'connect';
  drag.sourceId = sourceId;
  const src = S.work.nodes.get(sourceId);
  drag.from = anchorPoint(src, 'right');
  svg.classList.add('connecting');
  svg.setPointerCapture(ev.pointerId);
  drawTemp(drag.from, toWorld(ev.clientX, ev.clientY));
}

function drawTemp(p1, p2) {
  tempLayer.innerHTML = '';
  const dx = Math.max(40, Math.abs(p2.x - p1.x) * 0.5);
  const d = `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`;
  tempLayer.appendChild(el('path', { class: 'temp-edge', d }));
}

svg.addEventListener('pointermove', (ev) => {
  if (drag.mode === 'pan') {
    S.view.x = drag.vx + (ev.clientX - drag.startX);
    S.view.y = drag.vy + (ev.clientY - drag.startY);
    applyView();
  } else if (drag.mode === 'node') {
    const w = toWorld(ev.clientX, ev.clientY);
    const n = S.work.nodes.get(drag.nodeId);
    n.pos_x = Math.round(w.x - drag.dx);
    n.pos_y = Math.round(w.y - drag.dy);
    drag.moved = true;
    // 拖动中只更新几何，不全量重建（避免吞事件）
    positionNodeGroup(drag.nodeId, n);
    renderEdges();
  } else if (drag.mode === 'connect') {
    drawTemp(drag.from, toWorld(ev.clientX, ev.clientY));
  }
});

function positionNodeGroup(id, n) {
  const g = nodeLayer.querySelector(`[data-node-id="${CSS.escape(id)}"]`);
  if (g) g.setAttribute('transform', `translate(${n.pos_x},${n.pos_y})`);
}

svg.addEventListener('pointerup', (ev) => {
  svg.classList.remove('panning', 'connecting');
  if (drag.mode === 'node') {
    if (drag.moved && !isReadOnly()) {
      const n = S.work.nodes.get(drag.nodeId);
      stageNodeUpdate(drag.nodeId, { pos_x: n.pos_x, pos_y: n.pos_y });
    }
    renderAll();
  } else if (drag.mode === 'connect') {
    tempLayer.innerHTML = '';
    const targetG = document.elementFromPoint(ev.clientX, ev.clientY)?.closest?.('.node');
    finishConnect(targetG?.dataset.nodeId);
  }
  drag.mode = null;
});

svg.addEventListener('pointercancel', () => {
  tempLayer.innerHTML = '';
  svg.classList.remove('panning', 'connecting');
  drag.mode = null;
});

function finishConnect(targetId) {
  const sourceId = drag.sourceId;
  if (!targetId || targetId === sourceId || isReadOnly()) return;
  const src = S.work.nodes.get(sourceId);
  const dst = S.work.nodes.get(targetId);
  // 依据端点类型自动判定连线种类
  const kind = src.type === 'evidence' && dst.type === 'event'
    ? 'supports'
    : src.type === 'event' && dst.type === 'event' ? 'causes' : null;
  if (!kind) {
    flashBanner('连线规则：事件→事件（因果 causes）；证据→事件（证据连线，默认支持 supports，可在连线详情切换为反驳 refutes）。证据只能作为起点。');
    return;
  }
  if ([...S.work.edges.values()].some((e) =>
    e.source_id === sourceId && e.target_id === targetId && e.kind === kind)) {
    flashBanner('这两个节点之间已存在同类型连线。');
    return;
  }
  const edge = { id: uid('edge'), source_id: sourceId, target_id: targetId, kind, label: '', created_at: '' };
  S.work.edges.set(edge.id, edge);
  stageEdgeCreate(edge);
  afterLocalEdit();
}

// ---------- 缩放 ----------

function zoomAt(clk, factor) {
  const r = svg.getBoundingClientRect();
  const px = clk?.clientX ?? (r.left + r.width / 2);
  const py = clk?.clientY ?? (r.top + r.height / 2);
  const wx = (px - r.left - S.view.x) / S.view.k;
  const wy = (py - r.top - S.view.y) / S.view.k;
  S.view.k = Math.min(2.5, Math.max(0.3, S.view.k * factor));
  S.view.x = px - r.left - wx * S.view.k;
  S.view.y = py - r.top - wy * S.view.k;
  applyView();
}

svg.addEventListener('wheel', (ev) => {
  ev.preventDefault();
  zoomAt(ev, ev.deltaY < 0 ? 1.1 : 0.9);
}, { passive: false });

// ---------- Toast / 临时横幅 ----------

let toastTimer;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}
let bannerTimer;
function flashBanner(msg) {
  $('errorText').textContent = msg;
  $('errorBanner').hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(hideError, 4000);
}

// ---------- 控件绑定 ----------

function bindUI() {
  document.querySelectorAll('#addPanel .seg-btn').forEach((b) => b.addEventListener('click', () => {
    document.querySelectorAll('#addPanel .seg-btn').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    S.addType = b.dataset.add;
    $('evidenceFields').style.display = S.addType === 'evidence' ? '' : 'none';
  }));

  $('btnAddNode').addEventListener('click', () => {
    if (isReadOnly()) return;
    const title = $('newTitle').value.trim();
    if (!title) { $('newTitle').focus(); return; }
    let reliability = null, sourceGroup = null;
    if (S.addType === 'evidence') {
      const raw = $('newReliability').value.trim();
      reliability = raw === '' ? 0.5 : Number(raw);  // 留空默认 0.5
      if (!(reliability >= 0 && reliability <= 1)) {
        flashBanner('证据可靠度需在 0~1 之间');
        $('newReliability').focus();
        return;
      }
      sourceGroup = $('newSourceGroup').value.trim() || null;
    }
    addNodeLocal(S.addType, title, $('newDesc').value.trim(), reliability, sourceGroup);
    $('newTitle').value = ''; $('newDesc').value = '';
    if (S.addType === 'evidence') $('newSourceGroup').value = '';
    $('newTitle').focus();
  });

  $('btnCommit').addEventListener('click', commit);
  $('btnDiscard').addEventListener('click', discardChanges);
  $('commitMessage').addEventListener('input', renderHeader);
  $('btnCloseError').addEventListener('click', hideError);

  $('btnBackToCurrent').addEventListener('click', () => {
    if (S.preview) void exitPreview();
    else void backToCurrent();
  });
  $('btnRollbackView').addEventListener('click', rollbackViewing);
  $('btnRefreshVersions').addEventListener('click', refreshVersions);

  // 变更提案：保存 / 刷新 / 三方比较 / 预览 / 裁决
  $('btnPpSave').addEventListener('click', () => { void saveProposal(); });
  $('btnPpRefresh').addEventListener('click', () => { void loadProposals(); });
  $('btnPpMerge').addEventListener('click', () => { void openMergeDialog(); });
  $('btnMergeClose').addEventListener('click', hideMergeModalOnly);
  $('btnBackToMerge').addEventListener('click', () => {
    if (S.preview?.kind === 'node_merge') reopenNodeMergeDialog();
    else reopenMergeDialog();
  });
  $('mergeModal').addEventListener('click', (ev) => {
    if (ev.target === $('mergeModal')) hideMergeModalOnly();
  });
  $('btnMergePreview').addEventListener('click', () => { void previewMergeResult(); });
  $('btnMergeCommit').addEventListener('click', () => { void commitMerge(); });
  $('mergeMessage').addEventListener('input', updateMergeCommitState);

  // 重复事件归并：选择 / 预览 / 冲突定位 / 确认落库
  $('nmKeep').addEventListener('change', onNodeMergeKeepChange);
  $('btnNmPreview').addEventListener('click', () => { void openNodeMergePreview(); });
  $('btnNodeMergeClose').addEventListener('click', hideNodeMergeModalOnly);
  $('btnNodeMergeLocateBlocker').addEventListener('click', () => locateNodeMergeBlocker(0));
  $('btnNodeMergeCommit').addEventListener('click', () => { void commitNodeMerge(); });
  $('nodeMergeMessage').addEventListener('input', updateNodeMergeCommitState);
  $('nodeMergeModal').addEventListener('click', (ev) => {
    if (ev.target === $('nodeMergeModal')) hideNodeMergeModalOnly();
  });

  // 反事实干预沙盘
  $('sandboxTarget').addEventListener('change', (ev) => {
    S.sandbox.targetId = ev.target.value || null;
    if (!S.sandbox.targetId) { toggleSandboxOff(); return; }
    S.sandbox.active = true;
    void syncSandbox().then(() => { renderNodes(); renderEdges(); });
  });
  $('btnSandboxRun').addEventListener('click', () => {
    if (!S.sandbox.targetId) { flashBanner('请先选择观察目标事件。'); return; }
    S.sandbox.active = true;
    void syncSandbox().then(() => { renderNodes(); renderEdges(); });
  });
  $('btnSandboxClear').addEventListener('click', () => {
    S.sandbox.prevented.clear();
    renderSandboxControls();
    if (S.sandbox.targetId) {
      S.sandbox.active = true;
      void syncSandbox().then(() => { renderNodes(); renderEdges(); });
    } else {
      toggleSandboxOff();
    }
  });

  // 可复算复盘结论包
  $('btnPkgExport').addEventListener('click', exportConclusionPackage);
  $('btnPkgVerify').addEventListener('click', () => $('pkgFile').click());
  $('pkgFile').addEventListener('change', (ev) => {
    const f = ev.target.files?.[0];
    if (f) void verifyConclusionPackage(f);
    ev.target.value = '';  // 允许重复选择同一文件
  });

  // 根因共识盲评
  $('btnReviewNew').addEventListener('click', () => { void toggleReviewCreate(); });
  $('btnReviewRefresh').addEventListener('click', () => { void loadReviews(); });
  $('rvVersion').addEventListener('change', (ev) => { void loadRvEvents(Number(ev.target.value)); });
  $('btnReviewCreate').addEventListener('click', () => { void createReview(); });
  $('btnReviewClose').addEventListener('click', closeReviewModal);
  $('reviewModal').addEventListener('click', (ev) => {
    if (ev.target === $('reviewModal')) closeReviewModal();  // 点击遮罩关闭
  });
  document.querySelectorAll('.review-tabs .seg-btn').forEach((b) => b.addEventListener('click', () => {
    reviewUI.tab = b.dataset.rtab;
    renderReviewModal();
  }));

  $('btnZoomIn').addEventListener('click', () => zoomAt(null, 1.15));
  $('btnZoomOut').addEventListener('click', () => zoomAt(null, 0.87));
  $('btnZoomReset').addEventListener('click', () => { S.view = { x: 40, y: 30, k: 1 }; applyView(); });

  document.addEventListener('keydown', (ev) => {
    const tag = ev.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if ((ev.key === 'Delete' || ev.key === 'Backspace') && S.selected && !isReadOnly()) {
      ev.preventDefault();
      deleteSelected();
    }
    if (ev.key === 'Escape') {
      if (!$('nodeMergeModal').hidden) { hideNodeMergeModalOnly(); return; }
      if (!$('mergeModal').hidden) { hideMergeModalOnly(); return; }
      if (!$('reviewModal').hidden) { closeReviewModal(); return; }
      S.selected = null;
      renderAll();
    }
  });

  window.addEventListener('keydown', (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === 'Enter') commit();
  });
}

bindUI();
loadState().catch((err) => flashBanner('初始化失败：' + err.message));
