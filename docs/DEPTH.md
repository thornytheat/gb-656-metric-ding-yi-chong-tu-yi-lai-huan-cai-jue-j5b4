# 深度登记 metricgraph

- 规模：中型（8 个功能模块，80 个文件，115 条测试）
- 核心规则：同名同版本指标先做表达式稳定签名，再按依赖图检测环并裁决生效版本。
- 使用者与工作流：分析治理组在指标发布前复核定义冲突、缺依赖、依赖环与版本重叠；
  迁移后逐个下游确认，确认后旧引用被拒，未迁完的下游仍按别名运行。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 元数据采信 | 采信 DDL 或 JSON 元数据，看逐行诊断与隔离区 | POST /api/schemas/import | `app/modules/m1_intake/handler.py` `app/modules/m1_intake/service.py` `app/modules/m1_intake/reader.py` `app/modules/m1_intake/ddl.py` `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py` |
| M2 类型格 | 查类型格的值域与刻度，或直接比两个类型写法 | GET /api/types | `app/modules/m2_types/handler.py` `app/modules/m2_types/service.py` `app/modules/m2_types/coerce.py` `app/modules/m2_types/range.py` `app/modules/m2_types/lattice.py` | `tests/test_m2_types.py` |
| M3 冲突判定 | 对同名指标做签名并列出定义冲突 | POST /api/metrics/conflicts | `app/modules/m3_compat/handler.py` `app/modules/m3_compat/metric_rules.py` `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py` `tests/test_metric_rules.py` |
| M4 字段映射 | 看旧名到新名的映射、别名 SQL、冲突项 | GET /api/mapping | `app/modules/m4_mapping/handler.py` `app/modules/m4_mapping/service.py` `app/modules/m4_mapping/mapping.py` `app/modules/m4_mapping/sqlalias.py` `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py` |
| M5 影响面 | 看受影响的下游节点、环、断链、单边登记 | GET /api/impact | `app/modules/m5_impact/handler.py` `app/modules/m5_impact/service.py` `app/modules/m5_impact/closure.py` `app/modules/m5_impact/cycle.py` `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py` |
| M6 版本批次 | 跑批次、看历史、验陈旧拒绝与区间重叠 | POST /api/runs | `app/modules/m6_batches/handler.py` `app/modules/m6_batches/service.py` `app/modules/m6_batches/window.py` `app/modules/m6_batches/guard.py` `app/modules/m6_batches/history.py` | `tests/test_m6_batches.py` |
| M7 指标目录 | 导出一份定义、依赖与状态清单 | GET /api/export/catalog.csv | `app/modules/m7_migration/handler.py` `app/modules/m7_migration/service.py` `app/modules/m7_migration/csv_out.py` `app/modules/m7_migration/ledger.py` | `tests/test_m7_migration.py` |
| M8 迁移确认 | 逐下游确认 / 撤回，看实际可用引用与拒绝原因 | GET /api/migration | `app/modules/m8_confirm/handler.py` `app/modules/m8_confirm/service.py` `app/modules/m8_confirm/refs.py` `app/modules/m8_confirm/csv_out.py` | `tests/test_m8_confirm.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 导出件带字节序标记，首列名被污染 | 首列名带 `\ufeff`，按名取值全空 | 探测编码前先剥标记，再按回退链解码 | `app/modules/m1_intake/reader.py` | `tests/test_m1_intake.py::test_trap02_bom_stripped_before_decode` |
| TRAP-05 | 列定义行尾多一个分隔符 | 拆出空列，或者把坏行当正常列 | 顶层分隔符计数，命中即整行进隔离区 | `app/modules/m1_intake/ddl.py` | `tests/test_m1_intake.py::test_trap05_extra_separator_quarantined` |
| TRAP-09 | 字段身份是 2^53+1 这种大整数 | JSON 往返落进 float，末位被改 | 身份一律字符串保形，浮点身份直接拒绝 | `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py::test_trap09_big_integer_field_id_kept_as_string` |
| TRAP-17 | 上游改列名、放宽类型 | 按列名对账，改名算删除加新增 | 只按 field_id 对齐，放宽另算兼容 | `app/modules/m3_compat/judge.py` | `tests/test_m3_compat.py::test_trap17_rename_is_not_delete_plus_add` |
| TRAP-19 | 一个字段同时改名、换类型、改必填 | 结论取决于规则书写顺序 | 显式优先级表，重排输入结论不变 | `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py::test_trap19_rule_order_does_not_change_primary` |
| TRAP-20 | 下游依赖成环、节点指向自己 | 递归爆栈或静默截断 | 三色 DFS，自引用单列一类 | `app/modules/m5_impact/cycle.py` | `tests/test_m5_impact.py::test_trap20_cycle_and_self_loop` |
| TRAP-21 | 上下游只登记一边 | 只查单向，另一半关系漏报 | 双向核验，缺边清单逐条列出 | `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py::test_trap21_one_sided_registration` |
| TRAP-22 | 同一字段被连续改过两次名 | 只认最近一跳，最早的名字解析不到 | 跨版本改名链一路解析到底 | `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py::test_trap22_alias_chain_resolves_multi_hop` |
| TRAP-25 | 两个版本生效区间重叠 | 取到哪版看运气 | 半开区间比较，重叠直接标记警告 | `app/modules/m6_batches/window.py` | `tests/test_m6_batches.py::test_trap25_overlap_flagged_but_adjacent_clean` |
| TRAP-26 | 基于旧版本号提交 | 后提交无条件覆盖新结果 | 版本号乐观锁，基线落后即 409 | `app/modules/m6_batches/guard.py` | `tests/test_m6_batches.py::test_trap26_stale_submission_rejected` |
| TRAP-27 | 已确认下游改回旧名字继续跑 | 只要别名还在就放行，旧引用悄悄回流 | 确认后该下游的旧引用一律 `OLD_REF_BLOCKED`，未确认的仍可用 | `app/modules/m8_confirm/refs.py` | `tests/test_m8_confirm.py::test_confirm_blocks_old_refs_only_for_that_consumer` |
| TRAP-28 | 确认后又跑了新批次或改了登记 | 旧确认仍按已确认放行，约束张冠李戴 | 确认绑定批次号、引用集合摘要与字段解析摘要，任一变化即失效，旧引用恢复兼容 | `app/modules/m8_confirm/service.py` | `tests/test_m8_confirm.py::test_new_run_invalidates_confirmations_but_keeps_record` |
| TRAP-29 | 确认与撤回并发、同一版本号 | 后写覆盖前写，账上两笔都成 | 单事务内比对 `state_version`，落后即 409，状态与事件同回滚 | `app/store.py` `apply_confirmation` | `tests/test_m8_confirm.py::test_concurrent_confirms_one_wins_the_stale_one_is_rejected` |
| TRAP-30 | 写事件失败后重试 | 状态落了、账没记，重试再记一笔 | 同事务写入；带相同的 `request_id` 重试只回放不重复记账 | `app/store.py` `apply_confirmation` | `tests/test_m8_confirm.py::test_write_failure_rolls_back_and_retry_books_once` |

## 对账规则

- 字段账：旧版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 删除
- 字段账：新版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 新增
- 引用账：登记引用 = 可达 + 断链，其中可达 = 直接 + 经别名
- 采信账：列定义行 = 接受字段 + 阻断行
- 页面上四账并列显示，差额不为 0 即判不平

## 已知不覆盖

- SQL 只认 `别名.列名`；无别名的裸列名、子查询、CTE、函数包裹的列都不抽。
- DDL 只解析 `CREATE TABLE` 的列定义，不处理分区、约束、索引与 `ALTER`。
- 类型格只覆盖数值、字符、时间、布尔四族；映射表、数组、结构体一律算 `unknown`，保守判破坏。
- 生效区间重叠只标记不阻断，最终以版本序号为准。
- 迁移确认只约束本系统登记的引用；下游在系统外卖的视图不归这里管。
- 确认状态挂在 `consumer` 名字上，改名会被当成新下游，旧记录留作历史。
- 打印页脚用 `counter(page)`，Chrome 打印预览不渲染该计数。
