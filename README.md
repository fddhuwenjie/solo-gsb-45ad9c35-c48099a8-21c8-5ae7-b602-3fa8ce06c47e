# 事故复盘因果图（Incident Causal Graph）

基于 **FastAPI + SQLite + 原生 JavaScript** 的单页 Web 应用，用于事故复盘时录入「事件 / 证据」节点，
在画布上拖拽连线构建因果图，并以**事务**方式整批提交修改：任何会使图成环、或使事件时间窗传播后
产生矛盾的变更都会被**整批拒绝**。每次成功提交生成不可变的**版本快照**，可浏览旧版、一键回滚，
并对指定节点重算下游影响。内置**证据强度审查**：证据带可靠度与来源组，按支持 / 反驳两个方向
聚合并自动判定每个事件「已支持 / 已反驳 / 存在争议 / 证据不足」。另有只读的**反事实干预沙盘**：
勾选若干被阻止事件并指定观察目标，按事件的 `trigger_mode`（ANY / ALL）拓扑推演 prevented /
still_active，并给出目标的递归依据树。**可复算复盘结论包**：把任一事件连同全部 causes 祖先、
关联证据、版本元数据与重算结论打包成带 SHA-256 摘要的自包含文件，可下载留存、可上传只读核验。
**根因共识盲评**：从某个不可变版本选取 3~20 个事件作为候选，登记唯一评审人并设置最低票数；
每位评审人提交覆盖全部候选的严格排序，封榜前只公开已提交名单与数量、不泄露任何排序，
封榜（写事务内校验 `session_revision` 与最低票数）后按 **Schulze 法**计算两两偏好与最强路径，
逐层生成并列名次并公开全部选票。
**变更提案合并**：可把一组未提交修改保存为**绑定 `base_version` 的提案**，保存时只在对应基线上
复用整图校验、**不生成版本**；选择两份同基线且未合并的提案做**基线 / A / B 三方比较**，
单侧修改与双方相同修改直接采纳，同字段改成不同值、删除与修改相遇列为冲突逐项裁决，
服务端在 SQLite 写事务中重新合并（复查提案未使用、当前版本仍等于基线，并再次执行成环、
时间窗等整图校验），全部通过才生成一个 `merge` 版本并同时标记两份提案已合并，任一失败
都不留下版本或已合并状态。
**重复事件归并**：在当前工作图选择一个**保留事件**与一个或多个**同一事故的重复事件**，
先只读预览将被重接的 `causes`、`supports` / `refutes` 连线、同端点同类型连线的标签合并
与字段冲突（保留节点的标题、描述、时间窗与 `trigger_mode` 必须逐项明确取值来源），
再确认归并：服务端把重复节点的入边、出边和证据边迁移到保留节点，同端点同类型只保留一条
并合并非空标签；候选互为原因祖先、迁移形成自环 / 环、事件时间窗无交集、ALL 触发节点任一
前因会丢失，或归并后时间窗传播冲突，整次操作拒绝并返回确定性冲突路径。通过后在单个 SQLite
写事务中生成新的 `merge` 版本并返回旧节点 → 保留节点映射；未提交 changeset 不混入归并，
既有版本及绑定旧版本的评审、提案、签发记录一律不改写。
**根因结论签发**：从**已封榜**的盲评场提交一个不可变版本的最终根因集合与签发说明，
服务端在同一 SQLite 写事务中重新读取评审绑定版本、Schulze 名次、证据审查与有效时间窗，
要求集合恰好覆盖最高并列名次、候选之间不存在 causes 祖先关系，且每个候选证据状态不得为
insufficient 或 refuted；任一不满足都逐项拒绝。通过后写入**不可修改**的签发记录
（只追加触发器禁止 UPDATE/DELETE），保存规范化结论 payload 与 SHA-256 摘要并关联评审场
与版本；同场不可重复签发，后续图版本变化不影响记录。
**签发有效性复核**：针对一条不可变签发记录与用户指定的目标版本，服务端在单个 SQLite
写事务中加载签发时绑定版本、签发 payload 与目标整图，按原规则重新计算 Schulze 最高并列
（目标版本中仍在的候选上重算）、causes 祖先关系、证据强度与时间窗；目标版本缺失任一根因、
候选出现祖先关系、证据变为 insufficient/refuted、时间窗冲突或评审候选/版本不一致时，
按稳定顺序返回逐项失效原因与见证路径；全部条件仍满足才标记有效，并展示相对签发版的节点、
证据与时间窗变化。复核结果同样写入只追加的不可变记录（规范化报告 + SHA-256），
同一签发与目标版本重复复核返回 409、绝不覆盖旧记录。

## ✨ 功能特性

- **画布编辑（原生 JS + SVG）**
  - 录入事件（event）与证据（evidence）节点，填写标题与描述
  - 事件节点可填写**带时区的最早 / 最晚发生时间**（均可留空）
  - 证据节点可填写**可靠度 reliability（0~1）**与**来源组 source_group**（留空为独立来源）
  - 拖拽节点移动；从节点右侧圆点拖出到目标节点即创建连线
  - 连线方向规则（由端点类型自动判定）：
    - 事件 → 事件：因果 `causes`（实线箭头，表示源事件**不晚于**目标事件）
    - 证据 → 事件：证据连线，默认支持 `supports`（虚线箭头），可在连线详情切换为反驳 `refutes`（红色点线）
  - 拖空白处平移、滚轮缩放、Delete 删除选中元素
- **证据强度审查（Evidence Strength Review）**
  - 每条证据连线带立场：`supports`（支持）或 `refutes`（反驳），二者可在连线详情中互切
  - **同源去重**：同一 `source_group` 在每个方向（支持 / 反驳）只采用 reliability **最高**的一条，
    其余标记为「同源未计权」；未填写来源组的证据各自视为独立组
  - **独立聚合**：各入选组按独立证据合并 —— 支持强度 `S = 1 − ∏(1−r)`，反驳强度 `R = 1 − ∏(1−r)`
  - **状态判定**（按顺序）：`S≥0.6 且 R≥0.6` → 存在争议（disputed）；
    `S≥0.7` → 已支持（supported）；`R≥0.7` → 已反驳（refuted）；其余 → 证据不足（insufficient）
  - 选中事件即展示 S / R、状态，以及每个来源组的**入选证据**与**同源未计权证据**清单；
    事件节点右上角有状态色点（绿=已支持，红=已反驳，黄=存在争议）
  - 未提交的工作图**实时重算**；当前版与历史快照各自按该版本数据重算，**不改写任何旧版数据**
- **整组修改 + 事务校验**
  - 新建 / 更新 / 删除节点与连线先在本地累计为一个 changeset
  - 提交时在**一个 SQLite 事务**内把变更应用到整张图并重新做 DAG 与时间窗校验
  - 成环（含自环）、悬空端点、类型不合法、重复边、时间窗矛盾等任何一项失败 → `ROLLBACK`，全部变更拒绝
  - 乐观锁：页面基于的 `base_version_id` 必须是服务端最新版本，否则拒绝
- **事件时间窗传播（UTC 统一换算）**
  - 提交的时间必须带时区（如 `2026-09-20T10:00:00+08:00` / `...Z`），后端统一换算为 UTC 存储
  - 每条 `causes` 边表示 `t(源) ≤ t(目标)`，提交时在**应用后的整张图**上传播：
    - 有效下界 = 自身与**全部祖先**下界（earliest）的最大值
    - 有效上界 = 自身与**全部后代**上界（latest）的最小值
  - 选中事件可查看有效时间窗及**收紧来源节点 / 因果链**；未提交时前端按工作图实时重算
  - 任一事件「有效下界晚于有效上界」→ 整批回滚，响应给出提供两端界限的节点与可追溯因果链，
    前端红色高亮冲突链并保留全部未提交内容
- **版本快照与回滚**
  - 每次成功提交写入一条 `versions` 记录，并把全量 nodes/edges（含时间窗与证据强度字段）复制为该版本快照
  - 右侧版本列表面板可只读浏览任意历史版本，有效时间窗与证据强度审查**按所查看版本重算**
  - 「以此版本回滚」会把旧快照复制成一个**新版本**（不删除历史，可审计）
- **下游影响重算**
  - 选中任意节点 → 「重算下游影响」：服务端沿所有出边做 BFS 分层
  - 返回受影响节点 / 事件 / 证据数量、最大层级、分层节点与路径边，并在画布上绿色高亮
  - 存在未提交修改时自动改用本地计算并标注「基于未提交工作图」
- **反事实干预沙盘（只读 Counterfactual Sandbox）**
  - 事件节点新增 `trigger_mode`：`ANY` 表示任一 `causes` 前因发生即可触发，
    `ALL` 表示全部前因均发生才触发；**没有前因的事件默认发生**（可在事件详情中切换，随事务提交）
  - 右侧沙盘面板可在**当前工作图（含未提交修改）或任一历史快照**中勾选多个**被阻止事件**（干预点）
    并指定**观察目标**，系统按拓扑顺序推演每个事件的 `prevented` / `still_active`
  - 目标返回**递归依据树**：ANY 发生时取 **ID 最小的仍发生前因**，ANY 被阻止时列出全部前因；
    ALL 发生时列出全部前因，ALL 被阻止时取 **ID 最小的已阻止前因**；干预点标记为 `intervention`，不再展开
  - 画布三类高亮：🟧 **干预点**（橙色加粗 + ⛔ 标记）、🟥 **被阻止节点**（红色虚线，含推演得出）、
    🟪 **依据树**（紫色节点描边与因果边）；面板同时给出计数与可点击跳转的依据树
  - **完全只读**：不产生 changeset、不生成版本、不写库；未提交工作图在前端本地即时推演
    （与后端同构），干净当前版 / 历史快照调 `POST /api/counterfactual/simulate`
  - `trigger_mode` 随事务提交、快照复制与回滚保存；旧数据库启动时幂等迁移，历史事件行回填默认 `ANY`
- **可复算复盘结论包（Recomputable Conclusion Package）**
  - 从当前版或任一历史快照选择一个事件，服务端收集**该事件、全部 `causes` 祖先、
    以及这些事件关联的 `supports` / `refutes 证据**，连同**版本元数据**、按包内子图重算的
    **有效时间窗**与**证据审查结果**，组成**自包含 payload**
  - 规范化：对象键按**字典序**、节点与连线按 **ID 排序**，对 payload 的**紧凑 UTF-8 JSON**
    计算 **SHA-256**；页面可下载含 digest 的 `.json` 文件（文件内容即规范化 JSON）
  - **上传核验（只读）**：先校验 digest，再**仅凭包内原始节点与连线重建 DAG**，
    重算时间窗与证据审查，与随包结果**逐字段比对**；任何差异都列出
    **JSON Pointer、包内值、重算值**；**摘要匹配且重算一致**才显示「✔ 通过」
  - 导出与核验均**不写库、不生成版本、不改变工作图**；核验甚至不接触数据库（自包含）
- **根因共识盲评（Schulze Blind Review）**
  - **建场**：从某个**不可变版本快照**选取 **3~20 个事件**作为候选，登记**唯一评审人**并设置**最低票数**；
    评审场只读绑定该版本，不改写因果图、版本或结论包
  - **盲投**：每位登记评审人提交「包含全部候选且不重复」的**严格排序**；封榜前接口只做
    存在性公开（已提交名单 + 数量），**不泄露任何排序**；可覆盖自己的选票
  - **封榜**：在 SQLite 写事务（`BEGIN IMMEDIATE`）中校验 `session_revision` 与最低票数后
    冻结选票；revision 过期、重复封榜、票数不足的封榜均返回 **409**
  - **计票**：按 **Schulze 法**计算两两偏好 `d` 与最强路径 `p`，按胜出关系**逐层生成并列名次**；
    候选 ID 仅用于稳定展示，**不打断算法并列**；任意一对胜负都返回**确定性的最强路径见证**
  - 封榜后公开全部选票，名次 / 矩阵 / 见证均可在前端「评审室」弹窗中查看
- **根因结论签发（Root-Cause Conclusion Issuance）**
  - **签发**：从**已封榜**盲评场提交最终根因集合与签发说明；服务端在**同一个 SQLite 写事务**
    （`BEGIN IMMEDIATE`）中重新读取评审绑定版本快照、Schulze 名次（冻结选票现算）、
    证据审查与有效时间窗，逐项校验：集合**恰好覆盖最高并列名次**（不缺、不多）、
    候选之间**不存在 causes 祖先关系**（逐对附确定性见证路径）、每个候选证据状态
    **不得为 insufficient 或 refuted**（仅允许 supported / disputed），以及时间窗无冲突
  - **逐项拒绝**：任一条件不满足都返回失败并在 `violations` 中逐项列出代码与原因；
    失败事务回滚，**不留下签发记录**
  - **不可变记录**：通过后写入 `root_cause_issuances`（**只追加**：触发器禁止 UPDATE/DELETE，
    `session_id` 唯一约束兜底同场不可重复签发），保存**规范化结论 payload**
    （对象键字典序、紧凑 UTF-8 JSON、整数值浮点规范化）与其 **SHA-256** 摘要，关联评审场与版本
  - **与后续版本解耦**：记录自带 payload 与摘要，图之后产生新版本 / 回滚 / 合并都**不影响记录**；
    全程**不改写**图、评审票或历史快照
  - **只读预检与展示**：`POST …/issuance/validate` 复用同一套逐项校验返回结构化检查结果
    （不写库）；前端「评审室 → 签发」页展示校验结果、逐项失败原因、候选明细与已签发记录，
    并可下载含 digest 的签发文件
- **签发有效性复核（Issuance Validity Recheck）**
  - **按原规则复核**：选择一条不可变签发记录与一个目标版本，服务端在**同一个 SQLite 写事务**
    （`BEGIN IMMEDIATE`）中加载签发时绑定版本快照、签发 payload 与目标版本整图，并由评审场
    **冻结选票**重新计算 Schulze 最高并列（在目标版本中仍存在且仍为事件的候选上重算；
    已删除 / 改类别的候选单列「评审候选 / 版本不一致」失效项）
  - **逐项失效原因（稳定顺序 + 见证路径）**：签发记录与 payload / 评审场绑定版本不一致、
    目标版本缺失任一签发根因（或不再是事件）、评审候选与目标版本不一致、签发集合不再
    恰好覆盖重算的最高并列名次、根因之间在目标版本上出现 causes 祖先关系（附按 id 升序
    DFS 的确定性 `witness_path`）、某个根因证据变为 insufficient / refuted（附 S/R）、
    目标版本整图时间窗传播冲突（附因果链节点 / 边）—— `violations` 按固定代码顺序、
    同代码按节点 / 节点对排序，逐条给出中文原因
  - **相对签发版的变化**：`changes` 给出整图节点 / 连线增删，以及每个根因相对签发版的
    节点字段变化（标题 / 描述 / 位置 / 时间窗 / trigger_mode）、证据变化（状态迁移、
    S/R 分值、入选与同源未计权证据的增减）与有效时间窗逐项变化（上下界、收紧来源、传播链）
  - **全部条件仍满足**才标记 `status="valid"`，否则 `"invalid"`；**有效与无效结果都落库**
  - **不可变、不覆盖**：结果写入 `root_cause_issuance_rechecks`（**只追加**：触发器禁止
    UPDATE/DELETE），报告为规范化 JSON（键字典序、紧凑、整数值浮点规范化）并附 SHA-256；
    `UNIQUE(issuance_id, target_version_id)` 兜底：同一签发对同一目标版本重复复核返回
    **409** `recheck_already_exists`，旧记录绝不被覆盖
  - 复核只追加新表行，**不改写**因果图、评审、签发记录或任何历史快照；
    列表 `GET /api/issuances/{id}/rechecks`（倒序），详情 `GET …/rechecks/{rid}`
- **变更提案合并（Change Proposal Merge）**
  - **保存提案**：把画布上的未提交修改（changeset）保存为绑定 `base_version_id` 的提案；
    保存时在写事务内把修改应用到**对应基线**上，复用与普通提交完全相同的整图校验
    （成环、时间窗、字段 / 连线约束），通过后只写 `proposals` 表，**不生成版本**，
    本地未提交修改原样保留（仍可继续编辑、放弃或走普通 changeset 提交）
  - **单提案预览**：`GET /api/proposals/{id}/preview` 返回基线图与「应用该提案后」的完整图，
    前端以只读图载入画布（不写库、不生成版本）
  - **三方比较**：选择两份**同基线、未合并**的提案，以基线 / 提案 A 结果 / 提案 B 结果三方比较：
    - **单侧修改**（仅 A 或仅 B 改动）与**双方相同修改**（改成同一值、同删、同建）→ 直接采纳
    - **同一节点字段 / 连线被改成不同值** → 逐字段列为冲突，可选 A / B / 基线
    - **删除与修改相遇**（一侧删除节点 / 连线，另一侧修改）→ 实体级冲突，可选删除侧 / 保留侧 / 基线
    - **双方用同一 id 新建但内容不同** → 可选 A / B / `drop`（双方都不采纳）
    - 冲突清单给出每项的基线值 / A 值 / B 值与可选项，**冲突未裁决前一律不得落库**
  - **裁决落库**：逐项选择后，服务端在单个 SQLite 写事务（`BEGIN IMMEDIATE`）中
    **重新合并**（不信任预览结果）：先复查两份提案 `status='open'`（仍未使用）、
    当前版本 `MAX(versions.id)` 仍等于 `base_version_id`，再重算三方比较并核对裁决覆盖
    全部冲突、选项合法；把裁决结果合成为新的 changeset，**再次调用同一套整图校验**
    （DFS 成环、时间上下界传播、端点 / 类型 / 重复边等）；全部通过才插入一个 `kind='merge'`
    的版本（全量快照）并在同一事务内把两份提案标记为 `merged`
  - **全成或全不成**：提案已被其他合并使用（409 `proposal_already_merged`）、基线过期
    （400 `stale_version`）、裁决与最新冲突集不一致（409 `stale_conflict`）、存在未裁决项、
    成环 / 时间窗冲突等任一失败 → `ROLLBACK`，**不留下版本或已合并状态**，可重新裁决后再提交
  - 前端「变更提案合并」面板支持**保存、勾选比较、只读预览、逐项裁决与冲突一键定位**
    （紫色高亮节点 / 连线）；原 `/api/changes/commit` 提交与历史版本浏览 / 回滚保持完全可用，
    合并版本在版本列表中以「合并」标签展示并附两份来源提案编号
- **重复事件归并（Node Merge）**
  - 在**当前工作图**（已提交当前版；存在未提交 changeset 时前端禁用并提示先提交 / 放弃）选择
    一个保留事件与一个或多个同一事故的重复事件；预览为**只读**，不写库、不生成版本
  - **连线迁移**：重复节点的 `causes` 入边 / 出边与 `supports` / `refutes` 证据边全部改接保留节点；
    迁移后**同端点同类型连线只保留一条**（取 id 最小者），非空标签去重、按字典序以 ` / ` 合并；
    预览逐项列出「重接（端点改变的单边）」与「合并（多条合一条）」清单
  - **字段冲突**：保留节点的**标题、描述、最早 / 最晚时间窗、`trigger_mode`** 在候选事件间取值
    不同时逐项列为冲突，必须显式选择取值来自哪个候选（等值候选合并展示）；位置、id、
    `created_at` 不在冲突范围，一律沿用保留节点
  - **确定性拒绝路径**：候选在 `causes` 子图上互为祖先（附 BFS 见证路径）、迁移后自环 / 成环
    （附环节点 / 边）、候选自报时间窗无交集（下界 max > 上界 min，附两端来源）、
    **ALL 触发节点的任一前因会被丢失**（不同前因映射后收缩，附前因分组与见证边）、
    或归并后整图时间窗传播冲突（附因果链）——整次操作拒绝，前端可逐项定位高亮
  - **确认落库**：服务端在单个 `BEGIN IMMEDIATE` 写事务中**重读当前版本并重算整份计划**
    （不信任预览；`base_version` 乐观校验），全部通过才插入一个 `kind='merge'` 的全量快照版本，
    返回 `node_map`（旧节点 id → 保留节点 id）、删除节点 / 连线、重接连线与合并连线分组；
    任一失败 `ROLLBACK`，不留下版本。既有版本以及绑定旧版本的评审、提案与签发记录**不改写**

## 🚀 一条命令启动

需要已安装 Docker（含 Compose 插件）：

```bash
docker compose up -d --build
```

打开 <http://localhost:8000> 即可使用（首次启动自动建表并写入一份「支付链路中断」示例快照）。

停止与清理：

```bash
docker compose down          # 停止（数据保留在 Docker volume causal-data 中）
docker compose down -v       # 停止并删除数据卷
```

## 🧑‍💻 本地开发（不使用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

SQLite 数据库默认位于 `./data/app.db`，可用环境变量 `DB_PATH` 覆盖（删除该文件即恢复初始示例）。

## 🔌 HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前最新版本的完整图（nodes + edges） |
| GET | `/api/versions` | 版本快照列表（含节点/连线计数，倒序） |
| GET | `/api/versions/{id}` | 查看指定历史版本的图 |
| POST | `/api/changes/commit` | 事务提交一组修改（见下） |
| POST | `/api/versions/rollback` | 以 `target_version_id` 生成回滚新版本 |
| GET | `/api/nodes/{id}/downstream?version_id=` | 重算某节点下游影响（不传版本号则取最新） |
| POST | `/api/counterfactual/simulate` | 反事实干预沙盘推演（只读，见下） |
| GET | `/api/conclusion/export/{event_id}?version_id=` | 导出可复算复盘结论包文件（只读，见下） |
| POST | `/api/conclusion/verify` | 上传结论包文件做只读核验（见下） |
| GET | `/api/reviews` | 根因共识盲评：评审场列表（含计数与状态） |
| POST | `/api/reviews` | 新建评审场（绑定版本 + 3~20 候选事件 + 唯一评审人 + 最低票数） |
| GET | `/api/reviews/{id}` | 评审场详情：封榜前只含已提交名单/数量，封榜后附 Schulze 结果 |
| POST | `/api/reviews/{id}/ballots` | 提交 / 覆盖选票（登记评审人 + 全候选严格排序；封榜后 409） |
| POST | `/api/reviews/{id}/close` | 封榜（写事务校验 `expected_revision` 与最低票数；过期/重复 409） |
| POST | `/api/reviews/{id}/issuance/validate` | 根因签发只读预检：逐项校验集合 / 名次覆盖 / 祖先关系 / 证据状态 / 时间窗（不写库） |
| POST | `/api/reviews/{id}/issuance` | 签发不可变根因结论（写事务内重读并逐项校验；失败逐项拒绝，同场重复 409） |
| GET | `/api/reviews/{id}/issuance` | 读取签发记录（含规范化 payload 与 SHA-256；不存在 404） |
| GET | `/api/reviews/{id}/issuance/download` | 下载含 digest 的签发文件（规范化 JSON） |
| POST | `/api/issuances/{id}/recheck` | 对不可变签发记录 × 目标版本做有效性复核（写事务重算；逐项失效原因+见证+变化；同一对重复 409） |
| GET | `/api/issuances/{id}/rechecks` | 列出一条签发记录的全部复核记录（摘要，倒序） |
| GET | `/api/issuances/{id}/rechecks/{rid}` | 读取单条复核记录（含规范化报告与 SHA-256；不存在 404） |
| GET | `/api/proposals` | 变更提案列表（含未合并 / 已合并、基线、修改项数） |
| POST | `/api/proposals` | 保存提案（绑定 `base_version_id` 的 changeset，基线整图校验，**不生成版本**） |
| GET | `/api/proposals/{id}` | 提案详情（含所存 changeset） |
| GET | `/api/proposals/{id}/preview` | 单提案只读预览：基线图 + 应用提案后的完整图 |
| POST | `/api/proposals/merge-preview` | 三方比较（只读）：自动采纳项、冲突清单、成环 / 时间窗提示与合并预览图 |
| POST | `/api/proposals/merge` | 按逐项裁决在写事务中重新合并并生成 `merge` 版本（见下） |
| POST | `/api/node-merge/preview` | 重复事件归并只读预览：连线重接 / 合并、字段冲突、确定性拒绝路径与归并预览图（见下） |
| POST | `/api/node-merge/commit` | 确认归并：写事务内重读重算，生成 `merge` 版本并返回旧节点 → 保留节点映射（见下） |
| GET | `/health` | 健康检查 |

交互文档：启动后访问 <http://localhost:8000/docs>。

### 提交示例

```bash
curl -X POST http://localhost:8000/api/changes/commit \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "补充根因：缓存雪崩",
    "changes": {
      "base_version_id": 1,
      "node_creates": [
        {"id": "ev-cache", "type": "event", "title": "缓存雪崩",
         "description": "大批 key 同时过期", "pos_x": 120, "pos_y": 560}
      ],
      "edge_creates": [
        {"id": "edge-x1", "source_id": "ev-cache", "target_id": "n-null",
         "kind": "causes", "label": "缓存未命中打满下游"}
      ],
      "node_updates": [],
      "node_deletes": [],
      "edge_updates": [],
      "edge_deletes": []
    }
  }'
```

成功返回 `{"version_id": 2, ...}`。若该连线会成环，返回 HTTP 400：

```json
{ "detail": "该变更会使图形成环：A → B → A", "code": "cycle", "cycle": ["...", "..."] }
```

### 触发模式与反事实干预沙盘示例

事件节点可设置 `trigger_mode`（`ANY` / `ALL`，缺省 `ANY`；仅事件节点可设，证据节点提交该字段返回 400
`trigger_mode_not_allowed`）。它随事务提交并写入每个快照：

```json
"node_updates": [
  {"id": "n-null", "trigger_mode": "ALL"}
]
```

初始示例中「空指针异常」就是 ALL 门（需「错误版本发布」与「配置开关失效」同时发生）。沙盘推演为
**只读**接口 —— 在请求体中给出观察目标与被阻止事件（干预点），`version_id` 留空表示当前最新版本：

```bash
curl -X POST http://localhost:8000/api/counterfactual/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "target_id": "n-loss",
    "prevented_ids": ["n-code"]
  }'
```

响应给出每个事件的拓扑推演状态与目标的递归依据树（节选）：

```json
{
  "target_id": "n-loss",
  "target_status": "prevented",
  "intervened_ids": ["n-code"],
  "prevented_ids": ["n-code", "n-loss", "n-null", "n-outage"],
  "still_active_ids": ["n-config"],
  "events": [
    {"node_id": "n-code", "trigger_mode": "ANY", "status": "prevented", "reason": "intervention"},
    {"node_id": "n-null", "trigger_mode": "ALL", "status": "prevented", "reason": "some_prevented"},
    {"node_id": "n-outage", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented"}
  ],
  "tree_edge_ids": ["e1", "e3", "e4"],
  "tree": {
    "node_id": "n-loss", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented",
    "children": [
      { "node_id": "n-outage", "trigger_mode": "ANY", "status": "prevented", "reason": "all_prevented",
        "children": [
          { "node_id": "n-null", "trigger_mode": "ALL", "status": "prevented", "reason": "some_prevented",
            "children": [
              { "node_id": "n-code", "trigger_mode": "ANY", "status": "prevented",
                "reason": "intervention", "children": [] }
            ] }
        ] }
    ]
  }
}
```

依据树子节点选择规则：

| 节点状态 | trigger_mode=ANY | trigger_mode=ALL |
| --- | --- | --- |
| still_active | 取 **ID 最小的仍发生前因**（`any_active`） | 列出**全部前因**（`all_active`） |
| prevented | 列出**全部前因**（`all_prevented`） | 取 **ID 最小的已阻止前因**（`some_prevented`） |

无前因事件的 `reason` 为 `no_causes`（默认发生），依据树无子节点；显式勾选的干预点
`reason` 为 `intervention` 且不展开。目标 / 干预点不是事件或不存在时返回 HTTP 400。
在前端，未提交工作图由本地同构引擎即时推演（画布即时高亮），历史快照与干净当前版才发请求；
任何推演都**不会**产生 changeset 或版本。

### 证据强度审查示例

证据节点带 `reliability`（0~1）与 `source_group`；证据连线 `kind` 为 `supports` 或 `refutes`：

```bash
curl -X POST http://localhost:8000/api/changes/commit \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "为链路中断补充正反证据",
    "changes": {
      "base_version_id": 1,
      "node_creates": [
        {"id": "e-sre", "type": "evidence", "title": "SRE 值班记录",
         "pos_x": 600, "pos_y": 460, "reliability": 0.6, "source_group": "值班记录"},
        {"id": "e-isp", "type": "evidence", "title": "运营商公告",
         "pos_x": 760, "pos_y": 460, "reliability": 0.7, "source_group": "运营商"}
      ],
      "edge_creates": [
        {"id": "e10", "source_id": "e-sre", "target_id": "n-outage", "kind": "supports", "label": "确认中断"},
        {"id": "e11", "source_id": "e-isp", "target_id": "n-outage", "kind": "refutes", "label": "公告说更早恢复"}
      ]
    }
  }'
```

提交后 `GET /api/state` 中每个事件节点附带按该版本重算的 `evidence_review`：

```json
{
  "id": "n-outage",
  "evidence_review": {
    "support_score": 0.6, "refute_score": 0.7, "status": "disputed",
    "groups": [
      {"group": "值班记录", "direction": "supports",
       "selected": {"node_id": "e-sre", "edge_id": "e10", "title": "SRE 值班记录",
                    "reliability": 0.6, "reliability_set": true, "source_group": "值班记录"},
       "unweighted": []},
      {"group": "运营商", "direction": "refutes",
       "selected": {"node_id": "e-isp", "edge_id": "e11", "title": "运营商公告",
                    "reliability": 0.7, "reliability_set": true, "source_group": "运营商"},
       "unweighted": []}
    ],
    "selected_evidence": [ ... ],
    "unweighted_evidence": [ ]
  }
}
```

同源去重：若 `e-a(r=0.9)` 与 `e-b(r=0.8)` 同属 `"监控平台"` 且都 supports 同一事件，
只有 `e-a` 计入 `S`，`e-b` 出现在 `unweighted_evidence`（`reason: "same_source"`）。
`source_group` 留空（或提交 `null`）的证据各自独立计权。

连线立场切换（仅 `supports` ↔ `refutes`，`causes` 不可改）：

```json
"edge_updates": [{"id": "e6", "kind": "refutes"}]
```

约束：`reliability` 必须在 0~1（否则 422）；`reliability` / `source_group` 仅证据节点可设
（事件节点提交这两个字段返回 400 `evidence_field_not_allowed`）；清除来源组时显式传
`"source_group": null`。历史快照中未设置 `reliability` 的证据按 0 参与计算，并在
`selected.reliability_set = false` 中标注。

时间字段（`earliest_time` / `latest_time`，仅事件节点，可省略或为 `null`）必须带时区：

```json
"node_updates": [
  {"id": "n-null", "earliest_time": "2026-09-20T10:00:00+08:00", "latest_time": "2026-09-20T10:01:30Z"}
]
```

> 清除某条界限时显式传 `null`（与「不更新该字段」相区别）；不传时区（如 `2026-09-20T10:00:00`）返回 422。

时间窗传播后若下界晚于上界，返回 HTTP 400：

```json
{
  "detail": "时间窗冲突：「错误版本发布」给出的下界 … 晚于「空指针异常」给出的上界 …，但因果链 … 要求源事件不晚于目标事件",
  "code": "time_conflict",
  "conflict": {
    "node_id": "n-code",
    "lower_bound": "2026-09-20T03:00:00Z",
    "upper_bound": "2026-09-20T02:01:30Z",
    "lower_node_id": "n-code", "lower_node_title": "错误版本发布",
    "upper_node_id": "n-null", "upper_node_title": "空指针异常",
    "chain_node_ids": ["n-code", "n-null"],
    "chain_edge_ids": ["e1"]
  }
}
```

前端会把 `chain_node_ids` / `chain_edge_ids` 红色高亮，且数据库中不留任何半成品（事务已回滚，未提交编辑保留在页面）。

`/api/state` 与 `/api/versions/{id}` 的每个事件节点还附带按该版本重算的有效时间窗：

```json
{
  "id": "n-null",
  "earliest_time": "2026-09-20T02:00:00Z", "latest_time": "2026-09-20T02:01:30Z",
  "time_window": {
    "earliest_time": "2026-09-20T02:00:00Z", "latest_time": "2026-09-20T02:01:30Z",
    "effective_lower": "2026-09-20T02:00:30Z",
    "effective_upper": "2026-09-20T02:01:30Z",
    "lower_source": {"node_id": "n-code", "title": "错误版本发布"},
    "upper_source": {"node_id": "n-null", "title": "空指针异常"},
    "lower_chain": ["n-code", "n-null"], "upper_chain": ["n-null"]
  }
}
```

证据节点不参与时间窗，其 `earliest_time` / `latest_time` / `time_window` 恒为 `null`。

每个事件节点还附带按同一版本重算的证据强度审查（节选）：

```json
"evidence_review": {
  "support_score": 0.9, "refute_score": 0.7, "status": "disputed",
  "groups": [
    {"group": "监控平台", "direction": "supports",
     "selected": {"node_id": "e-log", "edge_id": "e5", "title": "监控日志 E-01",
                  "reliability": 0.9, "reliability_set": true, "source_group": "监控平台"},
     "unweighted": [
       {"node_id": "e-metric", "edge_id": "e7", "title": "错误率指标 M-22",
        "reliability": 0.8, "reliability_set": true, "source_group": "监控平台",
        "reason": "same_source", "group": "监控平台", "direction": "supports"}
     ]}
  ],
  "selected_evidence": [ ... ],
  "unweighted_evidence": [ ... ]
}
```

证据节点的 `reliability` / `source_group` 随节点对象直接返回（未设置为 `null`）。

### 可复算复盘结论包示例

**导出**：`GET /api/conclusion/export/{event_id}`（`version_id` 留空取当前最新版，传历史快照 id
则按该版本导出）。响应即结论包文件（`Content-Disposition: attachment`），内容为规范化 JSON
（对象键按字典序、节点与连线按 id 排序）：

```bash
curl -OJ http://localhost:8000/api/conclusion/export/n-null
# 保存为 conclusion-n-null-v1-<digest前8位>.json；导出历史快照：
curl -OJ "http://localhost:8000/api/conclusion/export/n-null?version_id=1"
```

文件结构（节选）：

```json
{
  "format": "incident-conclusion-package",
  "format_version": 1,
  "digest": {
    "algorithm": "sha256",
    "canonicalization": "json:utf8,compact,sort_keys,integral-floats-as-int",
    "value": "36b288a4…"
  },
  "payload": {
    "meta": { "root_event_id": "n-null", "version_id": 1, "version_message": "…",
              "generated_at": "2026-09-26T…", "node_count": 8, "edge_count": 7 },
    "nodes": [ { "id": "e-deploy", "type": "evidence", … }, … ],
    "edges": [ { "id": "e1", "kind": "causes", … }, … ],
    "results": {
      "time_windows":     { "n-null": { "effective_lower": "2026-09-20T02:00:30Z", … }, … },
      "evidence_reviews": { "n-null": { "support_score": 0.9, "refute_score": 0.7,
                                        "status": "disputed", … }, … }
    }
  }
}
```

- `payload.nodes` / `payload.edges` 是**原始数据**（根事件 + 全部 causes 祖先 + 关联证据）；
  `payload.results` 是**在包内子图上重算**的时间窗与证据审查 —— 因此包是自包含的：
  结论只依赖包内数据（注意：根事件下游不在包内，其 latest 不参与有效上界）。
- `digest.value` = 对 `payload` 的紧凑 UTF-8 JSON（`sort_keys`、无空格分隔符，且整数值浮点
  如 `60.0` 规范化为 `60`）计算的 SHA-256。数字规范化使摘要可用任意标准 JSON 工具复算，例如：
  `jq -cjS .payload pkg.json | sha256sum`（`-j` 去掉末尾换行；已实测与 jq / Node `JSON.stringify` 一致）。

**核验**：`POST /api/conclusion/verify`，请求体为上传文件的原始内容（`Content-Type: application/json`）：

```bash
curl -X POST http://localhost:8000/api/conclusion/verify \
  -H 'Content-Type: application/json' \
  --data-binary @conclusion-n-null-v1-36b288a4.json
```

响应（通过时）：

```json
{
  "ok": true,
  "digest":   { "algorithm": "sha256", "expected": "36b288a4…", "actual": "36b288a4…", "match": true },
  "recompute": { "consistent": true, "error": null, "events_checked": 3,
                 "diff_count": 0, "diffs_truncated": false, "diffs": [] },
  "meta": { "root_event_id": "n-null", "version_id": 1, … }
}
```

核验分两步，**都通过** `ok` 才为 `true`（页面显示「✔ 通过」）：

1. **摘要校验**：对包内 `payload` 重新规范化并计算 SHA-256，与 `digest.value` 比对；
2. **重算比对**：仅凭 `payload.nodes` / `payload.edges` 重建 DAG，重算时间窗与证据审查，
   与 `payload.results` 逐字段比对。任何差异都在 `recompute.diffs` 中列出
   **JSON Pointer、包内值、重算值**（缺失侧为 `null`，差异最多列 200 条）：

```json
{ "pointer": "/results/evidence_reviews/n-null/support_score",
  "packaged": 0.45, "recomputed": 0.9 }
```

即使篡改者改动结果后重新计算了 digest（摘要匹配），重算比对仍会逐字段抓出差异。
文件不是合法 JSON、缺 `format` / `digest` / `payload` 或版本号不支持时返回 HTTP 400。
导出与核验均**不写库、不生成版本、不改变工作图**；核验不接触数据库。

### 根因共识盲评示例

**建场**：绑定某个不可变版本，选 3~20 个事件作为候选，登记唯一评审人并设最低票数：

```bash
curl -X POST http://localhost:8000/api/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "支付故障根因排序",
    "version_id": 1,
    "candidate_ids": ["n-code", "n-config", "n-null", "n-outage"],
    "reviewers": ["alice", "bob", "carol"],
    "min_votes": 2
  }'
```

返回评审场详情（`revision` 从 1 开始）。候选必须是该版本快照中的**事件节点**，
评审人去空白后必须唯一，`min_votes` 不得超过评审人数量，否则返回 400。

**投票**：每位登记评审人提交「包含全部候选且不重复」的严格排序；重复提交即覆盖自己的选票：

```bash
curl -X POST http://localhost:8000/api/reviews/1/ballots \
  -H 'Content-Type: application/json' \
  -d '{"reviewer": "alice", "ranking": ["n-null", "n-code", "n-outage", "n-config"]}'
```

每次投票使 `session_revision` 自增。`GET /api/reviews/1` 在封榜前只返回
`submitted`（已提交名单）与 `submitted_count`，**不包含任何排序内容**；
未登记评审人返回 403，排序缺候选 / 有重复 / 含非候选返回 400。

**封榜**：在 SQLite 写事务（`BEGIN IMMEDIATE`）中校验 `expected_revision` 等于当前
`session_revision`、已提交票数达到 `min_votes`，随后冻结选票：

```bash
curl -X POST http://localhost:8000/api/reviews/1/close \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 3}'
```

- revision 过期 → `409 {"code": "stale_revision"}`；重复封榜 → `409 {"code": "already_closed"}`；
- 未达最低票数 → `409 {"code": "min_votes_not_met"}`；封榜后再投票 → `409 {"code": "session_closed"}`。

**计票结果**（封榜后 `GET /api/reviews/1` 的 `result` 字段，节选）：

```json
{
  "method": "schulze",
  "candidate_order": ["n-code", "n-config", "n-null", "n-outage"],
  "pairwise":  { "n-null": { "n-code": 2, "n-outage": 2, "n-config": 2 }, "...": {} },
  "strongest": { "n-null": { "n-code": 2, "n-outage": 2, "n-config": 2 }, "...": {} },
  "ranks": [
    { "rank": 1, "node_ids": ["n-null"] },
    { "rank": 2, "node_ids": ["n-code", "n-outage"] },
    { "rank": 3, "node_ids": ["n-config"] }
  ],
  "witnesses": {
    "n-null": { "n-code": { "strength": 2, "path": ["n-null", "n-code"] } }
  },
  "ballots": [ { "reviewer": "alice", "ranking": ["n-null", "..."], "submitted_at": "..." } ]
}
```

- `pairwise`：`d[i][j]` = 把 i 严格排在 j 之前的选票数；
- `strongest`：Schulze 最强路径强度 `p[i][j]`（只保留 `d[i][j] > d[j][i]` 的有向边，
  路径强度 = 路径上最弱一环，取所有路径的最大值）；
- `ranks`：每轮取出「不被其余候选击败」（对所有 j 都有 `p[j][i] ≤ p[i][j]`）的集合作为
  同一**并列层**；同层即算法并列，层内按候选 ID 排序**仅为稳定展示，不打断并列**；
- `witnesses`：任意 `p[i][j] > 0` 的方向都给出确定性最强路径见证 —— 在
  「`d[u][v] > d[v][u]` 且 `d[u][v] ≥ p[i][j]`」的子图上按候选 ID 升序取第一条路径，
  其最弱边恰为 `p[i][j]`，可复现；
- `ballots`：封榜后公开全部选票，结果可离线复算。

前端在右侧「根因共识盲评」面板建场，点击评审场打开**评审室**弹窗：
「投票」页选择评审人并点击候选构建严格排序、提交 / 覆盖选票、达到最低票数后封榜；
「名次」页展示逐层并列名次与已公开选票；「两两矩阵」页展示 `d` / `p` 矩阵，
点击最强路径矩阵单元格查看该方向的见证路径。评审场只读绑定原版本快照，
**不改写因果图、版本或结论包**。

### 根因结论签发示例

**只读预检**：在封榜后的评审场上校验一个候选集合（请求体可整体省略，默认按最高并列名次），
响应给出每个条件是否通过、逐项失败原因与候选明细（名次 / 证据状态 / 有效时间窗）：

```bash
curl -X POST http://localhost:8000/api/reviews/1/issuance/validate \
  -H 'Content-Type: application/json' \
  -d '{"node_ids": ["n-null"], "note": "经盲评确认 NPE 为最终根因"}'
```

响应（节选）：`checks` 各项为布尔条件，`violations` 为逐项失败原因，
`candidates` 为每个候选在绑定版本上重算的名次 / 证据状态 / 有效时间窗。

```json
{
  "session_id": 1, "version_id": 1, "status": "closed",
  "submitted_node_ids": ["n-null"], "top_rank_node_ids": ["n-null"],
  "checks": {
    "session_closed": true,
    "not_already_issued": true,
    "set_well_formed": true,
    "covers_top_rank_exactly": true,
    "no_candidate_ancestor_relation": true,
    "evidence_admissible": true,
    "time_window_ok": true
  },
  "valid": true,
  "violations": [],
  "candidates": [
    {"node_id": "n-null", "rank": 1, "is_top_rank": true,
     "evidence_status": "disputed", "support_score": 0.97, "refute_score": 0.7,
     "time_window": {"effective_lower": "2026-09-20T02:00:30Z", "…": "…"}}
  ]
}
```

**签发**：提交最终根因集合与签发说明。服务端在**同一个 SQLite 写事务**中重新读取
评审绑定版本、Schulze 名次、证据审查与有效时间窗并逐项校验：

```bash
curl -X POST http://localhost:8000/api/reviews/1/issuance \
  -H 'Content-Type: application/json' \
  -d '{"node_ids": ["n-null"], "note": "经盲评确认：空指针异常为支付链路中断的最终根因"}'
```

校验全部通过返回 200 与签发记录（节选）：

```json
{
  "id": 1, "session_id": 1, "version_id": 1,
  "note": "经盲评确认：空指针异常为支付链路中断的最终根因",
  "issued_at": "2026-10-01T08:00:00Z",
  "digest_algorithm": "sha256",
  "digest": "adcba1bd13cf…",
  "root_cause_node_ids": ["n-null"],
  "root_cause_titles": ["空指针异常"],
  "payload": { "format": "root-cause-issuance", "meta": { "…": "…" },
               "root_causes": [ { "…": "…" } ], "candidates": [ { "…": "…" } ],
               "schulze": { "method": "schulze", "ranks": [ { "…": "…" } ] } }
}
```

- 摘要口径与结论包一致：`digest = SHA-256(紧凑 UTF-8 JSON(payload))`
  （对象键字典序、整数值浮点规范化），可用 `jq -cjS .payload file.json | sha256sum` 复算；
  下载接口 `GET /api/reviews/1/issuance/download` 返回含 `digest` 的完整签发文件。
- 任一条件不满足 → HTTP 400，响应顶层与 `violations` 数组**逐项**说明原因，事务回滚不留记录：

| `violations[].code` | 触发条件 |
| --- | --- |
| `session_not_closed` | 评审场尚未封榜 |
| `already_issued` | 该场已签发（HTTP 409，同场不可重复签发） |
| `empty_set` / `duplicate_node` / `not_candidate` | 集合为空 / 有重复 / 含非本场候选 |
| `missing_top_rank` | 缺少最高并列名次中的候选（未完整覆盖） |
| `extra_not_top_rank` | 混入了不在最高并列名次的候选 |
| `candidate_ancestor_relation` | 两个候选之间存在 causes 祖先关系（附 `witness_path` 见证路径） |
| `evidence_not_admissible` | 候选证据状态为 `insufficient` 或 `refuted`（附 S/R 分值） |
| `time_window_conflict` | 绑定版本重算有效时间窗存在冲突 |
| `note_empty` / `note_too_long` | 签发说明为空或过长 |

签发记录为**只追加**：`root_cause_issuances` 上的触发器直接拒绝 UPDATE / DELETE，
`session_id` UNIQUE 约束兜底并发下的重复签发。记录只引用评审场与版本 id 并自带
规范化 payload 与摘要，因此后续图版本提交、回滚或合并都**不影响**既有记录；
签发过程**不改写**因果图、评审选票或任何历史快照。前端在「评审室 → 签发」页
勾选集合（默认勾选最高并列名次）、填写说明、查看实时预检结果与逐项失败原因；
签发后该页以只读形式展示最终根因、签发说明、摘要、候选状态快照与下载入口，
评审场列表与详情会出现「已签发 🔒」标记。

### 签发有效性复核示例

签发记录是**不可变**的，但图在后续归并、编辑或回滚后会产生新版本，原结论可能不再成立。
对一条签发记录（路径参数为签发记录 `id`，可从签发响应或评审场详情获得）与目标版本发起复核：

```bash
curl -X POST http://localhost:8000/api/issuances/1/recheck \
  -H 'Content-Type: application/json' \
  -d '{"target_version_id": 3, "note": "节点归并后复核签发结论"}'
```

服务端在**单个 SQLite 写事务**中加载签发时绑定版本快照、签发 payload 与 v3 整图，
由评审场冻结选票重新计算 Schulze 最高并列，并在目标版本上重算 causes 祖先关系、
证据强度与有效时间窗。全部条件仍满足时返回 201、`status="valid"`（节选）：

```json
{
  "id": 2, "issuance_id": 1, "target_version_id": 3, "status": "valid", "valid": true,
  "violation_count": 0, "checked_at": "2026-10-02T09:00:00Z",
  "digest_algorithm": "sha256", "digest": "f1885318…",
  "root_cause_node_ids": ["n-null"],
  "report": {
    "checks": {
      "issuance_bound_version_consistent": true,
      "roots_present_in_target": true,
      "review_candidates_consistent": true,
      "covers_top_rank_exactly": true,
      "no_candidate_ancestor_relation": true,
      "evidence_admissible": true,
      "time_window_ok": true
    },
    "violations": [],
    "changes": {
      "graph": {"issued": {"node_count": 10, "edge_count": 9},
                "target": {"node_count": 11, "edge_count": 10}},
      "nodes_added": [{"id": "e-cfg2", "type": "evidence", "title": "配置变更确认单"}],
      "nodes_removed": [],
      "edges_added": [{"id": "e12", "kind": "supports", "source_id": "e-cfg2",
                       "target_id": "n-config", "label": "开关确实被改"}],
      "edges_removed": [],
      "root_nodes": {"n-null": {"present": true, "field_changes": [ ]}},
      "root_evidence": {"n-null": {"status_change": null, "score_changes": [],
                                   "selected_added": [], "selected_removed": []}},
      "root_time_windows": {"n-null": {"field_changes": [
        {"field": "effective_lower",
         "issued_value": "2026-09-20T02:00:30Z", "target_value": "2026-09-20T02:00:45Z"}]}}
    }
  }
}
```

任一条件不满足仍返回 201 并落库，`status="invalid"`、`violations` 按**稳定顺序**逐项列出
代码、原因与见证路径（`witness_path` 为 causes 节点链；时间窗冲突另附 `witness_edge_ids`）。
例如目标版本中两个根因被新的 causes 边连通：

```json
{
  "status": "invalid", "valid": false, "violation_count": 1,
  "report": {
    "violations": [
      {"code": "candidate_ancestor_relation",
       "ancestor_node_id": "n-config", "descendant_node_id": "n-code",
       "ancestor_title": "配置开关失效", "descendant_title": "错误版本发布",
       "witness_path": ["n-config", "n-code"],
       "message": "目标版本中「配置开关失效」成为「错误版本发布」的 causes 祖先，根因之间不得存在因果祖先关系"}
    ]
  }
}
```

逐项失效原因代码（`violations[]` 按此代码顺序、同代码按节点 / 节点对排序）：

| `violations[].code` | 触发条件 | 见证 |
| --- | --- | --- |
| `issuance_bound_version_mismatch` | 签发记录 / payload / 评审场三处绑定版本不一致 | 三个版本 id |
| `root_missing_in_target` | 某签发根因在目标版本中已被删除 / 归并 | `witness_path: [node]` |
| `root_not_event_in_target` | 某签发根因在目标版本中不再是事件节点 | `target_type` |
| `review_candidate_missing_in_target` | 评审候选在目标版本中缺失（候选 / 版本不一致） | `witness_path: [node]` |
| `review_candidate_not_event_in_target` | 评审候选在目标版本中不再是事件节点 | `target_type` |
| `schulze_top_rank_mismatch` | 冻结选票在「目标版本仍在的候选」上重算后，签发集合不再恰好覆盖最高并列名次 | 缺项 / 多项 / 最高并列 id 列表 |
| `candidate_ancestor_relation` | 两个根因在目标版本上出现 causes 祖先关系 | `witness_path`（按 id 升序 DFS） |
| `evidence_not_admissible` | 某根因证据状态变为 insufficient 或 refuted | S/R 分值，`witness_path: [node]` |
| `time_window_conflict` | 目标版本整图有效时间窗传播冲突 | 冲突链 `witness_path` + `witness_edge_ids` |

- Schulze 在目标版本中**仍存在且仍为事件**的候选上由冻结选票重算；被删除 / 改类别的候选
  不再参与计票（两两偏好只看相对先后，剔除候选不影响其余候选间的比较），并由
  `review_candidate_*` 失效项单独报告。
- `changes` 展示**相对签发版**的差异：整图 `nodes_added/removed`、`edges_added/removed`；
  每个根因的 `root_nodes`（节点字段）、`root_evidence`（证据状态迁移、S/R、入选 /
  同源未计权证据增减）、`root_time_windows`（上下界 / 收紧来源 / 传播链逐项）。
- 结果为**只追加**记录：`root_cause_issuance_rechecks` 上的触发器禁止 UPDATE/DELETE；
  `UNIQUE(issuance_id, target_version_id)` 保证同一签发对同一目标版本至多一条 ——
  重复复核（无论上次是 valid 还是 invalid）返回 **409**，绝不覆盖：

```json
{ "detail": "签发记录 1 对目标版本 v3 已存在复核记录（#2，invalid），重复复核不得覆盖旧记录",
  "code": "recheck_already_exists" }
```

签发记录不存在 / 目标版本不存在返回 404（`issuance_not_found` / `target_version_not_found`）。
列表与详情：

```bash
curl http://localhost:8000/api/issuances/1/rechecks          # 全部复核（摘要，倒序）
curl http://localhost:8000/api/issuances/1/rechecks/2        # 单条（含完整 report）
```

摘要项含 `status`、`valid`、`violation_count`、`root_cause_node_ids`、`checked_at` 与
SHA-256；`digest` 可按与签发相同的规范化口径复算
（`jq -cjS .report file | sha256sum`，对 GET 详情中的 `report` 字段）。
复核全程只追加新表行，**不改写**因果图、评审、签发记录与任何历史快照。

### 变更提案合并示例

**保存提案**（不生成版本；`changes` 与普通提交的 changeset 完全同构，必须带
`base_version_id`，保存时在该基线上复用整图校验）：

```bash
# 提案 A：移动「订单损失」节点，并双方都会建一个同名新事件
curl -X POST http://localhost:8000/api/proposals \
  -H 'Content-Type: application/json' -d '{
    "title": "A 组：调整损失节点位置",
    "changes": {
      "base_version_id": 1,
      "node_updates": [{"id": "n-loss", "pos_x": 1000, "pos_y": 320}],
      "node_creates": [{"id": "ev-triage", "type": "event",
                        "title": "应急响应介入", "pos_x": 400, "pos_y": 620}]
    }
  }'
# -> {"id": 1, "title": "...", "base_version_id": 1, "status": "open", "change_count": 2}

# 提案 B：同一节点移到另一处、给「空指针异常」改名，也建同一个 ev-triage
curl -X POST http://localhost:8000/api/proposals \
  -H 'Content-Type: application/json' -d '{
    "title": "B 组：损失节点移位与 NPE 改名",
    "changes": {
      "base_version_id": 1,
      "node_updates": [
        {"id": "n-loss", "pos_x": 900, "pos_y": 500},
        {"id": "n-null", "title": "空指针异常 NPE"}
      ],
      "node_creates": [{"id": "ev-triage", "type": "event",
                        "title": "应急响应介入", "pos_x": 400, "pos_y": 620}]
    }
  }'
# -> {"id": 2, ..., "status": "open"}
```

保存后版本仍停留在 v1（`GET /api/proposals` 可列提案，`GET /api/proposals/1/preview`
可只读预览应用后的图）。**三方比较**（只读，不写库）：

```bash
curl -X POST http://localhost:8000/api/proposals/merge-preview \
  -H 'Content-Type: application/json' \
  -d '{"proposal_a_id": 1, "proposal_b_id": 2}'
```

响应（节选）：单侧修改与双方相同修改进入 `auto_resolved`，异值修改进入 `conflicts`；
同时返回合并预览图 `merged_graph_full`，以及预览图上的 `cycle` / `time_conflict`
（未裁决项在预览图中暂取基线值）：

```json
{
  "base_version_id": 1,
  "current_version_id": 1,
  "base_is_current": true,
  "auto_resolved": [
    {"subject": "node", "id": "ev-triage", "action": "create", "side": "both_same", "title": "应急响应介入"},
    {"subject": "node", "id": "n-null", "action": "field_change", "side": "b", "field": "title"}
  ],
  "conflicts": [
    {"key": "node:field:n-loss:pos_x", "subject": "node", "id": "n-loss",
     "type": "value_diverge", "field": "pos_x", "field_label": "横坐标",
     "base_value": 960.0, "a_value": 1000.0, "b_value": 900.0, "options": ["a", "b", "base"]},
    {"key": "node:field:n-loss:pos_y", "subject": "node", "id": "n-loss",
     "type": "value_diverge", "field": "pos_y", "field_label": "纵坐标",
     "base_value": 300.0, "a_value": 320.0, "b_value": 500.0, "options": ["a", "b", "base"]}
  ],
  "cycle": null,
  "time_conflict": null,
  "merged_graph_full": { "version_id": 1, "nodes": [ ... ], "edges": [ ... ] }
}
```

冲突类型与可选项：

| `type` | 含义 | `options` |
| --- | --- | --- |
| `value_diverge` | 同一字段（节点 / 连线）被双方改成不同值 | `a` / `b` / `base` |
| `delete_modify` | 一侧删除、另一侧修改同一节点 / 连线 | `a` / `b` / `base` |
| `create_create` | 双方用同一 id 新建但内容不同 | `a` / `b` / `drop`（双方都不采纳） |

`delete_modify` 项额外给出 `deleted_side` / `kept_side`；连线冲突还带 `source_id` /
`target_id` 便于前端定位。**裁决并生成合并版本**（每个冲突 key 必须恰好出现一次）：

```bash
curl -X POST http://localhost:8000/api/proposals/merge \
  -H 'Content-Type: application/json' -d '{
    "proposal_a_id": 1,
    "proposal_b_id": 2,
    "message": "合并 A/B 两组补充：横坐标取 A、纵坐标取 B",
    "resolutions": [
      {"key": "node:field:n-loss:pos_x", "choice": "a"},
      {"key": "node:field:n-loss:pos_y", "choice": "b"}
    ]
  }'
# -> {"version_id": 2, "kind 由服务端置为 merge,
#     "merged_proposal_ids": [1, 2], ...}
```

落库在**一个写事务**中完成：重新读取两份提案确认仍为 `open`、确认当前版本等于基线，
重新做三方比较并校验裁决（覆盖全部冲突、选项合法、无悬空连线），把结果合成为 changeset
再次跑成环 / 时间窗等整图校验，通过后才插入 `merge` 版本快照并把提案 1、2 同时置为
`merged`。任一失败返回错误且**不留下版本或已合并状态**：

| 场景 | 状态码 | `code` |
| --- | --- | --- |
| 尚有冲突未裁决 | 400 | `unresolved_conflict` |
| 选项对该冲突非法（如字段冲突选 `drop`） | 400 | `invalid_choice` |
| 裁决 key 与最新冲突集不符（提案 / 基线已变） | 409 | `stale_conflict` |
| 提案已被其他合并使用 | 409 | `proposal_already_merged` |
| 当前版本 ≠ 提案基线（基线过期） | 400 | `stale_version` |
| 合并结果成环 | 400 | `cycle`（响应附 `cycle` 节点环） |
| 合并后时间窗传播冲突 | 400 | `time_conflict`（响应附 `conflict` 因果链） |
| 两份提案基线不同 | 400 | `different_base` |
| 裁决结果相对基线无任何修改 | 400 | `empty_merge` |

合并成功后版本列表中该版本显示「合并」标签与来源提案编号
（`GET /api/versions` 的每项附 `merged_proposal_ids`）；已合并提案在 `GET /api/proposals`
中为 `"status": "merged"`、`merged_version_id` 指向新版本，不能再参与合并。前端在
「变更提案合并」面板中即可完成保存、勾选比较、只读预览、逐项裁决与冲突定位，
原 changeset 提交（`POST /api/changes/commit`）与历史版本浏览 / 回滚不受影响。

### 重复事件归并示例

在**当前最新版本**上选择保留事件 `n-null` 与重复事件 `n-null2`（同一事故被两个工单各录一次），
先预览（只读，不写库、不生成版本）：

```bash
curl -X POST http://localhost:8000/api/node-merge/preview \
  -H 'Content-Type: application/json' \
  -d '{"keep_id": "n-null", "duplicate_ids": ["n-null2"]}'
```

响应（节选）给出**连线迁移清单**、**字段冲突**、**确定性拒绝路径** `blockers` 与归并预览全图：

```json
{
  "base_version_id": 3, "current_version_id": 3, "base_is_current": true,
  "keep_id": "n-null", "keep_title": "空指针异常",
  "duplicates": [{"node_id": "n-null2", "title": "空指针异常(重复)",
                  "earliest_time": "2026-09-20T02:00:20Z", "trigger_mode": "ANY"}],
  "field_conflicts": [
    {"field": "title", "field_label": "标题", "resolved": false,
     "options": [
       {"value": "空指针异常", "source_node_id": "n-null", "equivalent_node_ids": ["n-null"]},
       {"value": "空指针异常(重复)", "source_node_id": "n-null2",
        "equivalent_node_ids": ["n-null2"]}]},
    {"field": "trigger_mode", "field_label": "触发模式", "resolved": false,
     "options": [
       {"value": "ALL", "source_node_id": "n-null", "equivalent_node_ids": ["n-null"]},
       {"value": "ANY", "source_node_id": "n-null2", "equivalent_node_ids": ["n-null2"]}]}
  ],
  "edge_moves": {
    "rewired": [
      {"edge_id": "ex4", "kind": "causes", "label": "慢查询放大空指针",
       "old_source_id": "n-db", "old_target_id": "n-null2",
       "new_source_id": "n-db", "new_target_id": "n-null"}],
    "collapsed": [
      {"kind": "causes", "source_id": "n-code", "target_id": "n-null",
       "surviving_edge_id": "e1", "removed_edge_ids": ["ex1"],
       "member_edge_ids": ["e1", "ex1"], "label": "引入空配置路径"},
      {"kind": "supports", "source_id": "e-log", "target_id": "n-null",
       "surviving_edge_id": "e5", "removed_edge_ids": ["ex3"],
       "member_edge_ids": ["e5", "ex3"], "label": "日志另录 / 时间线吻合"}]
  },
  "blockers": [],
  "merged_graph_full": { "version_id": 3, "nodes": [ … ], "edges": [ … ] }
}
```

- `edge_moves.rewired`：端点实际改变的单边迁移（旧端点 / 新端点均给出）；
- `edge_moves.collapsed`：迁移后同端点同类型的多条连线，保留 id 最小的一条、删除其余，
  非空标签去重并按字典序以 ` / ` 合并。

`blockers` 为空且每个字段冲突都明确取值后，确认归并（每个冲突字段恰好给一个来源候选 id）：

```bash
curl -X POST http://localhost:8000/api/node-merge/commit \
  -H 'Content-Type: application/json' -d '{
    "keep_id": "n-null",
    "duplicate_ids": ["n-null2"],
    "message": "归并重复空指针事件",
    "field_resolutions": {
      "title": "n-null", "description": "n-null2",
      "earliest_time": "n-null2", "latest_time": "n-null",
      "trigger_mode": "n-null"
    }
  }'
```

成功返回新的 `merge` 版本与**旧节点 → 保留节点映射**：

```json
{
  "version_id": 4, "kind": "merge", "base_version_id": 3,
  "keep_id": "n-null",
  "node_map": { "n-null2": "n-null" },
  "removed_node_ids": ["n-null2"],
  "removed_edge_ids": ["ex1", "ex2", "ex3"],
  "rewired_edge_ids": ["ex4"],
  "merged_edge_groups": [
    {"surviving_edge_id": "e1", "removed_edge_ids": ["ex1"]},
    {"surviving_edge_id": "e3", "removed_edge_ids": ["ex2"]},
    {"surviving_edge_id": "e5", "removed_edge_ids": ["ex3"]}
  ]
}
```

任一确定性拒绝条件命中时，预览仍返回 200（便于前端逐项展示与定位），`blockers` 给出
**确定性冲突路径**；确认落库则整体拒绝（HTTP 400，`code=node_merge_rejected`）并回滚，
不留下版本：

| `blockers[].code` | 触发条件 | 确定性见证 |
| --- | --- | --- |
| `candidate_ancestor_relation` | 两个候选在 causes 子图上互为祖先 | `path` 节点链 + `edge_ids`（按 id 升序 BFS 的最短路径） |
| `self_loop` | 某条连线迁移后两端都指向保留节点 | `edge_id` / `endpoints` |
| `cycle` | 迁移后的图成环 | `cycle` 环节点 + `edge_ids` |
| `time_window_disjoint` | 候选自报时间窗无交集（下界 max > 上界 min） | `lower/upper_node_id` 与两个界限值 |
| `all_prerequisite_lost` | 任一 ALL 触发节点的不同前因映射后数量收缩 | `parent_ids_before/after`、`collapsed_parent_groups`、见证 `edge_ids` |
| `time_conflict` | 归并后整图有效时间窗传播冲突 | 复用普通提交的 `conflict`（来源节点 + 因果链） |

其余错误：保留 / 重复节点不存在为 404（`keep_not_found` / `duplicate_not_found`）；
候选含证据节点 `keep_not_event` / `duplicate_not_event`；保留事件出现在重复列表
`keep_in_duplicates`；字段取值来源不在候选中 `invalid_field_resolution`；存在未裁决字段冲突
`unresolved_field_conflict`；`base_version_id` 不等于当前最新版本 `stale_version`（400）。
归并版本与提案合并版本同为 `kind='merge'`（`GET /api/versions` 的 `merged_proposal_ids`
为空表示它不来自提案合并）；**绑定旧版本的评审、提案与签发记录不受影响**。前端
「重复事件归并」面板支持候选勾选、只读预览图、字段逐项取值、连线清单与冲突路径一键定位。

## 🏗️ 架构与目录

```
.
├── app/
│   ├── main.py       # FastAPI 路由：提交事务、回滚、下游查询、反事实推演、结论包导出/核验、盲评建场/投票/封榜、根因签发预检/签发/下载、签发有效性复核、提案合并、重复事件归并、静态托管
│   ├── graph.py      # 图加载、changeset 校验（成环检测/证据字段）、时间窗传播、证据强度审查、BFS 下游、反事实拓扑推演、快照持久化
│   ├── node_merge.py # 重复事件归并：边迁移与同端点连线标签合并、字段冲突、确定性拒绝路径（祖先/自环/环/时间窗无交集/ALL 前因丢失/传播冲突）、写事务生成 merge 版本
│   ├── conclusion.py # 可复算复盘结论包：自包含 payload 收集、规范化 SHA-256 摘要、上传包重建 DAG 重算并逐字段比对（纯只读）
│   ├── review.py     # 根因共识盲评：评审场 CRUD（写事务校验 revision/最低票数）、Schulze 计票（两两偏好/最强路径/逐层并列/确定性见证）
│   ├── issuance.py   # 根因结论签发：写事务内重读绑定版本/Schulze/证据审查/时间窗逐项校验、规范化 payload+SHA-256、只追加签发记录与只读预检
│   ├── issuance_recheck.py # 签发有效性复核：写事务内加载签发绑定版本/payload/目标整图，按原规则重算 Schulze/祖先/证据/时间窗，逐项失效原因+见证+相对签发版变化，只追加复核记录（不覆盖）
│   ├── proposals.py  # 变更提案：保存（基线整图校验、不出版本）、三方比较（自动采纳/冲突）、写事务裁决合并（复查状态与基线+重跑整图校验+生成 merge 版本）
│   ├── database.py   # SQLite 连接、表结构、v1 示例数据、旧库幂等迁移（含 versions.kind 扩展到 merge）
│   └── schemas.py    # Pydantic 请求/响应模型
├── static/
│   ├── index.html    # 三栏布局：顶栏 / 画布 / 侧栏 + 评审室弹窗
│   ├── style.css
│   └── app.js        # 状态管理、SVG 渲染、拖拽/连线、changeset 累计与提交、结论包导出下载与核验展示、盲评建场/投票/封榜与矩阵视图、根因签发预检/签发/记录展示、变更提案保存/预览/三方比较裁决与冲突定位、重复事件归并候选选择/预览/字段取值/冲突路径定位与确认
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 数据模型（版本即快照）

- `versions(id, message, kind[commit|rollback|merge], base_id, created_at)`
- `nodes(id, version_id, type, title, description, pos_x, pos_y, earliest_time, latest_time,
  reliability, source_group, trigger_mode, created_at)`，主键 `(id, version_id)`；
  `earliest_time` / `latest_time` 为 UTC（`...Z`），仅事件节点非空；
  `reliability`（0~1）/ `source_group` 仅证据节点非空；
  `trigger_mode ∈ {ANY, ALL}` 仅事件节点非空（证据节点恒为 NULL）
- `edges(id, version_id, source_id, target_id, kind, label, created_at)`，主键 `(id, version_id)`；
  `kind ∈ {causes, supports, refutes}`
- 变更提案（保存不生成版本；合并成功后才与 merge 版本同时落状态）：
  - `proposals(id, title, base_version_id→versions, changes_json, status[open|merged],
    merged_version_id→versions, created_at, merged_at)`
  - `changes_json` 为与 `/api/changes/commit` 同构的 changeset（`json_valid` 约束），
    更新项额外保存 `_fields_set` 以区分「未提供」与「显式置空（清除字段）」；
    `open` 提案可反复预览，`merged` 提案不可再用，`merged_version_id` 指向合并版本
- 根因共识盲评（只读写以下表，不触碰上面的图数据）：
  - `review_sessions(id, title, version_id→versions, min_votes, status[open|closed],
    revision, created_at, closed_at)` —— `revision` 即 `session_revision`，每次投票 / 封榜自增
  - `review_candidates(session_id, node_id)`、`review_reviewers(session_id, reviewer)`
  - `review_ballots(session_id, reviewer, ranking, submitted_at)`，主键 `(session_id, reviewer)`；
    `ranking` 为 JSON 数组（全部候选的严格排序），重复提交即覆盖
- 根因结论签发（只读写 `root_cause_issuances` 一张新表，不触碰图数据与评审票）：
  - `root_cause_issuances(id, session_id→review_sessions UNIQUE, version_id→versions, note,
    payload_json, package_json, digest_algorithm, digest, issued_at)`
  - `payload_json` 为规范化结论 payload（紧凑、键按字典序），`package_json` 为含 digest 的完整签发文件；
    `digest` = payload 规范化 JSON 的 SHA-256；`session_id` 唯一保证**同场不可重复签发**
  - **只追加**：表上 `BEFORE UPDATE` / `BEFORE DELETE` 触发器直接 `RAISE(ABORT)`，
    记录写入后不可修改、不可删除；记录绑定的是封榜时的版本 id 且自带 payload，
    后续图版本变化不影响记录
- 签发有效性复核（只读写 `root_cause_issuance_rechecks` 一张新表，不触碰图数据 / 评审 / 签发）：
  - `root_cause_issuance_rechecks(id, issuance_id→root_cause_issuances,
    target_version_id→versions, status[valid|invalid], note, report_json,
    digest_algorithm, digest, checked_at)`，`UNIQUE(issuance_id, target_version_id)`
  - `report_json` 为规范化复核报告（紧凑、键按字典序）：逐项 `checks` / `violations`
    （稳定顺序 + 见证路径）、候选明细与相对签发版的节点 / 证据 / 时间窗变化；
    `digest` = report 规范化 JSON 的 SHA-256
  - **只追加**：表上 `BEFORE UPDATE` / `BEFORE DELETE` 触发器直接 `RAISE(ABORT)`；
    唯一约束兜底：同一签发与目标版本重复复核返回 409，旧记录（valid 或 invalid）
    **绝不覆盖**

最新版本 = `MAX(versions.id)`；读取图时只查该版本的行。回滚不修改任何旧行，而是把目标版本的
nodes/edges 复制到一条新的 rollback 版本中，因此历史始终可追溯。提案合并同理：三方裁决结果
合成为新的 changeset，经整图校验后复制全量 nodes/edges 到一条新的 **merge** 版本，并在同一事务
把两份提案置为 `merged`。旧版数据库启动时会幂等 `ALTER TABLE` 补齐时间列、证据强度列与
`trigger_mode` 列（历史事件行回填默认 `ANY`），并重建 edges / versions 表把 CHECK 约束分别
扩展到 `refutes` 与 `merge`（表重建按 SQLite 官方 12 步流程在 `foreign_keys=OFF` 下进行，
历史行原样复制，迁移后跑 `foreign_key_check` 兜底）。

### 事务校验流程（`graph.apply_changes`）

1. 批次内操作 id 去重检查
2. 删节点（级联删除关联边）→ 删边 → 更新节点 → 建节点 → 更新边 → 建边
3. 建边时校验：端点存在、非自环、端点类型符合 `causes/supports/refutes` 语义、无同类型平行边；
   更新边时仅允许 `supports ↔ refutes` 立场切换（`causes` 不可改类型）
4. 在应用后**的整张图**上跑 DFS 三色标记，发现回边即返回环路径并抛错
5. 在「事件 + causes」子图上做拓扑传播：祖先下界取 max、后代上界取 min；发现某节点
   `effective_lower > effective_upper` 即构造两端来源节点 + 因果链并抛 `time_conflict`
6. 全部通过后才在同一事务内插入新版本与快照行；任一步失败 → `ROLLBACK`

### 证据强度计算（`graph.compute_evidence_review`）

1. 收集每个事件的 `supports` / `refutes` 入边，按 `source_group` 分桶；未分组证据各自独立成桶
2. 每桶每方向取 `reliability` 最高者入选（并列取较小节点 id，保证确定），其余记为同源未计权
3. 各入选桶按 `S = 1 − ∏(1−r)`、`R = 1 − ∏(1−r)` 聚合（保留 4 位小数）
4. 依次判定：`S≥0.6 且 R≥0.6` → `disputed`；`S≥0.7` → `supported`；`R≥0.7` → `refuted`；否则 `insufficient`
5. 该函数为纯函数：查询当前版或历史快照时按该版本的 nodes/edges 现算，不写库、不改旧版

### 反事实推演（`graph.simulate_counterfactual`）

1. 只取「事件 + `causes`」子图，前因列表按节点 id 升序；Kahn 拓扑排序（初始队列按 id 排序）保证结果可复现
2. 用户勾选的干预点强制 `prevented`（`intervention`）；其余事件：无前因 → `still_active`
   （`no_causes`，即默认发生）；ANY 门看是否存在仍发生前因；ALL 门看是否存在被阻止前因
3. 依据树递归到前因：ANY 发生取 id 最小的仍发生前因、阻止时列全部；
   ALL 发生列全部、阻止时取 id 最小的已阻止前因；干预点与无前因事件不展开
4. 纯只读：不写库、不产生 changeset / 版本；前端对未提交工作图用同构 JS 引擎即时推演

### 可复算复盘结论包（`conclusion.build_package` / `conclusion.verify_package`）

1. **收集范围**：从根事件沿 `causes` 边反向遍历得到全部祖先事件；包内连线 = 祖先锥内的
   `causes` 边 + 指向这些事件的 `supports` / `refutes` 边（证据节点随边纳入）
2. **自包含结果**：有效时间窗与证据审查在**包内子图**上重算（与 `/api/state` 同一套
   `compute_time_windows` / `compute_evidence_review`），节点 / 连线先按 id 排序再计算，
   保证导出端与核验端遍历顺序一致、结果可逐字节复现
3. **摘要**：`digest.value = SHA-256(紧凑 UTF-8 JSON(payload))` —— 对象键按字典序、无空格分隔符，
   整数值浮点规范化为整数（`60.0` → `60`，与 jq / JavaScript 的序列化一致）；
   导出文件本身也是键按字典序、数字规范化后的 JSON
4. **核验**：重新规范化 `payload` 并比对 digest → 仅用包内 `nodes` / `edges` 重建内存 DAG
   （不接触数据库）→ 重算结果与 `payload.results` 递归逐字段比对，差异以 JSON Pointer 报告
   （`~0` / `~1` 转义，缺失侧为 `null`，数字 `1` 与 `1.0` 视为相等，最多列 200 条）
5. 两个方向都纯只读：不写库、不生成版本、不改变工作图

### 根因共识盲评（`review.create_session` / `submit_ballot` / `close_session` / `schulze_result`）

1. **建场**：校验版本存在、候选为 3~20 个不重复的该版本事件节点、评审人去空白后唯一、
   `1 ≤ min_votes ≤ 评审人数`；一个 `BEGIN IMMEDIATE` 事务写入 3 张表，`revision` 从 1 开始
2. **投票**：写事务内校验评审场开放、评审人已登记、排序恰为全部候选的一个排列；
   `INSERT ... ON CONFLICT DO UPDATE` 覆盖本人选票，`revision` 自增；封榜后一律 409
3. **封榜**：写事务内依次校验 `status = open`（否则 409 `already_closed`）、
   `expected_revision = revision`（否则 409 `stale_revision`）、票数 ≥ `min_votes`
   （否则 409 `min_votes_not_met`），然后置 `closed` 并自增 `revision` —— 此后投票接口拒绝，
   选票即冻结
4. **计票**（纯函数，读取时按冻结选票重算）：`d[i][j]` 为两两偏好计数；最强路径
   `p[i][j]` 只保留 `d[i][j] > d[j][i]` 的有向边，用 Floyd-Warshall 变体取「最弱一环」的
   最大值；每轮取出不被其余候选击败的集合作为同一并列层（Schulze 胜出关系是严格偏序，
   该集合必然非空）；候选 ID 仅用于矩阵 / 层内稳定排序与见证路径的确定性选择，不打断并列
5. **见证**：对任意 `p[i][j] > 0` 的方向，在「`d[u][v] > d[v][u]` 且 `d[u][v] ≥ p[i][j]`」
   的子图上按候选 ID 升序 DFS 取第一条路径 —— 其最弱边恰为 `p[i][j]`，确定性可复现

### 根因结论签发（`issuance.evaluate` / `issuance.issue_issuance`）

1. **同一写事务重读**（`BEGIN IMMEDIATE`）：评审场行（状态 / revision / 绑定版本）、
   该版本快照 nodes/edges、冻结选票、已存在签发记录；Schulze 名次由冻结选票现算，
   证据审查（`compute_evidence_review`）与有效时间窗（`compute_time_windows`）
   在绑定版本上重算 —— 不信任任何客户端随请求提供的结果
2. **逐项校验**：已封榜 → 同场未签发 → 集合非空 / 不重复 / 均为候选 →
   提交集合与最高并列名次**集合相等**（缺项 `missing_top_rank`、多项 `extra_not_top_rank`）→
   候选两两在「事件 + causes」子图上不可达（祖先关系按 id 升序 DFS 取确定性见证路径）→
   每个候选证据状态 ∈ {supported, disputed}（insufficient / refuted 拒绝）→
   绑定版本时间窗无冲突；每个失败条件都生成一条带 code 与中文原因的 violation
3. **写入只追加记录**：全部通过后组装规范化 payload（meta / 最终根因 / 全部候选状态 /
   Schulze 结果与公开选票），用与结论包相同的规范化（键字典序、紧凑 JSON、整数值浮点）
   计算 SHA-256，连同 payload 与完整 package 插入 `root_cause_issuances`；
   `session_id` 唯一约束在并发竞争时兜底（捕获 IntegrityError → 409）
4. **全成或全不成**：任一校验失败 → `ROLLBACK`，不留下签发记录；记录上的触发器永久禁止
   UPDATE/DELETE。整个流程只新增签发表行，**不改写** versions/nodes/edges、review_* 选票
5. **只读预检** `POST …/issuance/validate` 复用同一个 `evaluate`（不 BEGIN、不写库），
   返回 checks / violations / 候选明细，供前端即时展示

### 签发有效性复核（`issuance_recheck.create_recheck` / `evaluate_recheck`）

1. **同一写事务加载**（`BEGIN IMMEDIATE`）：不可变签发记录行（绑定版本 id / 规范化
   payload / 摘要）、评审场行与冻结选票、签发时绑定版本整图、目标版本整图；
   先校验目标版本存在（否则 404），再确认签发记录 / payload / 评审场三者绑定版本一致
2. **按原规则重算**：
   - Schulze 最高并列在「目标版本中仍存在且仍为事件」的评审候选上由**冻结选票**现算
     （剔除缺失候选不改变存活候选间的两两偏好）；签发集合（取自签发 payload 的
     root_causes）须仍与最高并列**集合相等**
   - 根因逐一检查在目标版本中存在且为事件；根因两两在目标版本「事件 + causes」子图上
     不可达（祖先关系复用签发的按 id 升序邻接与确定性见证路径）
   - 每个现存根因在目标版本上的证据状态 ∈ {supported, disputed}；整图时间窗
     `compute_time_windows` 传播无冲突（附冲突因果链节点 / 边）
   - 评审候选在目标版本缺失 / 改类别单列 `review_candidate_*` 失效项
3. **逐项失效原因（稳定顺序）**：violations 先按固定代码顺序
   （绑定版本 → 根因缺失/改类别 → 评审候选缺失/改类别 → Schulze 不覆盖 → 祖先关系 →
   证据 → 时间窗），同代码再按 node_id / (ancestor, descendant) 排序；每条带中文原因
   与见证路径，供前端逐项定位
4. **相对签发版变化**：在签发绑定版本与目标版本上分别重算时间窗 / 证据审查，给出整图
   节点 / 连线增删，以及每个根因的节点字段差异、证据差异（状态迁移、S/R、入选与同源
   未计权证据按 edge_id 集合比较）、有效时间窗差异（上下界 / 来源 / 传播链）
5. **写入只追加记录**：报告规范化（键字典序、紧凑 JSON、整数值浮点）后计算 SHA-256，
   插入 `root_cause_issuance_rechecks`，valid / invalid 都落库；UNIQUE 约束在并发竞争时
   兜底（捕获 IntegrityError → 409）。任一重复复核返回 409 并 `ROLLBACK`，
   **旧记录绝不覆盖**；触发器永久禁止 UPDATE/DELETE
6. 整个流程只新增复核表行，**不改写** versions/nodes/edges、review_* 与
   root_cause_issuances；原签发、原图、评审与 Docker Compose / 8000 启动方式均不变

### 变更提案保存与三方合并（`proposals.save_proposal` / `compute_merge` / `commit_merge`）

1. **保存**：写事务内把提案 changeset 在**其绑定基线**（不是当前版本）上重放，复用
   `graph.apply_changes` 整图校验，通过后只向 `proposals` 插入一行，**不写 versions / nodes / edges**；
   更新项随 changeset 保存 `_fields_set`，重载时还原「未提供 vs 显式置空」
2. **三方比较**（纯函数，预览不落库）：分别把两份提案在基线上重放为图 A / 图 B，按节点 id /
   连线 id 取基线、A、B 三方并集逐项对比：
   - 仅一方相对基线不同 → 采纳该方；双方改动相同 → 采纳一次；双方都删 → 删除
   - 双方改成不同值 → 按字段（节点 10 个字段、连线 `kind`/`label` 等）各生成一个 `value_diverge` 冲突
   - 一方删除、另一方修改 → `delete_modify` 实体冲突（节点被删时其关联边的去留跟随该节点裁决）
   - 双方同 id 新建内容不同 → `create_create`，可 `drop`；被丢弃节点的关联边不进合并图
   - 预览同时在合并图上跑 `find_cycle` 与 `compute_time_windows`，提示裁决后仍可能被整图校验拒绝
3. **裁决落库**（单 `BEGIN IMMEDIATE` 事务，全成或全不成）：
   - 事务内重读两份提案，复查均 `status='open'` 且基线一致；`MAX(versions.id)` 必须等于
     `base_version_id`（否则 `stale_version`）
   - **重新**重放提案并三方比较（不信任预览），核对裁决 key 全集与当前冲突集一致、
     每项选择在 `options` 内、不存在端点悬空的连线
   - 把合并结果相对基线重新表达为标准 `Changeset`（`build_merged_changeset`），再次调用
     `graph.apply_changes`：成环 / 时间窗 / 字段与连线约束任何一项失败即回滚
   - 通过后插入 `kind='merge'` 版本与全量快照，并在同一事务
     `UPDATE proposals SET status='merged', merged_version_id=? WHERE id IN (?, ?)`
4. 整个流程中原有 `/api/changes/commit` 提交路径与 versions 历史完全不变；merge 只是多一种
   版本来源，回滚、结论包、盲评等按版本快照工作的功能对 merge 版本同样适用

### 重复事件归并（`node_merge.build_merge_plan` / `commit_merge`）

1. **选择校验**：保留事件与重复事件均须存在且为事件节点、互不重叠、重复列表无重复项；
   预览 / 落库都只针对**当前最新版本**（`base_version_id` 乐观锁），不应用任何未提交 changeset
2. **字段冲突**：标题、描述、`earliest_time`、`latest_time`、`trigger_mode` 按候选 id 升序
   收集取值并分组，存在不同取值即生成一个冲突项（等值候选合并为同一选项）；位置 / id /
   `created_at` 不在冲突范围，保留节点沿用自身值。预览未裁决时暂取保留节点值占位，落库要求
   每个冲突字段都显式给出来源候选 id
3. **边迁移与合并**：所有边的端点经「重复 id → 保留 id」映射，按 `(新起点, 新终点, kind)`
   分组：单组一条且端点改变记 `rewired`；同组多条保留 id 最小者、删除其余，非空标签去重后
   按字典序以 ` / ` 合并记 `collapsed`；映射后两端相同记为自环 blocker（不进结果图）
4. **确定性拒绝路径**：候选间祖先关系用按 id 升序邻接表上的 BFS 最短路径见证；环检测复用
   `find_cycle`；时间窗无交集取候选最早时间的 max 与最晚时间的 min 比较；ALL 前因丢失比较
   映射前后各 ALL 节点的**不同前因集合大小**（候选锥内节点的前因先并集再映射）；最后在
   归并图上复用 `compute_time_windows` 整图传播检查
5. **写事务落库**（单 `BEGIN IMMEDIATE`，全成或全不成）：事务内重读当前版本并重算整份计划
   （不信任预览），blockers 非空或字段冲突未全部裁决即抛错回滚；通过后插入
   `kind='merge'` 版本与全量快照，返回 `node_map` 与删除 / 重接 / 合并清单。既有版本以及
   绑定旧版本的评审、提案、签发记录一律不改写

## 📌 说明

- 节点 / 连线 id 由客户端生成（`[A-Za-z0-9_-]+`），便于一次批次中同时「创建并连边」。
- 并发安全：提交使用 `BEGIN IMMEDIATE` 写锁 + 基准版本乐观校验，后提交者会收到 `stale_version` 错误。
