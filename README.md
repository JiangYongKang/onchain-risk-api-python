# onchain-risk-api

链上风险识别 API：交易批次导入、地址关系图谱、版本化规则（发布/回滚）、
历史回溯查询，以及**规则试算（dry-run）**——新规则上线前先看影响面。

## 快速开始

```bash
uv sync                      # 安装依赖
uv run uvicorn app.main:app  # 启动服务
uv run pytest -v             # 跑全部测试（-v 可看到每条用例对应的业务场景）
```

## 核心概念

- **数据版本（data_version）**：每次批次导入追加一个不可变数据快照，版本号单调递增。
- **规则版本（rule_version）**：每次发布/回滚追加一个不可变规则集版本；
  回滚是“复制目标版本内容生成新版本”，历史版本永远可复现。
- **风险结论 = f(数据版本, 规则版本)**：同一对版本在任何时刻查询结果完全一致，
  这是历史回溯与试算一致性的基础。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/transactions/batches` | 批次导入（按 `batch_id` 幂等；同 ID 不同载荷返回 `BATCH_CONFLICT`） |
| POST | `/rules/publish` | 发布新规则版本并生效 |
| POST | `/rules/rollback` | 回滚到指定历史版本（生成新版本） |
| GET  | `/rules/versions` | 列出全部规则版本 |
| GET  | `/risk` | 风险查询，支持 `data_version` / `rule_version` / `as_of` |
| POST | `/rules/dry-run` | **规则试算**（不生效、不污染正式结论） |

## 规则试算（dry-run）

拿一套候选规则（1~50 条）在指定数据上试算，与基线规则对比，输出每笔交易的
新增命中（`added`）、消失命中（`removed`）、分数/标签变化（`changed`）。
每条差异两侧都带命中的 `rule_id`，能说清“是候选里哪条规则带来的”以及
“旧结论是哪条规则贡献的”。

```json
POST /rules/dry-run
{
  "candidate_rules": [
    {"rule_id": "cand-mid", "score": 40, "labels": ["mid"],
     "condition": {"min_amount": 50}}
  ],
  "baseline":   {"type": "current"},                  // current | version | as_of
  "data_scope": {"type": "current"},                  // current | version | as_of
  "max_diffs": 100                                    // 可选，限制返回的差异条数
}
```

响应包含：

- `summary`：评估笔数、两侧命中笔数、added/removed/changed 计数
  （**始终按完整差异集统计**，不受 `max_diffs` 截断影响）；
- `diffs[]`：逐笔差异，`candidate` / `baseline` 两侧各有 `score`、`labels`、
  `hits[].rule_id`；`diffs_truncated=true` 表示差异条目被 `max_diffs` 截断；
- `coverage`：覆盖范围（见下文“容量边界”）。

### 与历史版本 / 历史时点配合

- `baseline.type = "version"` + `rule_version`：与某个历史规则版本对比；
- `baseline.type = "as_of"` + 时间戳：与“那个时点生效的规则”对比；
- `data_scope.type = "version" / "as_of"`：钉住数据快照；
- `data_scope.tx_ids` / `data_scope.limit`：缩小评估范围。

`as_of` 会**同时**钉住数据与规则到同一时点，保证对比与当时结论自洽，
不会把不同版本的数据和规则混在一起算（`as_of` 不允许与显式版本号混用，
混用返回 `VALIDATION_ERROR`）。

### 试算 = 上线后的结果

试算与正式发布共用同一个确定性求值引擎。把同一套候选规则发布后、按同一
`data_version` 查询，结果与试算的 candidate 侧逐笔一致（有自动化测试锁定：
`test_dry_run_matches_post_publish_results_on_same_data_version`）。

### 并发与隔离

- 存储为追加式不可变版本 + 原子切换当前指针；试算在开始时钉住
  （数据版本， 规则版本）引用，期间他人的导入/发布/回滚/查询互不影响；
- 试算不写任何状态：不产生规则版本、不进版本化缓存、不影响 `/risk` 结论；
- 并发隔离有专门测试：`tests/test_dryrun_concurrency.py`（并发写入/查询中
  的试算可按钉住版本精确重放）。

### 容量边界（超限反馈稳定、可区分）

| 限制 | 值 | 超限行为 |
|---|---|---|
| 候选规则条数 | 50 | `DRY_RUN_LIMIT_EXCEEDED`（422） |
| 单次试算交易数 | 20,000 | `DRY_RUN_LIMIT_EXCEEDED`（422），提示缩小范围或设 `data_scope.limit` |
| 单次风险查询交易数 | 100,000 | `DRY_RUN_LIMIT_EXCEEDED`（422） |
| `data_scope.limit` 小于命中数据量 | — | 不报错，返回部分结果并标注 `coverage.full=false`、`reason="SCOPE_LIMIT"`、`scanned/total` |

所有错误都是 `{"error": {"code", "message"}}` 形态，错误码稳定
（`BATCH_CONFLICT`、`RULE_VERSION_NOT_FOUND`、`DATA_VERSION_NOT_FOUND`、
`INVALID_AS_OF`、`VALIDATION_ERROR`、`DRY_RUN_LIMIT_EXCEEDED`、
`INTERNAL_ERROR`）；内部异常一律包装为 `INTERNAL_ERROR`，不外泄细节。

## 本地验证

```bash
uv run pytest -v
```

测试按业务场景组织：

- `tests/test_storage.py` — 批次幂等/冲突、版本解析、as_of 解析
- `tests/test_engine.py` — 扇出/扇入/环路（含自环）、规则匹配、差异对比
- `tests/test_service.py` — 回滚还原结论、历史复现自洽、并发读写一致性、
  环路与高扇出端到端
- `tests/test_dryrun.py` — 试算差异与归因、历史基线、范围裁剪、
  超限反馈、部分覆盖标注、试算=上线一致性
- `tests/test_dryrun_concurrency.py` — 试算与并发写入/查询隔离
- `tests/test_api.py` — HTTP 端到端、错误形态稳定、内部错误不外泄
