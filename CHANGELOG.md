# 更新日志

本文件记录 CasePilot 的所有重要变更。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-11-08

首个正式版本，对应路线图 5 轮迭代的全部功能：中文需求 / OpenAPI 文档 → 按测试方法论生成结构化测试用例 → 导出禅道 Excel / pytest 骨架 / Markdown，并配 4 维评测框架与 LLM-as-a-Judge 用例质量自评。

### 新增

- **迭代 1 · 核心链路**：`casepilot gen` CLI（全中文帮助）；中文 PRD 与 OpenAPI 3.x 解析（功能点 / 数字边界 / 主流程）；按等价类划分、边界值分析、场景法、判定表生成带显式方法论标注的测试用例；禅道 Excel 导出（8 列导入模板）；pydantic v2 结构化校验（`TestCase` / `GenerationResult`）；LLM 网关双模式自动降级（OpenAI 兼容 API / 本地 Ollama / 确定性 mock 规则引擎）
- **迭代 2 · 导出与门禁**：pytest 脚本骨架导出（可被 pytest 正常收集的合法 Python，方法论自定义标记）；Markdown 用例表导出（8 要素表格 + 方法论统计）；禅道导入前校验器（表头列名与顺序、必填字段、优先级取值、Sheet 名逐项检查）；LLM 输出解析 / 校验失败自动重试（错误信息回灌提示词）；覆盖维度检查（数量 / 方法论 / 类型 / 标注非空）；边界值预期结果强制带具体数值与两侧相邻值
- **迭代 3 · 评测体系**：`casepilot eval` 生成质量 4 维评分（结构合法率 / 方法论覆盖率 / 边界数值化率 / 基准要素重合度），报告落盘 `output/eval_report_*.md`；`evals/datasets/` 3 个人工整理 golden 基准集（每样例 17/18/19 条，附设计理由）；`casepilot judge` LLM-as-a-Judge 用例质量自评（完整性 / 方法论标注合理性 / 优先级合理性 / 可执行性 4 维，LLM 评审 / 规则降级双模式）；等价类 / 场景法预期结果引用输入具体词与数值
- **迭代 4 · 工程化**：GitHub Actions CI（pytest + 覆盖率门禁 + Python 3.10 / 3.12 矩阵）与覆盖率徽章；评测驱动改进 mock 引擎（接口端点补齐规则，petstore eval 97.4 → 100.0）；README 效果对比表、演示实录与面试讲解要点；最佳实践文档 `docs/best_practices.md`
- **迭代 5 · v1.0 收尾**：`docs/release_notes_v1.0.md` Release Notes；本更新日志

### 变更

- 版本号定格 1.0.0（`casepilot.__version__`、`pyproject.toml` 与 README 版本声明统一）
- `--emit` 参数接替迭代 1 的 `--format`（`--format` 继续可用）

### 实测基线（v1.0.0）

- 332 个 pytest 测试，总覆盖率 96.13%（门禁 ≥80%），CI 矩阵 Python 3.10 / 3.12
- 3 个固定样例无 key 离线跑通：生成 27 / 30 / 25 条用例，结构合法率 100%
- 3 样例评测总分 100.0 / 100.0 / 100.0（golden 基准 17 / 18 / 19 条）
- LLM-as-a-Judge 好 / 坏用例集区分度 2.9 分（4.9 vs 2.0）

[1.0.0]: https://github.com/qpqpalalzmzm112/CasePilot/releases/tag/v1.0.0
