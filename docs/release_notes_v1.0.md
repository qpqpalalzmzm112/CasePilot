# CasePilot v1.0.0 Release Notes（草稿）

> 本文为 GitHub Release 页面草稿，可直接粘贴到 v1.0.0 Release 描述。所有数字均为仓库实测，复现命令见 [README](../README.md)。

CasePilot v1.0.0 是首个正式版本：中文需求文档 / OpenAPI 接口文档 → 按测试方法论生成带显式标注的结构化测试用例 → 导出禅道 Excel / pytest 骨架 / Markdown，并内置生成质量评测与 LLM-as-a-Judge 用例质量自评。全程无 key 可离线跑通。

## 亮点特性

1. **显式测试方法论标注的用例生成**：按等价类划分、边界值分析、场景法、判定表生成用例，每条用例标注所用方法论，用例设计过程可解释、可评审。
2. **中文测试工作流直出禅道**：导出符合禅道官方导入模板的 Excel（固定 8 列、优先级数字映射），导出后自动执行导入前校验并输出中文报告，坏文件明确报出错误字段与行号。
3. **三格式一键导出**：禅道 Excel / pytest 脚本骨架（可被 pytest 正常收集的合法 Python，带方法论自定义标记）/ Markdown 用例表，`--emit xlsx|pytest|md` 切换。
4. **LLM 网关双模式三级降级**：有 key 走 OpenAI 兼容真实调用（temperature=0、JSON 输出）→ 无 key 探测本地 Ollama（500ms 超时）→ 降级为确定性 mock 规则引擎；纯离线也能完整跑通，运行时中文日志明示当前模式。
5. **质量评测体系**：`casepilot eval` 4 维离线评分（结构合法率 / 方法论覆盖率 / 边界数值化率 / 基准要素重合度，对照人工整理 golden 基准集）；`casepilot judge` LLM-as-a-Judge 逐条评审（LLM 评审 / 规则降级双模式）；pydantic v2 结构校验 + 解析失败自动重试 + 覆盖维度检查三层质量门禁；GitHub Actions CI（Python 3.10 / 3.12 矩阵 + 覆盖率门禁）。

## 量化效果

| 指标 | 实测数值 |
| --- | --- |
| pytest 测试 | 332 项通过，总覆盖率 96.13%（门禁 ≥80%） |
| 离线生成规模 | 3 个固定样例：27 / 30 / 25 条用例（无 key 纯离线） |
| 结构合法率 | 100%（3 样例共 82 条全部通过 pydantic 校验） |
| 评测总分 | 100.0 / 100.0 / 100.0（golden 基准 17 / 18 / 19 条，4 维全满分） |
| 评测驱动改进 | petstore 97.4 → 100.0（基准要素重合度 17/19 → 19/19） |
| LLM-as-a-Judge 区分度 | 好例 4.9 vs 坏例 2.0，分差 2.9（1-5 分制） |
| CI | Python 3.10 / 3.12 矩阵全绿 |

## 安装

```bash
git clone https://github.com/qpqpalalzmzm112/CasePilot.git
cd CasePilot
python -m venv .venv

# Windows PowerShell
.venv\Scripts\pip install -e ".[dev]"

# Linux / macOS
# .venv/bin/pip install -e ".[dev]"
```

要求 Python 3.10+。运行时依赖仅 `typer`、`pydantic>=2`、`openpyxl`、`pyyaml`、`httpx`。

## 快速开始

```bash
# 中文需求文档 → 禅道导入 Excel（默认 xlsx）
casepilot gen -i examples/prd_login.md

# OpenAPI 接口文档 → pytest 脚本骨架
casepilot api -i examples/openapi_petstore.yaml --emit pytest

# 生成质量评测：对照 golden 基准集输出 4 维中文评分报告
casepilot eval -i examples/prd_login.md

# LLM-as-a-Judge 用例质量自评
casepilot judge output/prd_login_cases.xlsx -o output/judge_report_prd_login.md

casepilot --version   # casepilot 1.0.0
```

无 API key 时自动降级为确定性 mock 引擎，纯离线即可跑通全部命令；配置 `CASEPILOT_LLM_API_KEY` / `OPENAI_API_KEY` 后走 OpenAI 兼容真实调用。密钥只从环境变量读取，仓库内无任何硬编码密钥。

## 已知限制

- 禅道列结构按官方导入模板对齐并已通过导入前校验；禅道 Web 端实际导入按 [docs/zentao_import.md](zentao_import.md) 的人工验证清单执行
- mock 模式为规则式生成，覆盖方法论骨架；接入真实 LLM 后用例细节更丰富
- 暂不支持 Word（.docx）等其他输入格式与多语言需求
- MCP 形态接入为后续规划

## 相关文档

- [README](../README.md)：完整使用说明与实测演示
- [CHANGELOG](../CHANGELOG.md)：5 轮迭代完整变更记录
- [docs/best_practices.md](best_practices.md)：最佳实践
- [docs/zentao_import.md](zentao_import.md)：禅道导入指南
