"""SQLite 连接管理、表结构与初始示例数据。

版本化设计：
- versions 记录每次成功提交 / 回滚产生的快照元数据；
- nodes / edges 每行都带 version_id，即"该快照中存在的元素"。
读取当前图时只查最大 version_id 的行；回滚 = 把旧快照的行复制为新版本。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "app.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('commit', 'rollback')),
    base_id     INTEGER REFERENCES versions(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id             TEXT NOT NULL,
    version_id     INTEGER NOT NULL REFERENCES versions(id),
    type           TEXT NOT NULL CHECK (type IN ('event', 'evidence')),
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    pos_x          REAL NOT NULL,
    pos_y          REAL NOT NULL,
    earliest_time  TEXT,   -- 事件最早发生时间（UTC，...Z）；证据节点恒为 NULL
    latest_time    TEXT,   -- 事件最晚发生时间（UTC，...Z）
    reliability    REAL,   -- 证据可靠度（0~1）；仅证据节点；旧快照可为 NULL
    source_group   TEXT,   -- 证据来源组；同组每方向只计最高可靠度一条
    trigger_mode   TEXT,   -- 事件触发模式 ANY（任一前因）/ ALL（全部前因）；仅事件节点非空，证据恒为 NULL
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (id, version_id),
    CHECK (type = 'evidence' OR trigger_mode IN ('ANY', 'ALL'))
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT NOT NULL,
    version_id  INTEGER NOT NULL REFERENCES versions(id),
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('causes', 'supports', 'refutes')),
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_version  ON nodes(version_id);
CREATE INDEX IF NOT EXISTS idx_edges_version  ON edges(version_id);

-- 根因共识盲评：评审场绑定某个不可变版本快照，只读写以下 review_* 表，
-- 不改写因果图、版本或结论包。
CREATE TABLE IF NOT EXISTS review_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    version_id  INTEGER NOT NULL REFERENCES versions(id),
    min_votes   INTEGER NOT NULL CHECK (min_votes >= 1),
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    revision    INTEGER NOT NULL DEFAULT 1,   -- session_revision：每次投票 / 封榜自增
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS review_candidates (
    session_id  INTEGER NOT NULL REFERENCES review_sessions(id),
    node_id     TEXT NOT NULL,
    PRIMARY KEY (session_id, node_id)
);

CREATE TABLE IF NOT EXISTS review_reviewers (
    session_id  INTEGER NOT NULL REFERENCES review_sessions(id),
    reviewer    TEXT NOT NULL,
    PRIMARY KEY (session_id, reviewer)
);

CREATE TABLE IF NOT EXISTS review_ballots (
    session_id   INTEGER NOT NULL REFERENCES review_sessions(id),
    reviewer     TEXT NOT NULL,
    ranking      TEXT NOT NULL CHECK (json_valid(ranking)),  -- JSON 数组：全部候选的严格排序
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (session_id, reviewer)
);

-- 变更提案：把一组未提交修改（changeset）保存为绑定 base_version_id 的提案，
-- 保存时只在对应基线上做整图校验，不生成版本；两份同基线、未合并的提案可经
-- 三方裁决合并为一个 kind='merge' 的版本，随后两份提案同时标记为 merged。
CREATE TABLE IF NOT EXISTS proposals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    base_version_id    INTEGER NOT NULL REFERENCES versions(id),
    changes_json       TEXT NOT NULL CHECK (json_valid(changes_json)),  -- 与 /api/changes/commit 同构的 changeset
    status             TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'merged')),
    merged_version_id  INTEGER REFERENCES versions(id),  -- 合并生成的版本（open 时恒为 NULL）
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    merged_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_proposals_base ON proposals(base_version_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

-- 根因结论签发：从已封榜盲评场签发一个不可变的最终根因集合。
-- 表为只追加（append-only）：UPDATE / DELETE 由触发器直接拒绝；
-- 每场评审场至多一条（session_id 唯一）；记录只引用版本 / 评审场 id 并自带
-- 规范化 payload 与 SHA-256 摘要，后续图版本变化不影响记录内容。
CREATE TABLE IF NOT EXISTS root_cause_issuances (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL UNIQUE REFERENCES review_sessions(id),
    version_id       INTEGER NOT NULL REFERENCES versions(id),
    note             TEXT NOT NULL,                 -- 签发说明
    payload_json     TEXT NOT NULL CHECK (json_valid(payload_json)),  -- 规范化结论 payload（紧凑、键排序）
    package_json     TEXT NOT NULL CHECK (json_valid(package_json)),  -- 含 digest 的完整签发文件
    digest_algorithm TEXT NOT NULL,
    digest           TEXT NOT NULL,                 -- payload 规范化 JSON 的 SHA-256
    issued_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TRIGGER IF NOT EXISTS trg_root_cause_issuance_no_update
BEFORE UPDATE ON root_cause_issuances
BEGIN
    SELECT RAISE(ABORT, 'root_cause_issuances 为只追加表，签发记录不可修改');
END;

CREATE TRIGGER IF NOT EXISTS trg_root_cause_issuance_no_delete
BEFORE DELETE ON root_cause_issuances
BEGIN
    SELECT RAISE(ABORT, 'root_cause_issuances 为只追加表，签发记录不可删除');
END;
"""

# (id, type, title, desc, x, y, earliest_time_utc, latest_time_utc, reliability, source_group, trigger_mode)
# 反事实示例：n-null 为 ALL 门（需「错误版本发布」与「配置开关失效」同时发生），其余事件为默认 ANY 门；
# 在沙盘中阻止任一前因即可看到 n-null 及下游被阻止。
SEED_NODES = [
    ("n-code", "event", "错误版本发布", "未灰度的构建被全量推送", 60, 200,
     "2026-09-20T02:00:30Z", "2026-09-20T02:01:00Z", None, None, "ANY"),
    ("n-config", "event", "配置开关失效", "特性开关默认值写成了 true", 60, 400,
     "2026-09-20T01:59:30Z", "2026-09-20T02:00:00Z", None, None, "ANY"),
    ("n-null", "event", "空指针异常", "支付服务读取到空的用户配置（需错误版本 + 开关失效同时成立）", 360, 300,
     "2026-09-20T02:00:00Z", "2026-09-20T02:01:30Z", None, None, "ALL"),
    ("n-outage", "event", "支付链路中断", "约 22 分钟无法下单", 660, 300,
     None, None, None, None, "ANY"),
    ("n-loss", "event", "订单损失", "峰值时段约 1.2 万笔失败", 960, 300,
     "2026-09-20T02:01:00Z", "2026-09-20T02:25:00Z", None, None, "ANY"),
    # 证据强度示例：e-log 与 e-metric 同属「监控平台」，在支持方向只有前者计权；
    # e-dissent 独立组反驳 n-null，使该事件成为 disputed；e-review 反驳 n-config。
    ("e-log", "evidence", "监控日志 E-01", "NullPointerException @ PaymentService:188，22 分钟", 360, 80,
     None, None, 0.9, "监控平台", None),
    ("e-deploy", "evidence", "发布单 R-4471", "构建时间与故障开始时间相差 40 秒", 60, 40,
     None, None, 0.75, "发布系统", None),
    ("e-metric", "evidence", "错误率指标 M-22", "错误率抬升与日志告警时间一致（与 E-01 同源）", 540, 70,
     None, None, 0.8, "监控平台", None),
    ("e-dissent", "evidence", "复盘异议 D-03", "部分失败工单早于 NPE，质疑空指针是唯一直接原因", 560, 200,
     None, None, 0.7, "内部复盘", None),
    ("e-review", "evidence", "评审记录 Q-09", "该发布未改动特性开关，开关默认值一直为 true", 60, 560,
     None, None, 0.8, "变更评审", None),
]

SEED_EDGES = [
    ("e1", "n-code", "n-null", "causes", "引入空配置路径"),
    ("e2", "n-config", "n-null", "causes", "开关误开新代码"),
    ("e3", "n-null", "n-outage", "causes", ""),
    ("e4", "n-outage", "n-loss", "causes", ""),
    ("e5", "e-log", "n-null", "supports", "时间线吻合"),
    ("e6", "e-deploy", "n-code", "supports", "发布时间线"),
    ("e7", "e-metric", "n-null", "supports", "同源指标互证（被同源去重）"),
    ("e8", "e-dissent", "n-null", "refutes", "质疑唯一直接原因"),
    ("e9", "e-review", "n-config", "refutes", "开关非本次发布改动"),
]


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表；为旧库补齐列与约束（幂等）；versions 为空时写入 v1 示例快照。"""
    conn.executescript(SCHEMA)
    _migrate_time_columns(conn)
    _migrate_evidence_columns(conn)
    _migrate_trigger_mode(conn)
    _migrate_edge_kinds(conn)
    _migrate_version_kinds(conn)
    count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    if count == 0:
        cur = conn.execute(
            "INSERT INTO versions (message, kind, base_id) VALUES (?, 'commit', NULL)",
            ("初始示例：支付链路中断复盘",),
        )
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO nodes (id, version_id, type, title, description, pos_x, pos_y, "
            "earliest_time, latest_time, reliability, source_group, trigger_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(nid, vid, t, title, desc, x, y, earliest, latest, rel, grp, trig)
             for nid, t, title, desc, x, y, earliest, latest, rel, grp, trig in SEED_NODES],
        )
        conn.executemany(
            "INSERT INTO edges (id, version_id, source_id, target_id, kind, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(eid, vid, s, t, k, label) for eid, s, t, k, label in SEED_EDGES],
        )


def _migrate_time_columns(conn: sqlite3.Connection) -> None:
    """旧版本数据库没有 earliest_time / latest_time 列，幂等补齐（历史行留空）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "earliest_time" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN earliest_time TEXT")
    if "latest_time" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN latest_time TEXT")


def _migrate_evidence_columns(conn: sqlite3.Connection) -> None:
    """旧库没有 reliability / source_group 列，幂等补齐（历史行留空，计权时按 0 处理）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "reliability" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN reliability REAL")
    if "source_group" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN source_group TEXT")


def _migrate_trigger_mode(conn: sqlite3.Connection) -> None:
    """旧库没有 trigger_mode 列：幂等补齐；事件行回填默认 ANY，证据行恒为 NULL。

    新表带 CHECK（证据节点可为 NULL，事件节点必须为 ANY/ALL），
    因此回填必须先于「新建表直接满足约束」的全新库路径之外的旧库迁移场景。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "trigger_mode" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN trigger_mode TEXT")
        conn.execute("UPDATE nodes SET trigger_mode = 'ANY' WHERE type = 'event'")


def _migrate_edge_kinds(conn: sqlite3.Connection) -> None:
    """旧库 edges.kind 的 CHECK 不含 'refutes'；SQLite 不能改 CHECK，按官方方式重建表。

    重建子表同样属于「12 步表重建」范畴：必须在 foreign_keys=OFF 下进行，否则改名后
    该表指向 versions 的外键注册会丢失（pragma 仍显示 ON，但约束不再生效）。
    幂等：检测建表 SQL 中已含 'refutes' 则跳过；历史行原样复制，不改写任何数据。
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='edges'").fetchone()
    if row is None or "'refutes'" in (row["sql"] or ""):
        return
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk_on:
        conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE edges_new (
            id          TEXT NOT NULL,
            version_id  INTEGER NOT NULL REFERENCES versions(id),
            source_id   TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('causes', 'supports', 'refutes')),
            label       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (id, version_id)
        );
        INSERT INTO edges_new (id, version_id, source_id, target_id, kind, label, created_at)
            SELECT id, version_id, source_id, target_id, kind, label, created_at FROM edges;
        DROP TABLE edges;
        ALTER TABLE edges_new RENAME TO edges;
        CREATE INDEX IF NOT EXISTS idx_edges_version ON edges(version_id);
    """)
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"edges 表重建后外键校验失败：{bad[:5]}")
    if fk_on:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_version_kinds(conn: sqlite3.Connection) -> None:
    """旧库 versions.kind 的 CHECK 不含 'merge'（提案三方合并生成）；按官方 12 步方式重建表。

    versions 被 nodes / edges / review_sessions / proposals 等子表以外键引用：
    重建期间临时关闭 foreign_keys（autocommit 连接，开关即事务边界），改名后子表的
    REFERENCES versions(id) 按表名重新解析到新表；迁移后跑 foreign_key_check 兜底。
    幂等：建表 SQL 已含 'merge' 则跳过，历史行原样复制。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='versions'"
    ).fetchone()
    if row is None or "'merge'" in (row["sql"] or ""):
        return
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk_on:
        conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript("""
        CREATE TABLE versions_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message     TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('commit', 'rollback', 'merge')),
            base_id     INTEGER REFERENCES versions(id),
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        INSERT INTO versions_new (id, message, kind, base_id, created_at)
            SELECT id, message, kind, base_id, created_at FROM versions;
        DROP TABLE versions;
        ALTER TABLE versions_new RENAME TO versions;
    """)
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:  # 理论不会发生（只改 CHECK 不动数据），防御性报错比静默破坏外键更安全
        raise RuntimeError(f"versions 表重建后外键校验失败：{bad[:5]}")
    if fk_on:
        conn.execute("PRAGMA foreign_keys = ON")
