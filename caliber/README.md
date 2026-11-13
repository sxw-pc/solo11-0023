# 指标准星（Caliber）

指标准星是一个面向小团队的**离线指标定义与计算一致性校验工作台**。它把「活跃用户数」「转化率」「客单价」这类业务口径从散落的 SQL 和报表说明中抽出来，变成可声明、可版本化、可测试的本地资产，并对多个实现执行结果做逐维度比对。

它解决的具体问题是：同一个指标由不同 SQL、不同数据源或不同人实现后，同名不同值；口径变化没有历史可查；发现差异时无法定位到具体维度和数值。指标准星用本地 SQLite 仓库保留定义版本与每次判定快照，并用 JSON / Markdown / HTML 报告呈现差异。

## 1. 项目定位与边界

**是：**

- 指标定义 DSL（YAML）与结构化校验。
- 指标仓库：SQLite 存储、自增版本、历史读取、字段级版本差异。
- 离线数据源：目录内 CSV 加载到内存 SQLite，支持编码、表头规范化与类型推断。
- 多实现比对：维度对齐、缺失维度识别、绝对/相对容差判定。
- 口径快照、`consistent → inconsistent` 漂移检测、审计日志。
- 本地 Flask 页面、HTTP API、命令行入口与三种报告格式。

**不是：**

- 不是 BI 拖拽看板，不做图表库。
- 不是 OLAP / 实时流计算 / 分布式执行平台。
- 不提供用户体系、权限、告警或定时调度。
- 不做指标血缘图谱、指标依赖计算、自然语言转 SQL。
- 不直连 MySQL、Postgres 等在线数据库；只支持 CSV 与 SQLite。
- 不做指标推荐、自动生成或爬虫采集。

## 2. 快速上手

要求 Python 3.10+。项目运行期不需要 Node、CDN 或任何网络服务。

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
source .venv/bin/activate
pip install -r requirements.txt

# 1) 导入三个 fixture 指标（写入 instance/caliber.sqlite3）
python -m app import-metrics --dir fixtures/metrics

# 2) 执行一次全量比对：两套数据源上同名指标的一致性
python -m app run --metrics active_users,conversion_rate,avg_order_value \
  --datasets fixtures/datasets

# 3) 导出最近一次运行的 Markdown 报告
python -m app report --last --format md

# 4) 启动本地页面（仅 127.0.0.1:5057）
python -m app serve
```

浏览器访问 <http://127.0.0.1:5057/>。页面展示指标列表、指标详情与版本历史、运行页和报告页。

测试与可选覆盖率：

```bash
pytest
pytest --cov=app/core --cov-report=term-missing
```
导出的 JSON 报告是机器可读结果；`report --format json --output report.json` 可写入文件。HTML 报告是单文件、内联样式的独立页面。

## 3. 指标 DSL

一份指标定义是一个 YAML 文件。顶层字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | string | 唯一标识，匹配 `^[a-z][a-z0-9_]{2,40}$` |
| `name` | string | 中文或业务展示名 |
| `owner` | string | 负责人/团队标识，不使用真实个人信息 |
| `description` | string | 口径说明，可为空字符串 |
| `unit` | string | 展示单位，如 `人`、`%`、`元` |
| `grain` | enum | `day` / `week` / `month` |
| `impls` | list | 至少两项；每项含 `id`、`source`、`sql_file` |

实现字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 指标内唯一的实现标识，匹配 `^[a-z][a-z0-9_]{1,40}$` |
| `source` | 数据集 key，对应 `fixtures/datasets/<key>.csv` |
| `sql_file` | 相对路径，只能在配置的受信任 SQL 目录内解析 |

完整示例（与 `fixtures/metrics/active_users.yaml` 一致）：

```yaml
# 合成 fixture：同名指标，两套结构相同的数据源。预期判定 consistent。
key: active_users
name: 活跃用户数
owner: data-platform
description: 按最后活跃日期统计的去重活跃用户数。
unit: 人
grain: day
impls:
  - id: impl_a
    source: users_a
    sql_file: active_users_impl_a.sql
  - id: impl_b
    source: users_b
    sql_file: active_users_impl_b.sql
```

SQL 约定：

- 只允许单条 `SELECT` 或 `WITH ... SELECT` 查询。
- 返回 1 列（数值）或 2 列（维度, 数值）。单列聚合结果的维度为 `__all__`。
- 维度值在 `grain=day/week/month` 下按 ISO 日期截断；非日期维度原样保留。
- 同一查询不得返回重复维度；结果行数默认不超过 10,000（按实现独立生效，可用 `--max-rows` 调整，超限记为行数截断降级）。
- 指标值必须是数值；空值、字符串、布尔值会得到明确的求值错误。

校验器会检查 key 格式、粒度枚举、至少两个实现、实现 ID 重复、SQL 文件存在性及路径白名单。错误以字段级列表返回。

## 4. 一致性判定规则

对同一指标的每个实现，分别得到 `维度 -> 数值` 的字典，然后对所有实现的结果取维度并集逐项比较。数值相等判定为：

```text
|a - b| <= abs_tolerance + rel_tolerance * max(|a|, |b|)
```

边界值按「不超过」视为一致。默认绝对容差为 `1e-6`，相对容差为 `0.0`；可在 CLI 使用 `--abs-tol` / `--rel-tol`，或在 API 请求体传入 `tolerances: {abs, rel}`。

判定为三态：

- `consistent`：所有实现拥有相同维度集合，且每个维度的数值都在容差内。
- `inconsistent`：至少一个维度存在数值超出容差。
- `partial`：不存在数值超差，但至少一个实现缺少某个维度。

`inconsistent` 优先于 `partial`。每次指标比对写入一条快照，记录指标 key、版本号、数据集、实现集合、判定、差异摘要与时间戳。若同一指标的上一条快照为 `consistent`、当前快照为 `inconsistent`，报告会标记一条漂移事件。

### 4.1 批内降级（超时 / 行数截断）

求值超时（默认 5 秒）与结果行数上限（默认 10,000 行）**按每条指标的每个实现独立生效**，而不是整批共享。某条指标触发限制时只把该指标标记为降级，批内其余指标继续执行：

- `timeout`：实现查询在截止时间被真正打断（SQLite progress handler 触发 `interrupt`），快照记录超时阈值与失败实现。
- `row_limit`：结果超过上限后显式排空游标，记录**截断前的实际行数**与上限值；若排空过程本身超时，`row_count_exact` 为 `false`，此时实际行数是下界。

每条指标求值结束（无论成功或降级）都会回滚事务、关闭游标并清除 progress handler，复用的只读连接不会残留悬挂状态。

**降级快照策略（显式选择：落库并打标）**：降级指标仍写入一条 `verdict = "degraded"` 的快照，使报告与看板能展示该指标被尝试过；但该行不包含任何比对结论。下游做一致性汇总时必须先排除降级行，识别方式（同时写入 `runs.summary_json.execution.degradation.downstream_detection`）：

- `snapshot.verdict == "degraded"`
- `snapshot.summary.degraded == true`
- `snapshot.result.degraded`（含 `reason`、`failed_impl_id`、`max_rows`、`observed_rows`、`row_count_exact`、`timeout_seconds`）
- `snapshot.summary.degraded_reason in {"timeout", "row_limit"}`

snapshots 表字段结构保持不变，降级标记只存在于既有的 `summary_json` / `result_json` 内，老报告读取方式不受影响。降级快照不参与漂移检测。

`runs.summary_json` 的计数：

| 字段 | 含义 |
| --- | --- |
| `consistent` / `inconsistent` / `partial` | 正常完成指标的判定计数 |
| `completed` | 正常完成指标数（= 上述三项之和） |
| `degraded_timeout` | 超时降级数 |
| `degraded_row_limit` | 行数截断降级数 |
| `degraded` | 降级总数 |
| `degraded_metrics` | 逐条降级明细（key、版本、原因、失败实现、消息） |
| `execution` | 限制作用域与降级策略、下游识别方式 |
| `failed` | 硬失败数（仅 `RunError` 路径非零） |

只有降级、没有硬失败时，run 状态仍为 `completed`，CLI 退出码为 `0`；CLI 会在输出中逐条打印降级指标及其原因。`--timeout` 与 `--max-rows` 可覆盖默认限制。

## 5. 目录结构

```text
caliber/
├── README.md
├── requirements.txt
├── pyproject.toml
├── app/
│   ├── __init__.py          # Flask 工厂、本地页面、错误边界
│   ├── __main__.py          # python -m app
│   ├── config.py            # 本地配置与回环地址约束
│   ├── db.py                # SQLite schema
│   ├── models.py            # 数据类型
│   ├── errors.py            # 领域异常
│   ├── audit.py             # 审计日志
│   ├── api/                 # 指标、运行、数据集、报告 API
│   ├── core/                # DSL、校验、仓库、求值、比对、快照、报告
│   ├── templates/           # 本地 Jinja 页面
│   └── static/              # 原生 CSS/JS，无外部依赖
├── cli/main.py              # serve / import-metrics / run / report
├── fixtures/
│   ├── datasets/            # 合成 CSV
│   ├── metrics/             # 合成 YAML 指标
│   └── impls/               # 合成只读 SQL
└── tests/                   # pytest 全链路测试
```

## 6. 主要 HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/metrics` | 指标列表 |
| GET | `/api/metrics/<key>` | 最新版本 |
| GET | `/api/metrics/<key>/versions` | 全部历史版本 |
| GET | `/api/metrics/<key>/versions/<n>` | 指定版本 |
| GET | `/api/metrics/<key>/diff?from=1&to=2` | 版本字段差异 |
| GET | `/api/datasets` | 数据集元信息 |
| GET | `/api/datasets/<key>/preview` | 数据预览 |
| POST | `/api/runs` | 触发同步比对 |
| GET | `/api/runs` / `/api/runs/<id>` | 运行历史与快照 |
| GET | `/api/reports/<run_id>?format=json\|md\|html` | 报告导出 |

`POST /api/runs` 示例请求体：

```json
{
  "metrics": ["active_users", "avg_order_value"],
  "datasets_dir": "fixtures/datasets",
  "tolerances": {"abs": 0.000001, "rel": 0}
}
```

## 7. 安全与合规

- **只读 SQL**：语句白名单只允许 `SELECT` / `WITH`；显式拒绝写入、DDL、ATTACH、PRAGMA、事务控制等关键字，并拒绝多语句。求值连接设置 `PRAGMA query_only = ON`，每次查询在事务中执行后回滚。违规语句抛出 `unsafe_sql`，测试覆盖写操作和 ATTACH。
- **路径白名单**：`sql_file` 只能相对受信任 SQL 目录解析。绝对路径、`..`、锚点路径以及逃逸出根目录的解析结果均被拒绝。
- **仅本机**：默认且只允许监听 `127.0.0.1`（或同义回环地址）；无鉴权，因为定位是单机工作台。
- **完全离线**：运行期不发起网络请求。模板不引用 CDN、远程字体、遥测或外部 API。
- **审计**：导入、运行、失败与报告导出写入本地 `audit_log`；异常由 CLI/HTTP 边界统一捕获，不静默失败。
- **无真实敏感信息**：仓库没有真实密钥、令牌、公司名、真实域名或真实 IP。

## 8. 数据与 fixture 声明

`fixtures/` 中全部 CSV、YAML、SQL 均为本项目的**合成数据**，不对应任何真实用户、订单、组织或业务系统。用户 ID、订单 ID、渠道名、金额和时间均为手工构造；仅用于演示一致与不一致两类场景：

- `active_users`：两套用户数据的活跃日期分布相同，预期 `consistent`。
- `conversion_rate`：两套订单金额不同，但按日支付订单比例相同，预期 `consistent`。
- `avg_order_value`：两套订单金额被刻意设置不同，预期 `inconsistent`，用于验证差异明细与漂移链路。

任何示例使用的域名仅限 `example.invalid`、`localhost` 或 `127.0.0.1`；本项目实际上不做 DNS 或 HTTP 出站访问。

## 9. 已知限制与后续方向

当前限制：

- 单进程同步执行，一个运行内按指标串行求值。
- CSV 全量加载到内存 SQLite；超大文件不适合。
- SQL 结果契约固定为 1–2 列；多维度组合需要预先拼成一个维度列。
- 漂移只检测相邻快照的 `consistent → inconsistent`，不检测版本定义差异引起的语义漂移。
- 无用户、权限、调度、告警和报告持久化文件管理。
- 报告 HTML 是独立静态字符串，和本地页面不是同一模板系统。

后续可迭代：SQL 结果多列维度、指标依赖与血缘、批处理队列、更多离线数据源适配器、报告归档目录、版本升级时的口径审批流。当前版本有意不包含这些能力。
