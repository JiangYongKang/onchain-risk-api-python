# On-chain Transaction Risk Analysis API（本地版）

一个**本地可直接运行、无外部依赖**的链上交易风险分析 HTTP API。支持交易批次导入与幂等处理、
地址关系图谱、规则版本发布/回滚与历史时点回放，并保证并发下结论来自一个完整版本、
拒绝原因稳定可区分、超限场景可控且显式标记。

- 数据只存内存，不连接链节点、不需要任何真实凭据。
- 技术栈：Python 3.14 + FastAPI + pytest；并发用单存储锁 + 不可变快照（MVCC 思想）。

---

## 1. 快速开始

```bash
uv sync
uv run python main.py            # 启动在 http://127.0.0.1:8000
# 或：uv run uvicorn main:app --port 8000
```

启动后：

- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /health`（返回当前 data_version / rule_set_version / now_ms）

运行测试：

```bash
uv run pytest -q
```

---

## 2. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/batches` | 提交交易批次（允许部分成功、幂等） |
| `GET`  | `/api/v1/batches/{batch_id}` | 查询批次导入状态与逐条结果 |
| `POST` | `/api/v1/relations` | 导入地址关系边（有向，无重复边） |
| `POST` | `/api/v1/rules` | 发布/更新一条规则，生成新规则集版本 |
| `POST` | `/api/v1/rules/{rule_id}/rollback` | 把规则回滚到指定历史版本（生成新版本，不覆盖历史） |
| `GET`  | `/api/v1/rulesets?version=N` | 查看规则集（默认当前） |
| `GET`  | `/api/v1/risk/{tx_hash}` | 风险分、风险标签与逐条命中依据 |

所有字段以 `0x` 开头的小写 hex 表示；传入大写会被规范化为小写。
金额为有限正数；时间为 epoch 毫秒整数。

### 2.1 批次导入

```json
POST /api/v1/batches
{
  "batch_id": "b-2026-001",
  "transactions": [
    {"tx_hash": "0xaa01", "from_addr": "0xa1..", "to_addr": "0xb1..",
     "amount": 250, "ts_ms": 1759000000000}
  ]
}
```

响应逐条给出结果，批次**允许部分成功**：

```json
{
  "batch_id": "b-2026-001", "replayed": false,
  "status": "PARTIAL",
  "data_version": 7, "rule_set_version": 2,
  "accepted": 1, "duplicate": 1, "rejected": 1,
  "items": [
    {"index": 0, "tx_hash": "0xaa01", "status": "ACCEPTED"},
    {"index": 1, "tx_hash": "0xaa02", "status": "DUPLICATE",
     "error": "TX_HASH_DUPLICATE", "message": "already imported in batch ..."},
    {"index": 2, "tx_hash": null, "status": "REJECTED",
     "error": "AMOUNT_NOT_POSITIVE", "message": "amount must be > 0"}
  ]
}
```

- `status`：`ACCEPTED`（全部新计入）/ `PARTIAL`（有新计入，但也有重复或拒绝）/ `REJECTED`（没有任何新计入）。
- 每条交易只计一次：批次内重复标 `ITEM_DUPLICATE_IN_BATCH`；与已入库交易重叠标
  `TX_HASH_DUPLICATE`（带原批次 id），均不计第二次。
- 同一 `tx_hash` 字段自相矛盾：批次内 `ITEM_CONFLICT_IN_BATCH`；与已存数据矛盾
  `TX_HASH_CONFLICT`，一律拒绝，不静默跳过。
- 同一 `batch_id` 重复提交：
  - **负载完全一致** → 返回首次结果，`replayed=true`，数据版本不变（幂等）；
  - **负载不同** → `409 BATCH_REPLAY_MISMATCH`。

### 2.2 规则与版本

一条规则：

```json
{
  "rule_id": "big-tx",
  "kind": "TX_AMOUNT",
  "name": "large transfer",
  "description": "",
  "score": 40,                 // 该规则命中得分 0-100
  "tag": "LARGE_TX",
  "params": {"min_amount": 100},
  "effective_at": 1759000000000   // 可选，缺省=发布时刻
}
```

规则类型（`kind`）：

| kind | 参数 | 命中条件 |
| --- | --- | --- |
| `TX_AMOUNT` | `min_amount` | 交易金额 ≥ 阈值 |
| `TIME_WINDOW` | `window_ms`, `max_count` | 同一付款方在以本交易为右端点的时间窗内交易笔数 > 上限（并列按 tx_hash 排序，结论确定） |
| `ADDRESS_FANOUT` | `direction`(out/in), `min_count`, `within_ms` | 锚点地址在窗口内交易过的不同对手数 ≥ 阈值 |
| `WATCHED_ADDRESS` | `addresses[]`, `match`(from/to/either) | 收/付款地址命中名单 |

每次发布/更新都会产生**新的规则集版本（rule_set_version）**，规则集只追加不修改。
风险分是命中规则分数之和，封顶 100；标签为命中规则 tag 的去重排序列表。

### 2.3 风险查询与逐条依据

```
GET /api/v1/risk/0xaa01?rule_set_version=1
GET /api/v1/risk/0xaa01?as_of_ms=1759000000000
```

- 每条命中都有独立 evidence：`rule_id`、`rule_version`、`rule_set_version`、`score`、
  `tag`、人类可读的 `detail`（判定依据）。
- **一次查询的所有 evidence 必定来自同一个规则集版本**，新旧规则不会混算。
- `counterparties` 给出地址关系图（有界 BFS）遍历到的关联方；`cycle=true` 标环路，
  `truncated=true` + `graph_truncation_reason` 标部分结果。

---

## 3. 版本与时间语义（重点）

系统中有两类单调版本：

- **data_version**：每次有新交易/新关系实际入库时 +1（幂等回放不 +1）。
- **rule_set_version**：每次发布或回滚规则时 +1。`v0` 是空规则集。

风险查询两种历史复现方式：

1. **指定规则集版本** `rule_set_version=N`
   精确使用第 N 版规则集（含每条规则当时的 rule_version 与分值），可复现当时结论；
   已回滚的历史版本依然可查。
2. **指定历史时点** `as_of_ms=T`
   - 规则：选择「发布时间 ≤ T 的最新规则集」；
   - 数据：只统计「提交时刻 ≤ T」已存在的交易；T 之后才导入的交易返回 `found=false`。

**回滚语义**：回滚不是删除历史，而是把某条规则的旧定义**重新发布为一个新的
rule_version**（版本号继续单调递增）。因此：回滚后用新版本复现旧结论，而任何旧的
rule_set_version 仍原样保留。

批次响应里的 `data_version` / `rule_set_version` 记录了「这批数据在哪个版本下导入、
当时规则集是哪版」，配合 `GET /batches/{id}` 可追溯。

---

## 4. 幂等与并发边界

- 所有写操作在服务层串行化（`write_lock`），底层 Store 用一把可重入锁 +
  **不可变快照**。读取只取一个快照，因此任何查询看到的都是一个完整版本，不会读到
  「数据写了一半、规则切了一半」的中间态。
- 并发提交**同一个批次**：只有一次真正入库，其余全部 `replayed=true`，data_version 不重复增长。
- 并发提交**重叠批次**（共享 tx_hash）：共享交易只计一次，其它请求得到 `TX_HASH_DUPLICATE`。
- 并发发布规则：规则集版本按完全顺序线性增长，任意版本都是一套完整规则。
- 并发导入关系：同一条边只入库一次。

---

## 5. 上限与超限行为（不卡死、不静默截断）

硬上限（超限**整体拒绝**，返回稳定错误码，不落任何数据）：

| 限制 | 默认 | 错误码 |
| --- | --- | --- |
| 单批次交易条数 | 10,000 | `BATCH_TOO_LARGE` |
| 单批关系条数 | 5,000 | `RELATION_BATCH_TOO_LARGE` |
| 单笔金额 | 1e18 | `AMOUNT_TOO_LARGE` |
| 时间窗 | 过去 365 天 / 未来 5 分钟 | `TS_OUT_OF_RANGE` |

软上限（返回**有明确标记的部分结果**）：

| 限制 | 默认 | 标记 |
| --- | --- | --- |
| 单节点邻居数 | 200 | `FANOUT_TOO_LARGE` |
| 遍历总节点数 | 10,000 | `GRAPH_NODE_CAP` |
| 遍历深度 | 6 | 深度以外不展开（不报错） |

- 环路：检测回边，关联方标 `cycle=true`，`notes` 提示环上成员，绝不无限递归。
- 所有限制集中在 `app/limits.py`，可调。
- 内部异常不会透出底层信息：未预期错误统一返回
  `500 {"error":"INTERNAL_ERROR", ...}`（业务校验错误使用稳定业务码，见下）。

### 稳定拒绝码

`BATCH_ID_MISSING` / `BATCH_EMPTY` / `BATCH_TOO_LARGE` / `BATCH_REPLAY_MISMATCH`(409)、
`ITEM_NOT_OBJECT` / `ITEM_FIELD_MISSING` / `TX_HASH_INVALID` / `ADDRESS_INVALID` /
`AMOUNT_NOT_NUMBER` / `AMOUNT_NOT_POSITIVE` / `AMOUNT_TOO_LARGE` /
`TS_INVALID` / `TS_OUT_OF_RANGE` /
`ITEM_DUPLICATE_IN_BATCH` / `ITEM_CONFLICT_IN_BATCH` /
`TX_HASH_DUPLICATE` / `TX_HASH_CONFLICT` /
`RULE_INVALID` / `RULE_VERSION_NOT_FOUND` /
`VERSION_NOT_FOUND` / `ASOF_INVALID` / `RELATION_BATCH_TOO_LARGE`。

---

## 6. 日志

每个关键动作打一行 JSON 日志（logger 名 `risk.api`），包含批次、版本和判定依据：

- `batch_submit` / `batch_replay`：batch_id、data_version、rule_set_version、
  status、accepted/duplicate/rejected、各类拒绝原因计数；
- `rule_publish` / `rule_rollback`：rule_id、rule_version、rule_set_version；
- `risk_query`：tx_hash、data/rule_set 版本、score、labels、每个命中的
  `{rule_id, rule_version, tag, score}`；
- `relations_import`：接受/拒绝边数与 data_version。

---

## 7. 本地验证清单

```bash
uv run pytest -q
# 覆盖：重复/重叠批次、非法数据拒绝码可区分、部分成功标记、
#       规则发布/升级/回滚、固定版本与 as_of 历史复现、
#       并发读写线性一致性、高扇出/节点上限、环路、异常不透出、日志内容。
```

手动端到端：

```bash
uv run python main.py &
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/v1/batches -H 'Content-Type: application/json' \
  -d '{"batch_id":"b1","transactions":[
       {"tx_hash":"0xaa01","from_addr":"0xa101","to_addr":"0xb101",
        "amount":250,"ts_ms":1759000000000}]}'
curl -s -X POST localhost:8000/api/v1/rules -H 'Content-Type: application/json' \
  -d '{"rule_id":"big","kind":"TX_AMOUNT","name":"big","score":40,
       "tag":"LARGE","params":{"min_amount":100}}'
curl -s localhost:8000/api/v1/risk/0xaa01
curl -s "localhost:8000/api/v1/risk/0xaa01?rule_set_version=1"
```

---

## 8. 代码结构

```
app/
  clock.py           可注入时钟（测试里固定/推进时间，历史回放确定）
  limits.py          所有硬/软上限
  errors.py          稳定错误码 + ServiceError
  models.py          响应模型
  validation.py      手工逐行校验（稳定拒绝码、批次内重复/矛盾）
  store.py           内存 MVCC 存储：锁 + 不可变快照、交易/关系/规则集版本
  graph.py           有界 BFS：深度/扇出/节点上限、环路检测、截断标记
  rules.py           四类规则引擎，逐条 evidence，不混版本
  service.py         应用服务：幂等、并发边界、版本/时间语义
  api.py             FastAPI 路由与统一错误处理
  logging_config.py  JSON 行式日志
tests/               33 个测试（批次/规则版本/并发/图上限/日志）
```
