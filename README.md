# On-chain Risk API（含规则试算 / Dry-Run）

一个进程内、线程安全、**全版本化**的链上风险服务。支持交易批次导入、地址关系
（高扇出 / 环路）、规则发布与回滚、历史时点复现，以及本次新增的核心能力——
**规则试算（rule dry-run）与版本差异对比**。

> 存储为内存实现（append-only 日志 + 不可变快照），重点是把“版本一致性、可复现、
> 互不干扰、容量边界”这些语义做扎实；接口与领域逻辑与存储实现解耦。

---

## 1. 数据与版本模型（理解一切的前提）

* **数据版本 `dN`（data version）**：每导入一个批次，append-only 数据日志就增加一个
  前缀版本 `dN`。`dN` = “前 N 个批次”构成的不可变快照，永不改变。
* **规则版本 `vN`（rule version）**：每次发布产生一条不可变规则版本，构成只增链。
  回滚不是删除，而是把旧版本的规则**原样再发布成一个新版本**，链条始终可审计。
* **求值是纯函数**：`评估(规则版本, 数据快照) → 每笔交易的命中/分数/标签`。
  交易按 `(ts, tx_id)` 固定顺序处理，命中按 `rule_id` 排序、标签去重排序，因此
  *同一份（规则, 数据）永远得到完全相同的结果*。

正是这套模型保证了：**试算看到什么，发布后用同一个数据版本查，就是什么**。

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/batches` | 导入交易/关系批次（按 `batch_id` 幂等） |
| POST | `/v1/rules/publish` | 发布一套规则，生成新 `vN` |
| POST | `/v1/rules/rollback/{version}` | 回滚到指定规则版本（再发布） |
| GET  | `/v1/rules/current` | 当前生效规则版本 |
| GET  | `/v1/transactions/{tx_id}` | 单笔交易风险（可钉版本/时点） |
| GET  | `/v1/addresses/{address}` | 地址聚合风险（可钉版本/时点） |
| POST | `/v1/dry-runs` | **规则试算 + 差异对比** |
| GET  | `/health` | 健康检查 |

启动：

```bash
uv run uvicorn main:app --reload
# 交互文档：http://127.0.0.1:8000/docs
```

### 规则类型

| `rule_type` | 含义 | `params` |
| --- | --- | --- |
| `amount_over` | 交易金额 ≥ 阈值命中 | `{"threshold": number}` |
| `address_blacklist` | 转出或转入地址在名单中 | `{"addresses": [string,...]}` |
| `high_fanout` | 转出地址**去重出度** ≥ 阈值 | `{"threshold": int≥1}` |
| `loop_detect` | 该笔交易的边闭合一个有向环 | `{}` |

多规则命中时：**分数相加、标签取并集**；只要有任意命中 `risk=true`。

### 导入

```jsonc
POST /v1/batches
{
  "batch_id": "2026-10-08-batch-a",
  "imported_at": 1728400000000000,   // 可选，epoch 微秒；缺省用服务端时钟
  "transactions": [
    {"tx_id": "t1", "from": "A", "to": "B", "amount": 200, "ts": 1}
  ],
  "edges": [ {"from": "X", "to": "Y"} ]   // 额外的地址关系，参与环路/扇出
}
```

* 同一 `batch_id` + **相同业务内容**：幂等，返回 `"imported": false, "deduped": true`，
  不产生新数据版本（重试时即使没带 `imported_at` 也算重复，以首次时间戳为准）。
* 同一 `batch_id` 但内容不同：`409 CONFLICT`。

---

## 3. 规则试算怎么用

拿**一套候选规则（一条或多条）**在现有数据上“试跑”，**绝不生效、绝不写库**，并与
某个**基线**逐条对比：

```jsonc
POST /v1/dry-runs
{
  "candidate_rules": [
    {"rule_id": "mid", "rule_type": "amount_over", "score": 5,
     "tags": ["mid"], "params": {"threshold": 50}},
    {"rule_id": "loop", "rule_type": "loop_detect",
     "score": 7, "tags": ["cycle"], "params": {}}
  ],
  "baseline": {"kind": "current"},          // 见下：基线选择
  "scope":   {"kind": "current"}            // 见下：数据范围
}
```

### 返回结构（关键字段）

* `data_version` / `baseline_rule_version`：本次试算实际钉住的数据版本与基线规则版本。
* 三类差异列表，每条差异都带**双向规则归因**：
  * `new_hits`：候选命中、基线不命中（**多了哪些命中**）。
  * `lost_hits`：基线命中、候选不命中（**少了哪些命中**）。
  * `changed`：两边都命中但结论不同，进一步分为
    `score_changed` / `tags_changed` / `hit_rules_changed`
    （分数、标签、或“分数标签都相同但贡献规则集合变了”）。
* 每个 `TxDiff`：
  * `baseline` / `candidate`：该笔在两侧的完整视图（分数、标签、命中规则 id）。
  * `baseline_contributors` / `candidate_contributors`：**这条差异是基线哪条规则 /
    候选哪条规则贡献的**（rule_id、该规则分数、标签）。
  * `added_tags` / `removed_tags` / `score_delta`：标签增减与分数差。
* 覆盖统计：`total_tx_count`、`in_scope_tx_count`、`covered_tx_count`、`covered`
  （实际计算的 tx_id）、`missing_tx_ids`（指定了但该版本不存在的 tx）。
* 完整性标注：`complete`、`truncated`、`partial_reason`（稳定原因值：
  `truncated` / `missing_tx` / `truncated_and_missing_tx`）、`warnings`，以及
  `new_hits_count / lost_hits_count / changed_count`（截断前列表总数）。

### 试算 ≠ 生效

* 试算不产生规则版本，`/v1/rules/current` 与所有正式查询看到的仍是已发布版本。
* 候选规则本身会先做合法性校验（重复 id、参数类型/取值等），非法直接拒绝，且不写库。

### 可复现保证（重要）

试算结果与“之后真的把这套候选规则发布、再用**同一个数据版本**查询”的结果**逐字段一致**：
两者走的是同一个确定性求值引擎，输入是同一份不可变规则内容与同一份数据前缀。
因此建议把试算响应里的 `data_version` 记下来，发布后用
`GET /v1/transactions/{id}?data_version=<那个版本>` 复核。

---

## 4. 和历史版本 / 历史时点怎么配合

### 基线选择 `baseline`

| `kind` | 含义 | 额外字段 |
| --- | --- | --- |
| `current`（默认） | 当前生效规则版本 | — |
| `rule_version` | 指定某个历史规则版本 `vN` | `"rule_version": N` |
| `as_of` | 该历史时点**当时生效**的规则版本 | `"as_of": <epoch us>` |

### 数据范围 `scope`

| `kind` | 含义 | 额外字段 |
| --- | --- | --- |
| `current`（默认） | 最新数据 | — |
| `data_version` | 精确钉住某个数据前缀 `dN` | `"data_version": N` |
| `as_of` | 该时点之前（含）导入的数据前缀 | `"as_of": <epoch us>` |
| （任意 kind） | 可叠加 `"tx_ids": [...]` 只看子集 | — |

### 自洽规则（不会把版本混着算）

* **同一次求值的一侧**，永远是“一份不可变规则版本 + 一份不可变数据前缀”，
  不会在中途换版本，也不会出现“半新半旧”的数据/规则。
* 当基线用 `baseline.kind = "as_of"` 且**没有显式给 `scope`** 时，数据会**自动钉到
  同一历史时点**，保证对比与“当时的结论”自洽（响应 `warnings` 会提示这一自动钉版）。
* 你也可以**显式**让“旧规则 × 新数据”（`baseline.rule_version` + `scope.current`），
  这是调用方的明确选择，系统会如实标注两侧版本。
* 正式查询同样自洽：`GET ...?as_of=<t>` 会同时使用 t 时刻的数据前缀与当时生效的规则；
  `?rule_version=N` / `?data_version=N` 则是显式钉版。不存在的历史对象返回
  `404 NOT_FOUND`，而不会拿当前数据糊弄。

---

## 5. 并发与隔离

* 所有写入（导入 / 发布 / 回滚）与“取已发布状态”的动作由同一把可重入锁保护；
  正式读者拿到的永远是一个**完整版本**，不会读到半成品。
* 试算只在取不可变快照的瞬间短暂持锁，随后在快照上**无锁求值**。因此：
  * 试算期间，别人照常导数据、发规则、查风险，看到的始终是一个完整正式版本；
  * 试算开始后发生的导入/发布**不会**改变本次试算结果（不会看到“移动靶”）；
  * 候选规则不会被当作已生效规则，也不会改写任何历史结论；
  * 多个试算互不干扰，结果确定且可重复。

---

## 6. 容量边界与部分覆盖（不卡死、不少算、不泄漏）

上限通过环境变量配置（默认值如下）：

| 环境变量 | 默认 | 命中后的行为 | 稳定错误码 |
| --- | --- | --- | --- |
| `RISK_MAX_CANDIDATE_RULES` | 200 | 直接拒绝（HTTP 413） | `DRYRUN_RULES_LIMIT_EXCEEDED` |
| `RISK_MAX_TX_SCANNED` | 1,000,000 | 范围内交易过多，直接拒绝（413） | `DRYRUN_SCOPE_TOO_LARGE` |
| `RISK_MAX_TX_REPORTED` | 100,000 | **截断**：只算确定性顺序前 N 笔 | 200 + `truncated=true` |
| `RISK_MAX_DIFF_ENTRIES` | 10,000 | 每个差异列表最多返回 N 条 | 200 + `truncated=true` |

* **硬上限（前两类）**：在真正计算前就拒绝，反馈快速、稳定、可区分，`details` 里带
  `submitted/in_scope` 与 `limit`。
* **截断（后两类）**：不会卡死、也不会悄悄少算——响应里明确给出
  `truncated=true`、`complete=false`、`partial_reason`、`covered`（实际覆盖到哪些 tx，
  按 `(ts, tx_id)` 确定性顺序）、各 `*_count` 总数，以及可读的 `warnings`。
  需要“宁可报错也不要截断”时，服务层可传 `allow_truncation=False`，得到
  `DRYRUN_REPORT_LIMIT_EXCEEDED`。
* 指定的 `tx_ids` 在钉住的数据版本里不存在时，不会被静默忽略：进入 `missing_tx_ids`，
  并把 `complete` 置为 `false`。
* 所有领域错误都返回统一信封
  `{"error": {"code", "message", "details"}}`；未预期的内部异常只返回通用
  `INTERNAL_ERROR`（HTTP 500），内部细节仅记日志，不抛给调用方。

---

## 7. 本地验证

```bash
# 1) 安装依赖（dev 组含 pytest / ruff）
uv sync

# 2) 一键跑全部自动化测试（-v 可看到每条用例名与其覆盖的业务场景）
uv run pytest -v
# 只看本次新增的几块：
uv run pytest -m "dryrun or diff or isolation or limits" -v
# 只看历史能力回归：
uv run pytest -m regression -v

# 3) lint
uv run ruff check app tests

# 4) 起服务
uv run uvicorn main:app --reload
```

### 测试覆盖了什么

| 文件 | 业务场景 |
| --- | --- |
| `tests/test_01_import.py` | 重复导入幂等、内容冲突、批内重复 tx、数据版本递增（回归） |
| `tests/test_02_rules.py` | 发布、参数校验、回滚链（回归） |
| `tests/test_03_graph_history.py` | 环路 / 高扇出 / 自环、历史时点复现、显式版本钉版（回归） |
| `tests/test_04_concurrency.py` | 并发导入 / 重复导入 / 发布期间查询的一致性（回归） |
| `tests/test_05_dry_run.py` | 试算不生效、候选视图、多规则聚合、试算==发布后同版本查询、子集范围 |
| `tests/test_06_diff.py` | 新增/丢失命中、分数/标签/规则集合变化、双向规则归因、历史版本/时点基线自洽 |
| `tests/test_07_limits.py` | 三类可区分错误码、截断覆盖标注、缺失 tx、完整性标记 |
| `tests/test_08_isolation.py` | 试算与并发写入/查询隔离、多试算互不干扰、全集图结构对子集可见 |
| `tests/test_09_api.py` | HTTP 契约、稳定错误信封、413、截断标注、内部异常不泄漏 |

用 `-v` 运行时，每条用例名即“它覆盖的业务场景 + 期望结果”。

### 手动快速验证一遍

```bash
curl -s localhost:8000/v1/batches -H 'content-type: application/json' -d '{
  "batch_id":"b1",
  "transactions":[
    {"tx_id":"t1","from":"A","to":"B","amount":200,"ts":1},
    {"tx_id":"t2","from":"B","to":"C","amount":60,"ts":2},
    {"tx_id":"t3","from":"C","to":"A","amount":30,"ts":3}]}'

curl -s localhost:8000/v1/rules/publish -H 'content-type: application/json' -d '{
  "rules":[{"rule_id":"amt","rule_type":"amount_over","score":10,
            "tags":["big"],"params":{"threshold":100}}]}'

curl -s localhost:8000/v1/dry-runs -H 'content-type: application/json' -d '{
  "candidate_rules":[
    {"rule_id":"mid","rule_type":"amount_over","score":5,"tags":["mid"],
     "params":{"threshold":50}},
    {"rule_id":"loop","rule_type":"loop_detect","score":7,
     "tags":["cycle"],"params":{}}]}'
# 预期：t2/t3 进入 new_hits（分别归因 mid / loop），t1 进入 changed。
# 同时 GET /v1/rules/current 仍只有 amt —— 试算未生效。
```
