# CasePilot

> 中文需求文档 / OpenAPI 接口文档 → 按测试方法论生成结构化测试用例 → 导出禅道 Excel / pytest 骨架 / Markdown

CasePilot 是一个开源的 AI 测试用例生成 CLI 工具，面向中文测试工作流设计。它把需求文档或接口文档解析为结构化要点，按经典测试方法论（等价类划分、边界值分析、场景法、判定表）生成带**显式方法论标注**的测试用例，并直接导出可导入禅道的 Excel 用例表。

## 核心特性

- **显式测试方法论标注**：每条用例的「关键词」列标注所用方法论（等价类 / 边界值 / 场景法 / 判定表），用例设计过程可解释、可评审
- **多格式导出**：禅道导入 Excel（8 列标准模板）/ pytest 脚本骨架（可被 pytest 正常收集的合法 Python）/ Markdown 用例表，一键切换 `--emit xlsx|pytest|md`
- **中文测试工作流**：输出符合禅道导入模板的 Excel（`用例` Sheet、固定 8 列、优先级数字映射），导出后自动执行**导入前校验**并输出中文报告，贴合国内团队协作习惯
- **LLM 网关双模式自动降级**：配置 API key 时走 OpenAI 兼容协议真实调用；无 key 时先探测本地 Ollama（500ms 超时）；仍不可达则降级为**确定性规则引擎**（mock），纯离线也能完整跑通，运行时以中文日志明确提示当前模式
- **LLM-as-a-Judge 用例质量自评**（`casepilot judge`）：对生成的用例集按 4 维度（完整性 / 方法论标注合理性 / 优先级合理性 / 可执行性）逐条评审打分，输出中文报告与引用具体用例标题的改进建议；双模式：有 key 时 LLM 评审（temperature=0、JSON 输出、解析失败自动重试），无 key 时降级为**可解释规则评分**
- **生成质量可量化评测**（`casepilot eval`）：对照人工整理的 golden 基准集做 4 维离线评分（结构合法率 / 方法论覆盖率 / 边界数值化率 / 基准要素重合度），报告落盘 `output/eval_report_<样例名>.md`，同输入同分数（确定性）
- **结构化校验与质量门禁**：全部用例经 pydantic v2 模型校验（枚举、必填、禁止未知字段）；LLM 输出解析失败自动重试（错误信息回灌提示词）、覆盖维度检查（数量 / 方法论 / 类型 / 标注非空）、边界值预期结果强制带具体数值与两侧相邻值

## 快速开始

### 环境要求

- Python 3.10+（开发环境为 3.12）

### 安装

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\pip install -e ".[dev]"

# Linux / macOS
# .venv/bin/pip install -e ".[dev]"
```

### 生成用例

```bash
# 中文需求文档（.md / .txt）→ 禅道导入 Excel（默认 xlsx）
casepilot gen -i examples/prd_login.md

# OpenAPI 接口文档（.yaml / .yml / .json）→ pytest 脚本骨架（api 为接口专用入口）
casepilot api -i examples/openapi_petstore.yaml --emit pytest

# PRD → Markdown 用例表（按模块分组 + 方法论统计）
casepilot gen -i examples/prd_boundary.md --emit md

# 指定输出路径
casepilot gen -i examples/prd_boundary.md -o output/my_cases.xlsx
```

运行后会打印中文进度、覆盖维度检查结果与当前 LLM 模式，例如（无 key 离线环境）：

```text
正在解析输入文档：examples\prd_login.md
解析完成：模块「用户登录模块需求文档」，功能点 / 接口 6 个，数字边界 10 个，主流程步骤 5 步
已生成 27 条用例（全部通过 pydantic 结构校验）
覆盖维度检查：共 27 条用例
  - 用例数量 27 条，下限 8 条（达标）
  - 方法论覆盖 4 种（边界值、等价类、场景法、判定表），下限 3 种（达标）
  - 用例类型覆盖 3 种（边界、功能、异常），下限 3 种（达标）
  - 方法论标注非空：27 / 27 条（达标）
覆盖维度检查结论：达标
当前 LLM 模式：mock（无 API key 且本地 Ollama 不可达，已降级为确定性规则引擎，纯离线生成）
共生成 27 条用例，正在导出 xlsx 格式…
导出完成：output\prd_login_cases.xlsx（共 27 条用例）
禅道导入前校验：output\prd_login_cases.xlsx
  - Sheet「用例」存在，表头 8 列与官方导入模板一致
  - 数据行 27 行，必填字段完整
  - 优先级取值全部合法（1/2/3/4）
校验结论：可导入
```

### 版本与帮助

```bash
casepilot --version     # casepilot 0.1.0
casepilot gen --help    # 全中文帮助
```

### 评测与用例质量自评

```bash
# 生成质量评测：对照 golden 基准集输出 4 维中文评分报告（自动匹配 evals/datasets/<输入名>_golden.json）
casepilot eval -i examples/prd_login.md

# LLM-as-a-Judge 用例质量自评（无 key 时自动降级为可解释规则评分）
casepilot judge output/prd_login_cases.xlsx -o output/judge_report_prd_login.md
```

## CLI 命令

| 命令 | 说明 |
| --- | --- |
| `casepilot gen -i <文件> [--emit xlsx\|pytest\|md] [-o 输出路径]` | 解析文档并生成测试用例（PRD / OpenAPI 通用入口） |
| `casepilot api -i <openapi> [--emit xlsx\|pytest\|md] [-o 输出路径]` | OpenAPI 专用入口，`--emit pytest` 直接产出脚本骨架 |
| `casepilot eval -i <文件> [--golden <golden.json>]` | 评测生成质量：4 维评分报告落盘 `output/eval_report_<样例名>.md` |
| `casepilot judge <cases.xlsx> [-o 报告.md]` | LLM-as-a-Judge 用例质量自评（LLM 评审 / 规则降级双模式） |
| `casepilot --version` | 显示版本号 |

- `-i / --input`：输入文档路径。按扩展名自动识别：`.md` / `.txt` → 中文 PRD；`.yaml` / `.yml` / `.json` → OpenAPI
- `--emit`（迭代 1 的 `--format` 继续可用）：导出格式 `xlsx` / `pytest` / `md`，默认 `xlsx`（禅道导入用例表）
- `-o / --output`：输出文件路径，默认 `output/<输入名>_cases.xlsx`（pytest 格式为 `output/<输入名>_test_skeleton.py`，md 格式为 `output/<输入名>_cases.md`）

## 导出格式

### 1. 禅道 Excel 列规范（--emit xlsx，默认）

Sheet 名为「用例」，第一行为表头，列顺序固定：

| 所属模块 | 用例标题 | 前置条件 | 步骤 | 预期结果 | 优先级 | 用例类型 | 关键词 |
| --- | --- | --- | --- | --- | --- | --- | --- |

映射规则：

- **所属模块**：`父模块/子模块` 路径（如 `用户登录模块需求文档/边界验证`）
- **步骤**：多步骤用换行 `1. …\n2. …`
- **优先级**：`P0→1`、`P1→2`、`P2→3`、`P3→4`（禅道数字优先级）
- **用例类型**：填用例类型中文值（`功能` / `接口` / `异常` / `边界`）。禅道中可在「用例 → 用例类型」里自定义类型映射
- **关键词**：填测试方法论（`等价类` / `边界值` / `场景法` / `判定表`）——保住显式方法论标注

xlsx 导出后 CLI 自动执行**导入前校验**并输出中文报告（对照禅道官方模板逐字段检查：表头列名与顺序、必填字段、优先级取值 1/2/3/4、Sheet 名），坏文件会明确报出错误字段与行号。导入步骤与人工验证清单见 **[docs/zentao_import.md](docs/zentao_import.md)**。

### 2. pytest 脚本骨架（--emit pytest）

生成可被 pytest 正常收集运行的合法 Python 骨架（`pytest <文件> --collect-only` 零收集错误，未实现用例统一 `pytest.skip("待实现")`）：

- 文件头注释：来源文档、生成时间、方法论 → 标记映射说明
- 每条用例一个 `def test_<序号>_<slug>():` 函数（函数名由中文用例标题 / 接口名生成，合法 Python 标识符）
- 函数内注释块：前置条件 / 步骤 / 预期结果 / 用例标题 / 所属模块 / 优先级
- 方法论双标注：`# 方法: 边界值` 注释 + `@pytest.mark.boundary` 自定义标记
  （等价类 `equivalence`、边界值 `boundary`、场景法 `scenario`、判定表 `decision_table`）
- 函数体：`pytest.skip("待实现")` + TODO 注释，通过 `python -m py_compile` 校验

### 3. Markdown 用例表（--emit md）

- `# 用例集：<来源>` 文件标题 + `## <模块>` 按所属模块分组
- 每条用例一个三级标题 + 8 要素表格（用例标题 / 前置条件 / 步骤 / 预期结果 / 优先级 / 用例类型 / 测试方法论 / 所属模块）
- 尾部附方法论统计表（每种方法论用例数与占比）

## 数据模型

`TestCase`（pydantic v2 结构化校验）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| title | str | 用例标题 |
| precondition | str | 前置条件 |
| steps | str | 操作步骤（多步骤换行编号） |
| expected | str | 预期结果 |
| priority | P0 / P1 / P2 / P3 | 优先级 |
| category | 功能 / 接口 / 异常 / 边界 | 用例类型 |
| method | 等价类 / 边界值 / 场景法 / 判定表 | 测试方法论 |
| module | str | 所属模块（父模块/子模块） |

`GenerationResult`：`source`（输入来源）、`cases`（用例集）、`mode`（openai / ollama / mock）、`generated_at`（生成时间）。

## LLM 网关：双模式自动降级

模式决策顺序（`mode` 决策逻辑可被单测覆盖）：

1. 配置了 API key（`CASEPILOT_LLM_API_KEY` 或 `OPENAI_API_KEY`）→ **openai**：OpenAI 兼容协议 `POST {base_url}/chat/completions`，temperature=0，要求输出 JSON 数组，解析后过 pydantic 校验
2. 无 key 时探测本地 Ollama（默认 `http://localhost:11434/v1`，500ms 超时）→ **ollama**
3. Ollama 不可达 → **mock**：确定性规则引擎，同一输入输出完全一致，纯离线可跑

### 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `CASEPILOT_LLM_API_KEY` | API 密钥（优先） | 无 |
| `OPENAI_API_KEY` | API 密钥（兼容） | 无 |
| `CASEPILOT_LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://api.openai.com/v1`（ollama 模式为 `http://localhost:11434/v1`） |
| `CASEPILOT_LLM_MODEL` | 模型名 | `gpt-4o-mini`（ollama 模式为 `qwen2.5`） |
| `CASEPILOT_LLM_MAX_RETRIES` | LLM 输出解析 / 校验失败自动重试次数 | `2` |

> 密钥只从环境变量读取，任何文件、日志与提交中都不存在硬编码密钥。

### mock 规则引擎（离线降级）

1. 数字 / 比较词边界（≥/≤/不超过/至少/超过/满/减 等）→ **边界值**用例（取边界值与边界 ±1），
   预期结果强制引用具体数值与两侧相邻值（如「输入 5 时被接受…4 和 6 …」），拒绝通用套话
2. 功能点 / 动宾短语 → **等价类**用例（有效类 + 无效类），预期结果引用输入中的功能点具体词
   （如「…成功执行「支持账号密码登录」…」），不再是纯模板套话
3. 主流程操作序列 → **场景法**用例（基本流 / 备选流 / 异常流），预期结果引用具体场景要素
   （主流程步骤文本与文档中的边界值，如「触发「连续错误 5 次」（5次）限制时按需求规则处理」）
4. 优先级规则：主流程 P0、有效等价类与边界 P1、异常 / 无效类 P2
5. 多条件组合 → **判定表**用例；并保证每文档 ≥8 条用例、method 覆盖 ≥3 种、category 覆盖 ≥3 种

### LLM 输出质量门禁

- JSON 解析 / pydantic 结构校验失败时**自动重试**（≤ `CASEPILOT_LLM_MAX_RETRIES` 次，默认 2），
  每次把上一次的错误信息回灌提示词引导修正；仍失败抛出清晰中文错误
- 生成后自动执行**覆盖维度检查**（每文档 ≥8 条、method ≥3 种、category ≥3 种、method 无空值），
  不达标时以中文警告输出差距明细

## 固定样例（examples/）

| 样例 | 说明 | 生成规模（mock 模式） |
| --- | --- | --- |
| `examples/prd_login.md` | 中文登录需求：验证码、连续错误 5 次锁定 30 分钟、密码 8-20 位等边界 | 27 条，method 4 种 / category 3 种 |
| `examples/openapi_petstore.yaml` | OpenAPI 3.0 petstore：`/pet`、`/pet/{petId}`、`/store/order`、`/user`，含 schema 约束 | 27 条，method 4 种 / category 4 种 |
| `examples/prd_boundary.md` | 纯边界值场景：优惠券满 100 减 20、单次最多 50 件等大量数字边界 | 25 条，method 4 种 / category 3 种 |

三个样例均可无 key、无 Ollama 的纯离线环境跑通，生成结果结构合法率 100%；
等价类 / 场景法用例预期结果引用输入具体词或数值的比例为 **100%**（3 个样例实测，≥80% 门禁，见 `tests/test_mock_concrete.py`）。

## 评测与用例质量自评（迭代 3）

### `casepilot eval`：生成质量 4 维评分（评测集 + 评分框架）

`evals/` 包内置 3 个人工整理的 golden 基准集（`evals/datasets/*_golden.json`，每样例 17-19 条，
覆盖 4 种方法论 / 3+ 种类型 / P0-P2，附设计理由 note）。评分全部可离线计算、结果确定性：

| 维度 | 口径 |
| --- | --- |
| 结构合法率 | 生成用例通过 pydantic 校验的比例 |
| 方法论覆盖率 | golden 要求的 method 被生成集覆盖的比例 |
| 边界数值化率 | 边界值用例预期结果含具体数字的比例 |
| 基准要素重合度 | golden「模块+方法论+类型」三元组被生成集宽松匹配的比例 |

总分 = 4 维等权平均 × 100。3 个样例实测总分（mock 模式，可当场复现）：

| 样例 | 总分 | 说明 |
| --- | --- | --- |
| prd_login | 100.0 / 100 | 4 维全满分 |
| prd_boundary | 100.0 / 100 | 4 维全满分 |
| openapi_petstore | 97.4 / 100 | 基准要素重合度 89.5%（mock 未覆盖 `/user` 接口三元组，报告给出改进建议） |

### `casepilot judge`：LLM-as-a-Judge 用例质量自评

输入禅道 Excel（与 gen 产出兼容），按 4 维度（完整性 8 要素 / 方法论标注合理性 / 优先级合理性 / 可执行性）
逐条评审打分（1-5 分），报告头部注明评审模式并给出 ≥3 条引用具体用例标题的改进建议。
双模式沿用 LLM 网关：有 key 时 LLM 评审（temperature=0、JSON 输出、解析失败自动重试 ≤2 次），
无 key 降级为可解释规则评分（如「预期结果为空 → 完整性 1 分」）。

实测（mock 规则降级模式，确定性）：`output/prd_login_cases.xlsx`（27 条）总分 **4.9 / 5**；
构造的坏用例集（缺预期结果 + 优先级全 P0）总分 **1.5 / 5**，分差 3.4，且针对性建议
（如「用例「验证登录」的完整性被判低分：预期结果为空…」「用例集优先级问题：优先级全部为 P0…」）。

## 开发与测试

```bash
# 运行测试 + 覆盖率门禁（≥80%）
.venv\Scripts\python -m pytest --cov=casepilot --cov=evals --cov-fail-under=80
```

- 测试全部离线可跑（HTTP 全部替身 mock），覆盖：schema 校验、网关模式决策、PRD / OpenAPI 解析、mock 生成器确定性与方法论覆盖、Excel 列结构断言、CLI（typer CliRunner）、4 维评分器好 / 坏输入分数方向、golden 加载校验、judge 双模式（含 LLM 解析失败重试）、等价类 / 场景法预期结果具体值比例门禁（≥80%）
- 开发依赖仅 `pytest`、`pytest-cov`；运行时依赖仅 `typer`、`pydantic>=2`、`openpyxl`、`pyyaml`、`httpx`

## 项目结构

```text
casepilot/
  cli.py                 # typer CLI：gen / api 命令 + --version（全中文帮助）
  core/
    schema.py            # pydantic TestCase / GenerationResult / DocumentProfile
    methodology.py       # 方法论提示词模板（等价类/边界值/场景法/判定表）
    llm.py               # LLM 网关：OpenAI 兼容 / Ollama / mock 自动降级 + 解析失败自动重试
    mock_engine.py       # 确定性规则式 mock 生成器（边界值预期结果数值化）
    generator.py         # 编排：输入文本 → 用例集 → pydantic 校验 → 覆盖维度检查
    quality.py           # 质量门禁：覆盖维度检查 + 禅道导入前校验器
  adapters/
    prd.py               # 中文 PRD 解析（功能点 / 数字边界 / 主流程）
    openapi.py           # OpenAPI 3.x 解析（paths / parameters / schema）
  exporters/
    zentao_excel.py      # 禅道 Excel 导出（openpyxl）
    pytest_skeleton.py   # pytest 脚本骨架导出（可被 pytest 收集的合法 Python）
    markdown_export.py   # Markdown 用例表导出（8 要素表格 + 方法论统计）
evals/
  metrics.py             # 4 维评分器（结构合法率 / 方法论覆盖 / 边界数值化率 / 基准重合度）
  judge.py               # LLM-as-a-Judge 评审器（LLM 评审 / 规则评分降级双模式）
  run.py                 # 评测运行入口（casepilot eval 编排，支持 python -m evals.run）
  datasets/              # 3 个人工整理的 golden 基准集（JSON，含设计理由 note）
docs/
  zentao_import.md       # 禅道导入操作指南 + 人工验证清单
examples/                # 3 个离线固定样例
tests/                   # pytest 测试套件
```

## 路线图

- **迭代 2（已完成）**：pytest 脚本骨架导出、Markdown 导出、禅道导入前校验与导入指南、LLM 输出失败自动重试、质量门禁雏形（覆盖维度检查 / 边界值预期结果数值化）
- **迭代 3（已完成）**：LLM-as-a-Judge 用例质量自评（双模式）、evals 评测集与 4 维评分框架、等价类 / 场景法预期结果引用输入具体值
- **迭代 4**：MCP 形态接入、CI 与徽章

## 已知限制

- 禅道列结构按导入模板对齐并已通过导入前校验；禅道 Web 端实际导入按 [docs/zentao_import.md](docs/zentao_import.md) 的人工验证清单执行
- mock 模式为规则式生成，覆盖方法论骨架；接入真实 LLM 后用例细节更丰富
- 暂不支持 Word（.docx）等其他输入格式与多语言需求

## License

MIT
