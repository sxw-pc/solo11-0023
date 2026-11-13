# 口径对齐（可比性声明）设计讨论

- 状态：讨论稿（Proposed），不含代码交付
- 范围：caliber 跨数据集实现比对的归一化层
- 关联代码：`app/core/evaluator.py`、`app/core/comparator.py`、`app/core/snapshot.py`、`app/models.py`、`app/core/dsl_parser.py`、`app/core/dataset_loader.py`、`app/core/reporter.py`

---

## 0. 结论先行

如果只记五条，记这五条：

1. **不要二选一，按信息类型拆开。** 数据集声明「数据事实」（单位、量纲、基数、字段语义），规则声明「可比性要求」（要比较什么估计量、允许哪些归一策略、容差在哪个空间）。两者由解析器组合，而不是让任一方单独承担全部口径。
2. **默认恒等、永不因缺声明中断。** 老规则没有声明时走 `identity`，原始值直接比较，结果标记 `degraded`，评估链继续。非法的新声明在**导入期**拒绝，不把作者错误拖到运行期。
3. **能改变数值的变换必须显式声明。** 类型推断只能用来「提示」和「识别矛盾」，不能自动授权缩放、换币、除以基数。这样才不是拍脑袋写死系数，也不是让推断悄悄改结果。
4. **原始值和归一值双写，且记录完整 provenance。** 快照同时保存 raw、normalized、策略参数、声明来源、profile 哈希、resolver 版本。报告并排展示两者。
5. **归一解决的是「物理/统计口径」，解决不了「语义不同」。** 语义不可比时明确标 `incomparable` 并排除维度，不要用数学变换制造可比性。跨数据集基准比较（本来就预期不同）应另做参考区间，不塞进一致性判定。

推荐方案是**混合声明模型**：

```text
指标 YAML: comparability contract   （要求）
数据集旁车 YAML: comparability profile（事实）
                    │
                    ▼
            resolver（纯函数、确定性）
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   NormalizationPlan        NormalizationPlan
   (impl_a × orders_a)      (impl_b × orders_b)
        │                       │
        └───────────┬───────────┘
                    ▼
        空间一致性检查 → 归一值比较 → verdict + coverage
                    ▼
        快照：raw_values + normalized_values + plans
```

---

## 1. 先分清两类比较

这是整个设计的前置判断，混淆会导致方案方向错误。

### 1.1 实现一致性比较（caliber 当前能力）

同一指标的两个实现，分别跑在两套数据源上，**业务上期望结果一致**。例如 `active_users` 的 `impl_a` 和 `impl_b`。当前 `compare_impl_results` 的隐含假设就是「所有实现语义等价」。

口径对齐层服务于这一类：当两套数据源的物理单位、基数、字段可选性不同，但业务语义对应时，先把值映射到同一个规范空间，再比较。

### 1.2 跨数据集基准比较（另一种需求）

同一个规则跑在两个真实不同的群体上，**预期结果本来就不同**。例如两个市场的客单价、两个渠道的转化率。此时「A 是 0.12、B 是 0.35」不是不一致，而是基线差异。

这类需求不应该用「归一后判 consistent」来表达。它需要的是参考区间、基线差值、置信区间，属于另一条产品线。**建议 v1 明确不做**，否则口径对齐会退化成「把数字调到相等」的工具。

### 1.3 对 0.12 / 0.35 这个例子的判断

0.12 和 0.35 本身不足以决定归一策略，必须先回答四个问题：

| 问题 | 若答案是 | 处理 |
| --- | --- | --- |
| 两个值都是比率，还是一个是计数？ | 一个是计数 | 除以各自分母（基数） |
| 单位/量纲是否不同（分/元、毫秒/秒）？ | 是 | 显式单位换算 |
| 分母口径是否不同（全量行 vs 合格行）？ | 是 | 按规则要求的基数重算比率 |
| 字段语义是否真的对应？ | 否 | `incomparable`，不做数值变换 |

这里有一个容易忽略的统计前提：样本量决定「多小的差距才值得判」。若两侧各只有 40 个样本，两个比率之差的 95% 区间半宽约 ±0.18，小于这个量的差距都无法与抽样噪声区分；两侧各 200 个样本时收窄到约 ±0.08，0.23 的差距才稳定可判。所以同一对 0.12 / 0.35，在不同样本基数下可能分别得到「无法区分」「低置信地不一致」「明确不一致」三种结论。正确动作不是「归一成同一个数」，而是让样本量进入判定：样本不足时排除该维度，或合并粒度（§7.2）；完整算例见 §4.5。

另一个容易被误用的变换：logit。对 0.12 和 0.35 做 logit 不会改变两者是否相等（单调变换保序），只会改变「多接近算接近」的容差尺度。它可用于效果量分析，但不是口径对齐。**非线性变换不能修复语义不可比。**

---

## 2. 声明应该跟着谁走

### 2.1 为什么这不是二选一

「口径」包含两类信息，它们的归属不同：

| 信息 | 例子 | 天然归属 | 变更频率 |
| --- | --- | --- | --- |
| 数据事实 | 金额单位是分、货币是 CNY、`user_id` 是实体 ID、合格行过滤条件、行数 | 数据集 | 数据源升级时 |
| 比较要求 | 要比较的是比率还是计数、规范单位是什么、允许哪些变换、样本精度门槛、容差空间 | 规则 | 口径评审时 |
| 观测值 | 每个维度的分母、样本量 | 运行时 | 每次运行 |

规则不可能知道数据集实际用什么单位；数据集也不可能知道某条规则要比较的估计量是「每千人比率」还是「原始计数」。所以混合不是折中，而是信息本身的边界。

### 2.2 三种方案的维护成本

设 N 个数据集、M 条规则：

| 方案 | 新增数据集 | 新增规则 | 某数据集单位变更 | 某规则口径变更 | 主要问题 |
| --- | --- | --- | --- | --- | --- |
| 只跟规则走 | 可能改 M 条规则（每条要加该数据源的缩放/条件） | O(1) | 改 M 条规则 | O(1) | 数据事实散落在规则里，规则文件出现 `if source == b then ×0.01`，正是想避免的硬编码 |
| 只跟数据集走 | O(1) 旁车 | 若需要新基数语义，可能改 N 个数据集 | O(1) | 可能改 N 个数据集 | 数据集被迫知道「规则要什么」，只能表达单位换算，无法表达估计量选择；也容易让数据集声明任意变换来「调平」结果 |
| **混合** | **O(1) 旁车** | **O(1) 合同** | **O(1)** | **O(1)** | 需要一次性的 schema 和 resolver 设计 |

混合方案的关键收益不是「少写几个字段」，而是**变更局部化**：换数据源只改该数据集的旁车；改口径只改规则合同；两者都不需要触碰对方。并且规则合同里写的是「要求」，不是「A 数据集要乘 0.01」，语义没有被数据集细节绑死。

### 2.3 推荐

- 规则侧：`comparability` 合同，跟指标版本一起进入 `metric_versions`，可 diff、可审计。
- 数据集侧：`<key>.comparability.yaml` 旁车，和 CSV 同目录，随代码库评审；不把元数据塞进 CSV，也不改现有 CSV 加载契约。
- 运行侧：只允许在数据集**没有**旁车时用覆盖做迁移/试验；有旁车时覆盖不静默生效，冲突必须记录（见 §7.4）。
- 不建议引入「每个 impl 一份自由变换」的声明。impl 级只允许引用 profile，不允许写任意因子，否则比较结果可被单个实现随意调平。

---

## 3. 数据模型

### 3.1 规则侧：可比性合同

加在指标 YAML 顶层，可选；缺失即老规则。

```yaml
comparability:
  target:
    kind: money                  # money | ratio | per_base | duration | count | raw
    canonical_unit: CNY          # 归一后进入的规范单位
    per: 1                       # 归一为「每 N 个基数单位」；非 per_base 时忽略
    # base: eligible_rows        # ratio/per_base/count 且需要样本检查时必填
  allowed_strategies: [identity, unit_scale]
  small_sample: exclude          # exclude | shrink | raw_with_flag
  tolerance:
    abs: 0.01
    rel: 0.0
  rationale: 两套订单数据的金额单位不同，统一到元后比较客单价。
```

比率类合同的最小形式：

```yaml
comparability:
  target:
    kind: ratio
    canonical_unit: ratio
    base: eligible_rows          # 引用数据集 profile 中 base_definitions 的键名
  allowed_strategies: [identity, rate_per_base]
  precision:
    metric: proportion
    max_half_width: 0.05
    confidence: 0.95
  small_sample: exclude
  rationale: 分母口径必须一致；日样本不足 385 时该维度不参与判定。
```

> 注意：指标现有的 `unit: 元` 是**展示单位**，只在报告和页面上出现，不参与计算。不要复用它做归一，否则「把单位改成元」就会改变判定语义。归一只认 `comparability.target.canonical_unit`。两者可以不一致（例如展示用「万元」、归一用「元」），报告里分别标注。

字段约束（导入期校验）：

- `kind`、`small_sample` 是枚举；`allowed_strategies` 必须是策略目录的子集。
- `kind` 为 `ratio` / `per_base` / `count` 时：若合同要做样本量检查（默认启用），`base` 必填；若显式提供 `n_min` 且不需要按基数归一，`base` 可省。`raw` 与 `money` 不要求 `base`。
- 使用 `rate_per_base` 或 `shrink` 时，必须提供 `precision` 或 `n_min`，否则拒绝导入。这样可以杜绝「没有精度策略却悄悄除以基数」。
- `precision.metric: proportion` 时 `max_half_width` 是**绝对**半宽（0.05 = ±5 个百分点）；`metric: mean` 时是**相对**半宽（0.05 = ±5% 均值，公式里乘 `mean`）。同一个键在不同 metric 下量纲不同，schema 注释和报告都要写明。若团队希望统一用绝对单位，建议把 mean 的字段改名为 `max_relative_half_width`，避免误读。
- 非线性策略（如果未来开放）必须显式提供 `tolerance`，并在报告中标注 `tolerance_space`。
- `rationale` 建议必填。它迫使声明者写出判断依据，也是审计时最有价值的一行。

v1 不保留 `on_missing_declaration` 字段。缺声明只有一种降级动作：`identity` + `degraded`，写死在 resolver 里，不允许配置成报错或跳过。如果未来需要严格模式，由运行级 `mode=strict` 承担（跑完后判失败），而不是让单个指标合同声明自己「可以中断整条链」。

### 3.2 数据集侧：事实档案

新增 `<dataset_key>.comparability.yaml`，与 CSV 同目录。`DatasetCatalog` 目前只 glob `*.csv`，不会误加载。

```yaml
key: orders_a
profile_version: 1
declared_by: commerce-analytics
rationale: 金额以分存储，订单日期为自然日。
columns:
  amount:
    semantic: money
    unit: cent
    scale_to_canonical: "0.01"   # 1 cent = 0.01 CNY，字符串保留声明精度
    currency: CNY
    tags: [required_field, non_negative]
  user_id:
    semantic: entity_id
  order_date:
    semantic: date
    grain: day
  status:
    semantic: enum
    values: [paid, unpaid]
base_definitions:
  eligible_rows:
    kind: row_count
    filter:
      status: {in: [paid, unpaid]}
  active_users:
    kind: distinct_count
    column: user_id
    filter:
      status: paid
```

约束：

- `base_definitions` 只允许声明式聚合（`row_count`、`distinct_count`、`count_non_null`、`sum`），可选白名单过滤；**不允许写 SQL 文本**。加载器把它编译成参数化查询，延续项目的只读与路径白名单约束。
- 旁车里的列名必须在 CSV 中存在；未声明的列按 `unknown` 处理。
- `scale_to_canonical` 用字符串保存原始声明，快照中同时记录解析后的浮点值，避免精度审计歧义。
- 旁车语法错误时：**数据仍可加载**，profile 置为无效，运行继续，审计记录 `profile_invalid`。不要把「元数据坏了」升级成「数据集不可用」。

### 3.3 运行时：解析计划

`resolver` 是纯函数，输入合同、profile、推断结果、观测基数，输出每个 `(impl, dataset)` 的计划：

```json
{
  "impl_id": "impl_a",
  "dataset": "orders_a",
  "space": {"canonical_unit": "CNY", "base": null, "per": 1},
  "strategy": "unit_scale",
  "params": {"factor": 0.01, "from": "cent", "to": "CNY"},
  "source": "declared_profile",
  "status": "aligned",
  "reason_codes": [],
  "contract_hash": "sha256:...",
  "profile_hash": "sha256:...",
  "resolver_version": "1.0"
}
```

`source` 取值：`declared_profile`、`declared_override`、`inferred_hint`（只用于恒等或提示）、`default_identity`。

**注意区分两个层级的状态**，否则实现时容易把「这条 impl 的声明不全」和「这个维度不能比」混为一谈：

- **计划状态（plan.status）**是 `(impl, dataset)` 级别的，说明这条实现的数据能否被解释：

| plan.status | 含义 | 后果 |
| --- | --- | --- |
| `aligned` | 显式声明且满足合同 | 该 impl 的所有维度进入候选比较 |
| `degraded` | 缺声明或推断不足，走恒等 | 该 impl 仍参与，但整体结论降级 |
| `incomparable` | 数据集层面就无法建立空间（如币种不可换算） | 该 impl 的所有维度排除 |
| `not_recorded` | 老快照没有该字段 | 仅展示兼容 |

- **维度状态（dimension.status）**是 `(dimension, 全部 impl)` 级别的，说明某个维度能否真正比较。取值 `ok`、`space_mismatch`、`base_missing`、`base_zero`、`insufficient_sample`、`semantic_mismatch`、`normalizer_error`。优先级从高到低：`space_mismatch` > `base_missing` / `base_zero` > `insufficient_sample` > `semantic_mismatch` > `normalizer_error` > `ok`。只要任一 impl 在该维度上不能进入共同空间或达不到样本门槛，该维度就不参与 verdict。

把两者分开后，报告可以准确地说「A、B 的声明都完整（plan 全 aligned），但 2026-02-01 这天 B 的样本不足（dimension 排除）」，而不是笼统地给整个指标贴 `degraded`。

### 3.4 比较结果

`ComparisonResult` 增加一层正交的 `comparability` 信息，不要把它塞进 verdict：

```json
{
  "verdict": "consistent",
  "coverage": "partial",
  "comparability": {
    "status": "incomplete",
    "reason_codes": ["missing_profile:orders_b", "sample_below_threshold:2026-02-01"],
    "excluded_dimensions": 3,
    "dimension_status": {
      "2026-02-01": "insufficient_sample",
      "2026-02-02": "ok"
    },
    "plans": [{ "...": "见上" }]
  }
}
```

`comparability.status` 是给报告和 CI 用的**聚合等级**，取值与 §7.6 的四档一一对应：

| status | 条件 | 与 coverage 的关系 |
| --- | --- | --- |
| `aligned` | 所有 plan 为 `aligned`，无维度排除 | `complete` |
| `degraded` | 无维度排除，但至少一个 plan 为 `degraded`（恒等降级、容差默认） | `complete`，结论标「参考」 |
| `incomplete` | 至少一个维度被排除（空间/基数/样本/语义/计算异常） | `partial` 或 `none` |
| `invalid` | 合同或 profile 无效 | 按实际可算维度，通常 `partial`/`none` |

聚合规则：`invalid` > `incomplete` > `degraded` > `aligned`。`excluded_dimensions` 包含样本不足、基数缺失、空间不一致等所有被排除的维度；`reason_codes` 按维度聚合去重，并带上 dimension 名，方便定位。

`verdict` 仍表达「可比维度上的数值关系」；`coverage` 表达「这些维度覆盖了多少」。两者分开，报告读者才不会把「排除了 90% 维度后的 consistent」误读成整体一致：

- `coverage = complete`：所有维度都参与。
- `coverage = partial`：部分维度因样本不足/不可比被排除。
- `coverage = none`：没有可比维度，`verdict = undetermined`。

`undetermined` 是建议新增的第四种 verdict，仅在全部维度被排除时出现。它和 `inconsistent` 的区别很重要：没有可比数据不等于数据有问题。老运行不会产生它。

---

## 4. 归一策略目录与自适应选择

### 4.1 策略目录（v1 建议）

| 策略 | 变换 | 需要的声明 | 适用 |
| --- | --- | --- | --- |
| `identity` | `x' = x` | 无 | 默认；已同空间 |
| `unit_scale` | `x' = x × f + o` | 单位、`scale_to_canonical`、规范单位 | 分→元、毫秒→秒、件→千件 |
| `rate_per_base` | `x' = x / base × per` | 基数定义、逐维基数、精度策略 | 空值计数→空值率、订单数→每千单 |
| `currency_convert` | `x' = x × fx` | 源/目标币种、离线汇率、`as_of` | 跨币种金额；**不联网取汇率** |
| `shrink` | 经验贝叶斯收缩 | 先验、强度参数、精度策略 | 小样本，默认关闭 |
| `standardize` | 按标准群体加权 | 标准群体权重 | 未来；人群结构不同时的率标准化 |
| 非线性（`logit` 等） | 单调变换 | 显式容差空间 | 未来、opt-in；不改变相等性 |

v1 只建议落地 `identity`、`unit_scale`、`rate_per_base`。`currency_convert` 需要汇率来源策略（离线、手工、带日期），可以放到第二阶段。`standardize` 和 `logit` 放未来。

### 4.2 自适应选择规则

「自适应」应该体现在**根据事实选择已声明的策略与参数**，而不是让启发式算法自行决定估计量。判定表：

| 合同目标 | 数据集事实 | 选择 | 结果 |
| --- | --- | --- | --- |
| `raw` | 两侧单位声明一致，或都未声明单位 | `identity` | space = `raw:<unit>`（都未声明则 `raw:unknown`） |
| `raw` | 两侧单位声明不同 | 无可用策略 | plan `incomparable`，reason `unit_mismatch_raw` |
| `ratio` | 值已是比率，字段 tags 匹配 | `identity` | space = `ratio` |
| `ratio` / `count` | 值是计数，且逐维基数可用 | `rate_per_base` | space = `ratio` / `per_base:<base>` |
| `per_base` | 基数可用 | `rate_per_base` | space = `per_base:<base>` |
| `money` | 单位可换算，币种一致 | `unit_scale` | space = 规范币种 |
| `money` | 币种不同且有离线汇率 | `currency_convert` | space = 规范币种 |
| `money` | 币种不同且无汇率 | 无可用策略 | plan `incomparable`，reason `currency_unconvertible` |
| `duration` | 单位可换算 | `unit_scale` | space = 规范单位 |
| 任意 | 字段 `semantic = unknown` | `identity` | plan `degraded`，reason `type_unknown` |
| 任意 | tags 与合同要求不匹配 | 无可用策略 | plan `incomparable`，reason `semantic_mismatch` |
| 任意 | 逐维基数缺失 / 为 0 | 若合同要求基数 | dimension `base_missing` / `base_zero` |
| 任意 | 变换不在 `allowed_strategies` 内 | 不应用该变换 | plan `incomparable`，reason `strategy_not_allowed`；见 §7.4 |

三条硬规则：

1. **推断只能用于恒等或提示。** 推断出「这一列像金额」不能自动乘 0.01；必须有人在旁车里声明 `unit: cent`。
2. **变换必须可逆解释。** 每个变换都要能回答「原始值是什么、为什么这样映射、映射到哪个空间」。非线性变换必须额外声明容差空间。
3. **空间必须一致才能比较。** 见 §4.3。

### 4.3 空间一致性检查

每个计划带一个 `space` 标识：`canonical_unit + base + per`。

- 所有参与比较的 impl 必须在同一个 space，才允许比。
- 两个都是恒等、且都未声明单位：space 记为 `raw:unknown`。这是唯一允许的「共同未知空间」，仅用于老规则兼容；报告标 `degraded`。
- 一个已声明单位、另一个未知：不能证明同空间，标 plan `incomparable` + dimension `space_mismatch`。宁可排除，也不要产生假的 `consistent`。
- 一个已换算、另一个仍是原单位：space 不同，排除。
- 合同声明 `kind: ratio` 但两侧都缺少 `semantic: ratio` 标签：**仍走 `identity` + `degraded/type_unknown`**，不根据值域推断为比率。推断可以看到「值在 [0,1]」，但无法区分比率、概率、相关系数，无法排除百分比已乘 100 的情况；因此它只能作为 UI 的声明建议，不能作为空间判定依据。要消除 `degraded`，由人补旁车。
- 两侧都声明了同一个 `unit`，但 `scale_to_canonical` 不同（例如都写 `unit: cent`，一边 `"0.01"`、一边 `"1"`）：这是声明自相矛盾，按 `profile_invalid` 处理并排除该维度，不要挑一个「看起来更合理」的用。

这是防止「只给 B 配了声明、A 没配，结果拿 B 的归一值和 A 的原值比」的关键闸门。

### 4.4 逐维基数从哪里来

小样本和 `rate_per_base` 都要求**逐维度的基数**，而不是整个数据集的行数。比如按日算空值率，每天的合格行数不同。

建议扩展 SQL 结果契约：

- 现在：1 列（数值，维度 `__all__`）或 2 列（维度, 数值）。
- 增加可选第 3 列：`(维度, 数值, 基数)`。仅当合同要求 `base` 时使用；基数为该维度的分母/样本量。
- 未提供第 3 列但合同要求基数：该维度 `base_missing`，不猜、不拿全表行数代替。
- `__all__` 单行指标可以从数据集 profile 的 `row_count` / `distinct_count` 获取基数；有维度时优先用第 3 列。
- 基数必须 `> 0`；为 0 时该维度 `base_zero`。

这比在数据集侧做「按维度 join 基数」简单得多，也避免维度截断（day/week/month）与过滤条件错位。代价是 SQL 契约从 1–2 列扩到 1–3 列，需要同步改 `evaluate_sql` 的列数校验和测试。

### 4.5 完整算例：0.12 vs 0.35

假设规则是「空值率」，两套数据集都声明了旁车，但事实不同：

| | 数据集 A | 数据集 B |
| --- | --- | --- |
| 该维度总行数 | 10,000 | 1,200 |
| 合格行（进入分母） | 8,000 | 200 |
| 空值行 | 960 | 70 |
| 原始比率 | 0.12 | 0.35 |
| 合同要求 | `kind: ratio`，`base: eligible_rows`，`h=0.05` | 同左 |

解析过程：

1. A 的旁车声明合格行过滤条件，基数为 8,000；B 为 200。
2. 两侧都是比率，策略 `identity`，空间 `ratio`，无需缩放。
3. 样本量检查：`p=0.5` 最坏情况下 `h=0.05` 需 `n_req≈385`。A 通过；B 的 200 不足，按默认策略排除该维度，标 `sample_below_threshold`。
4. 若团队把合同改成 `h=0.10`（`n_req≈97`），B 通过，两侧归一值仍是 0.12 / 0.35，判定 `inconsistent`，差异 0.23。但注意：B 只有 200 个样本，这个差异的 95% 区间半宽约 ±0.08，所以结论是「可判定为不一致」，而不是「差异精确等于 0.23」。
5. 若两侧样本再降到各 40，即使 `h=0.10` 也不满足，默认两个维度都被排除；强行比较没有统计意义。

如果 0.35 其实来自「空值数 / 合格行数」（70/200），而合同要求的分母是「总行数」（70/1200 ≈ 0.058），那才是口径问题：B 的 SQL 或旁车基数的过滤条件配错了，应修正基数定义，而不是加一个 0.35→0.12 的缩放系数。**先确认分母，再谈缩放；多数「量级差三倍」的案例其实是分母不同，不是单位不同。**

这个算例也说明：归一化不能替代业务判断。resolver 能做的是把分母事实显式化、把样本不足标记出来，而不是替团队决定「0.12 和 0.35 算不算一致」。

---

## 5. 容差放在哪个空间

归一后，容差必须在**比较空间**里解释，否则会得到荒谬的结论。

- `unit_scale` / `rate_per_base` / `currency_convert`：空间仍是物理可解释单位，沿用 `abs + rel × max`。报告写明 `tolerance_space`，例如 `CNY`、`ratio`、`count/1000 eligible_rows`。
- 非线性变换：必须在合同里单独声明容差；不允许沿用默认 `1e-6`。导入期校验。
- 运行级 `--abs-tol` 仍可覆盖，但覆盖的是归一空间的容差，不是原始值的容差。报告要同时显示原始值和归一值，避免读者误解。
- 如果合同没有声明容差，且策略不是 `identity`，建议默认用配置值但在报告中标 `tolerance_defaulted`，提醒评审。

---

## 6. 存储、审计与报告

### 6.1 快照

`snapshots.result_json` 是自由 JSON，**不需要数据库迁移**。建议引入 `result_schema_version: 2`：

```json
{
  "result_schema_version": 2,
  "metric": { "...": "..." },
  "implementations": [
    {
      "impl_id": "impl_a",
      "source": "orders_a",
      "raw_values": {"2026-02-01": 10000.0},
      "normalized_values": {"2026-02-01": 100.0},
      "normalization": {"strategy": "unit_scale", "params": {"factor": 0.01}, "source": "declared_profile", "status": "aligned"}
    }
  ],
  "dimensions": ["2026-02-01"],
  "diffs": [
    {
      "dimension": "2026-02-01",
      "kind": "value_mismatch",
      "raw_values": {"impl_a": 10000.0, "impl_b": 10040.0},
      "normalized_values": {"impl_a": 100.0, "impl_b": 100.4},
      "space": {"canonical_unit": "CNY"},
      "sample": {"base": 120, "required_n": 97, "status": "ok"},
      "status": "ok"
    }
  ],
  "comparability": { "...": "..." }
}
```

兼容策略：

- 老快照没有 `result_schema_version` 和 `comparability`：解码为 `not_recorded`，报告正常渲染，不报警。
- 老字段 `values` 保留为**归一值**（比较基准）；`raw_values` 新增。这样现有 comparator/report 的消费方不会突然拿不到值。
- 如果担心语义混淆，可以在 v2 报告中只展示 `raw_values` 和 `normalized_values`，`values` 仅作为内部兼容字段。
- 快照存完整计划和 profile 哈希，不依赖旁车文件后续不变。审计时能复原当时的选择。

### 6.2 审计日志

不要每个 impl 一条，噪音太大。建议：

- `comparability.resolved`：每次运行一条，记录各 metric 的策略计数、降级原因、排除维度数。
- `comparability.conflict`：仅在声明冲突、profile 无效、空间不一致时记录，状态 `warn`/`error`。
- 导入期合同错误走现有 `ValidationError` 和 `metric.import` 失败审计。
- 运行期 resolver 内部异常走 `record_error`，但只影响该维度，不失败整条运行。

### 6.3 报告

JSON 是机器可读主格式；Markdown/HTML 增加一个「口径对齐」区块：

- 合同摘要：目标估计量、规范单位、基数、精度策略。
- 计划表：`impl | dataset | strategy | params | source | status | reasons`。
- 差异表：`dimension | raw A | raw B | normalized A | normalized B | space | base/n_req | status`。
- 排除维度清单及原因。
- 覆盖度 badge：`complete / partial / none`，与 verdict 并列展示。
- 降级时在报告顶部加一条明确警告：`本报告包含推断/默认恒等的结果，跨数据集结论仅供参考`。

---

## 7. 降级、异常与冲突

### 7.1 总原则

- **运行期缺声明 → 降级，不抛错。**
- **导入期显式声明非法 → 拒绝该版本导入，旧版本不受影响。**
- **运行期单维度异常 → 排除该维度，其他维度继续。**
- **不要静默。** 每个降级都有稳定 reason code，进快照、进报告、进审计。

### 7.2 样本基数过小

#### 判定门槛

不要写死「n < 30」。让合同表达精度目标，resolver 逐维计算：

- 比率：`n_req = ceil(z² × p(1-p) / h²)`，`p` 用观测值，最坏情况 `p=0.5`；`z` 由 `confidence` 决定。
- 均值/人均：`n_req = ceil((z × s / (h × mean))²)`；`s` 缺失时要求显式 `n_min`。
- 合同也可以直接给 `n_min`，用于业务上已有硬门槛的场景。

例：`h=0.05, confidence=0.95` 时，最坏情况 `n_req ≈ 385`；`h=0.10` 时约 97。按日数据经常达不到 385，这会强制团队面对「日粒度下这个率到底能不能比」的问题，而不是让一个小分母制造假差异。团队可以调 `h` 或改用周/月粒度，这是显式的业务取舍。

#### 低于门槛时

| 策略 | 行为 | 评价 |
| --- | --- | --- |
| `exclude`（默认） | 该维度不参与 verdict，计入 `excluded_dimensions`，状态 `insufficient_sample` | 推荐。不制造结论，也不中断 |
| `shrink` | 向先验收缩；记录先验、强度、公式 | opt-in。会改变估计量，可能掩盖真实差异，必须显式声明 |
| `raw_with_flag` | 仍用原值比较，但标 `low_sample` | 只建议迁移期使用 |

如果所有维度都被排除：`verdict = undetermined`，`coverage = none`。不要退化成 `consistent`，也不要报 `inconsistent`。

#### 基数缺失或为 0

`base_missing` 或 `base_zero`，该维度排除。不要用全表行数顶替按日分母。

### 7.3 字段类型推断失败

现状：`dataset_loader.infer_type` 对整列做全量匹配，混合类型落为 `str`；空列也是 `str`。

推荐策略：**保守恒等，不跳过评估**。

- 推断失败或混合类型 → `semantic: unknown`。
- 合同需要基数/单位，但事实未知 → 不能变换，走 `identity`，`degraded/type_unknown`。
- 不要把整个指标跳过；原始值仍然有比较价值，只是结论降级。
- 推断可以作为 UI 提示（「这一列看起来像金额，考虑声明 `semantic: money`」），但不能写回结果。
- 如果显式声明与推断明显矛盾（例如声明 `money` 但列全是文本），记 `declaration_conflict` 警告；仍以显式声明为准，因为它可能包含推断看不到的信息（前导零、分单位整数等）。

### 7.4 声明冲突时以谁为准

冲突分五类，不能用一个「优先级」概括：

| 冲突类型 | 处理 | 理由 |
| --- | --- | --- |
| 规则要求 vs 数据集事实 | 能转换则转换；不能转换则 `incomparable` | 这不是谁赢，而是可满足性检查。规范单位是要求，实际单位是事实 |
| 显式旁车 vs 类型推断 | 旁车优先；推断不得覆盖 | 显式声明包含业务知识 |
| 旁车 vs 运行级覆盖 | 有旁车时旁车优先；覆盖仅在旁车缺失时生效；冲突记录 | 旁车是评审过的持久事实，CLI 覆盖是临时试验，不应静默改结果 |
| 两份旁车/重复 key | profile 置为无效，降级 + 审计 error | 配置歧义不猜；但数据仍可加载，运行不中断 |
| 合同要求 vs 运行覆盖 | 合同是硬约束；覆盖只能从 `allowed_strategies` 里选，不能改规范单位 | 防止一次运行绕过口径评审 |
| 同一数据集被多条规则使用 | 不冲突，各取所需事实 | 事实是中立的 |

补充：如果未来确实需要「运维纠偏」旁车，建议引入显式的 `authoritative: true` 或 `--force-profile`，并把它写进快照和审计，而不是让普通覆盖默默生效。

### 7.5 失败兜底矩阵

| 场景 | 检测点 | 默认行为 | reason code | 链路影响 |
| --- | --- | --- | --- | --- |
| 老规则无合同 | parser | identity，原值比较 | `missing_contract` | 继续，结果 `degraded` |
| 数据集无旁车 | resolver | 只用恒等；推断不授权变换 | `missing_profile` | 继续，`degraded` |
| 旁车语法/字段错误 | profile loader | 忽略该 profile，审计 | `profile_invalid` | 数据可用，继续 |
| 字段类型未知/混合 | resolver | identity，不猜 | `type_unknown` | 继续，`degraded` |
| 合同要求基数但缺失 | resolver | 该维度排除 | `base_missing` | 继续 |
| 基数为 0 | resolver | 该维度排除 | `base_zero` | 继续 |
| 样本低于门槛 | resolver | 默认排除维度 | `sample_below_threshold` | 继续 |
| 空间不一致 | comparator | 该维度排除 | `space_mismatch` | 继续 |
| 归一化计算异常（溢出等） | resolver try/except | 该维度排除，审计 | `normalizer_error` | 继续 |
| 合同非法 | 导入校验 | 拒绝新版本 | `validation_error` | 旧版本仍可运行 |
| 单指标求值异常 | run loop | 记录失败快照，继续其他指标 | `evaluation_error` | 建议改为 `completed_with_errors` |
| 老快照无新字段 | snapshot decode | `not_recorded` | — | 报告可读 |

最后两行需要额外说明：

- 当前 `perform_run` 在任一指标抛异常时会终止整个运行。口径对齐层的 resolver 本身设计为不抛错，所以「缺声明中断」的问题已经解决。但如果要做到真正的「一条评估链不中断」，建议把 run loop 改成逐指标捕获异常：失败的指标写一条 `verdict = undetermined` + `evaluation_status = failed` 的快照，运行状态 `completed_with_errors`，其余指标照常。这是独立于本方案的一处加固，建议一并排期。
- `undetermined` 和 `evaluation_status = failed` 语义不同：前者是「有数据但没有可比维度」，后者是「求值失败」。报告要分开显示。

### 7.6 降级等级

给 UI 和 CI 一个简单的四档：

| 等级 | 条件 | 含义 |
| --- | --- | --- |
| L0 `aligned` | 所有 impl 显式声明、同空间、样本充足 | 结论可信 |
| L1 `degraded` | 恒等降级、部分推断、容差默认，但无维度排除 | 结论仅供参考 |
| L2 `incomplete` | 有维度因空间/基数/样本/语义被排除 | 覆盖不完整，看 coverage |
| L3 `invalid` | 合同导入失败 / profile 无效 | 需要修复声明；运行可能已降级 |

plan 级还有一个 `incomparable`，它是 L2 的成因之一：某条 impl 在数据集层面就无法进入共同空间。为了不让读者混淆，报告只展示 L0–L3 四档聚合等级，plan 级的 `incomparable` 作为 reason code 出现在计划明细里。

**为什么 `invalid` 要单独占一档。** 合同在导入期已经被拒绝，所以 `invalid` 不会来自新导入的指标；它来自两种情况：一是数据集 profile 在运行期才被发现无效（例如文件损坏、重复 key），二是合同校验规则在未来版本收紧后，历史版本重新运行时不再满足。前者说明「声明没生效」，后者说明「口径评审结论已经过时」。这两种都需要人处理，不能和「样本不够」混在同一档。`strict` 模式应把 `invalid` 视为失败，`incomplete` 视为警告。

`strict` 模式可以让 L1/L2 的运行退出码非 0，但**应在所有指标跑完后**再失败，而不是中途中断。

---

## 8. 配置、兼容与上线

### 8.1 运行模式

`comparability.mode`：

- `off`：完全走旧逻辑，忽略所有声明。用于回归对照。
- `auto`（默认）：解析并应用；缺声明降级；永不因口径问题中断。
- `strict`：解析并应用；跑完全部指标后，若存在 `degraded` / `incomplete` / `invalid` 或 `undetermined`，运行状态 `completed_with_warnings`，CLI 退出码非 0。`invalid` 建议直接判失败，`incomplete` 判警告。适合 CI。

默认 `auto` 且没有声明时，所有计划是 `identity`，`normalized == raw`，verdict 与今天完全一致。**默认上线不改变任何现有结果**，这是可以安全合入的前提。

### 8.2 向后兼容清单

- `MetricDefinition.from_dict`：`comparability` 缺失 → `None`。
- `dsl_parser.TOP_FIELDS`：加入 `comparability`；未知字段仍然报错，防止拼写错误静默生效。
- `dsl_validator`：合同校验；缺声明不报错。
- `dataset_loader`：旁车可选；`DatasetMeta` 增加 profile 字段；无旁车不影响加载。
- `ImplEvaluation`：增加 `raw_values` / `normalized_values`；保留 `values` 作为归一值。
- `ComparisonResult`：增加 `coverage`、`comparability`；老字段不动。
- `snapshot`：JSON 加字段，无 DDL 迁移；`result_schema_version: 2`。
- `reporter`：`report_version` 升到 1.1；老快照按 `not_recorded` 渲染。
- `VERDICTS`：增加 `undetermined`；所有硬编码三态计数的位置（run summary、reporter）需要同步。
- CLI/API：新增可选参数；不传时行为不变。

### 8.3 分阶段落地

| 阶段 | 内容 | 行为变化 |
| --- | --- | --- |
| P0 | 管线传递 raw/normalized（全部恒等）、快照 schema v2、报告并排展示 | 无判定变化 |
| P1 | 合同 + 旁车解析、resolver、`unit_scale` / `rate_per_base`、空间检查 | 仅在存在显式声明时变化 |
| P2 | 小样本、冲突处理、`strict` 模式、逐指标异常隔离 | 仅影响声明覆盖到的指标 |
| P3 | 离线汇率、标准化、非线性策略 | opt-in |

每一阶段都应保留「无声明 fixture 的 verdict 与基线完全一致」的回归测试。

---

## 9. 验收与测试矩阵

设计验收看的是「决策是否完整、可执行」，不是代码行数。建议按下列矩阵评审：

| # | 场景 | 期望 | 关注点 |
| --- | --- | --- | --- |
| 1 | 现有三个 fixture，无任何声明 | verdict 与今天完全一致；`comparability.status = degraded`；normalized == raw | 默认不改变行为 |
| 2 | 金额单位不同（分 vs 元），两侧都有旁车 | `unit_scale`；raw 不同、normalized 相同；报告并排 | 单位换算 |
| 3 | 空值计数 vs 空值率，计数侧提供第 3 列基数 | `rate_per_base`；两侧进入 ratio 空间后一致 | 逐维基数 |
| 4 | 合同要求基数，但 SQL 未返回第 3 列 | 维度 `base_missing`，运行继续 | 不猜基数 |
| 5 | 日粒度基数 40、`h=0.05` | 维度 `insufficient_sample` 被排除，`coverage=partial`；全部排除时 `undetermined` | 小样本 |
| 6 | 基数 0 | 该维度排除，不抛除零异常 | 边界 |
| 7 | 旁车声明 `money` 但列被推断为 `str` | 以显式声明为准 + 冲突警告；或按声明不可满足处理 | 推断不覆盖显式 |
| 8 | 数据集无旁车，列类型混合 | identity + `degraded/type_unknown`；不跳过指标 | 推断失败兜底 |
| 9 | A 声明 CNY、B 声明 USD 且无汇率 | 维度 `space_mismatch`（原因 `currency_unconvertible`） | 不猜汇率 |
| 10 | 只给 B 配了单位、A 未声明 | `space_mismatch`，不产生假 consistent | 空间闸门 |
| 11 | 旁车说 cent，运行覆盖说 yuan | 旁车优先 + `declaration_conflict` 审计 | 冲突优先级 |
| 12 | 合同 `allowed_strategies` 不含实际需要的策略 | 导入期或运行期拒绝，给出字段级原因 | 合同是硬约束 |
| 13 | 合同非法（未知 strategy、负 n_min） | `ValidationError`，旧版本仍可运行 | 导入期失败 |
| 14 | 老快照 JSON 无新字段 | 报告正常渲染，状态 `not_recorded` | 向后兼容 |
| 15 | 一个指标 SQL 失败、其余正常 | 其余指标出快照；失败指标 `undetermined`；运行 `completed_with_errors` | 链不中断 |
| 16 | `mode=off` | 忽略声明，verdict 回到基线 | 回退开关 |

另外建议加一条「反作弊」验收：任何未在合同 `allowed_strategies` 中的变换都不得生效；任何推断都不得单独改变数值。这是整个方案可信度的底线。

---

## 10. 风险与反模式

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| 用归一化掩盖真实差异 | 给每个数据集配一个因子，把所有结果调成一致 | 显式声明 + `rationale` + 报告并排 raw/normalized + 合同白名单 |
| 推断悄悄改结果 | 类型推断出金额就自动缩放 | 推断只提示/恒等；值变换必须显式 |
| 声明散落、无法审计 | 每个人在规则里写 `if source == ...` | 事实归旁车、要求归合同、resolver 统一 |
| 语义不同却强行可比 | 不同字段含义的率被映射到一起 | `semantic` tags + `incomparable` |
| 小样本被平滑掩盖 | `shrink` 把异常拉回均值 | 默认 `exclude`；`shrink` opt-in 并记录先验 |
| 容差空间混淆 | 归一后仍用原始容差，或 logit 空间用 `1e-6` | `tolerance_space` 显式记录，非线性强制声明 |
| 旁车漂移 | 数据源升级后旁车没改 | snapshot 存 profile hash；导入/运行审计；报告显示声明时间 |
| 覆盖度误导 | 排除了大部分维度仍显示 consistent | `verdict` 与 `coverage` 并列；报告警告 |

---

## 11. 待团队决定的问题

1. **`undetermined` 是否可接受？** 全部维度被排除时，新增第四种 verdict 最诚实，但会改变对外契约；备选是保留三态 + `coverage=none`。我推荐前者，并同步升级报告版本。
2. **SQL 契约是否允许第 3 列基数？** 这是逐维小样本和 per-base 归一的前提。备选是每条 impl 配一条单独的基数查询，但复杂度和错位风险更高。我推荐允许第 3 列。
3. **旁车是否要进版本库评审？** 我推荐是；不进数据库版本表，但快照存 hash。若团队更想要 DB 管理，需要额外的 profile 版本表。
4. **跨境/多币种汇率来源？** 项目要求完全离线，只能声明静态汇率 + `as_of`，并接受其时效风险。没有汇率就 `incomparable`，不自动取数。
5. **`strict` 模式是否作为 CI 默认？** 建议先在 CI 用 `strict`、本地用 `auto`，避免本地开发被降级警告打断。
6. **跨数据集基准比较是否单独立项？** 我建议单独立项；不要把它混进一致性 verdict，否则「归一」会变成「调平」。

---

## 12. 一句话总结

把「口径」拆成数据事实和比较要求，用混合声明 + 确定性 resolver 组合；默认恒等降级、显式才变换、空间不一致就不比、小样本就排除、原始值和归一值全留痕。这样既能跨数据集比较，又不会把不可比的东西伪装成可比。
