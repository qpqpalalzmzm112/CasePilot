"""CasePilot 评测集与评分框架（迭代 3，面试核心话题轮）。

包含三部分：
- datasets/：3 个固定样例的人工整理基准用例集（golden set，JSON 格式）
- metrics.py：4 维离线评分器（结构合法率 / 方法论覆盖率 / 边界数值化率 / 基准要素重合度）
- judge.py：LLM-as-a-Judge 用例质量自评（真实 LLM 评审 / 规则评分降级双模式）
- run.py：评测运行入口（casepilot eval 命令的编排实现）

全部评分可离线计算、结果确定性（同输入同分数）；输出全中文报告。
"""
