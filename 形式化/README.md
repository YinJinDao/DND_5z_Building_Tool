# 规则形式化

> 状态：方案草稿待评审 | 最后更新：2026-10-23
> 本目录用于把 dnd5z 规则书转成可计算、可校验、可被工具和 AI 使用的结构化规则。原始资料在 `docs/`。

## 范围

当前只覆盖打击者（`Striker`）伤害链路所需的规则子集，例如攻击、AC、豁免、伤害类型、行动经济、资源等。范围外的规则按需扩展。

## 当前状态

- **方案 v0.2**：`方案-打桩伤害链.md`（打桩契约含距离模型、数据模型、结算管线、期望法/模拟法取舍、关键语义清单）。
- **数据库**：`数据库.md` 定义存储与字段约定；首批 A 档数据已落地。
- **回归用例**：`回归用例.md`（来自 `docs/【碗】5z狂战士、战士手册.docx` 第七节，含验算、口径换算与待补假设）。

首批数据：

| 表 | 条数 | 状态 |
| --- | --- | --- |
| `data/maneuvers.jsonl` | 252 | A 档完成 |
| `data/class_features.jsonl` | 475 | A 档完成（仅 9 个打击者职业），464 条有等级门槛 |
| `data/class_tables.jsonl` | 180 | 等级→特性 + 资源曲线 |
| `data/feats.jsonl` | 672 | A 档完成 |
| `data/spells.jsonl` | 1103 | A 档完成，读规则书正文（已弃用 1.49 版 xlsx） |
| `data/relevance.jsonl` | 2539 | 相关性粗筛 |
| `data/feat_triage.jsonl` | 672 | 专长按 F1–F12 分诊：in 267 / manual 185 / unclear 174 / defensive 27 / out 19 |

**重建顺序**（有依赖关系）：

```bash
python -I 形式化/tools/parse_class_tables.py   # 先生成职业表
python -I 形式化/tools/extract_classes.py      # 再抽特性（用职业表补门槛）
python -I 形式化/tools/extract_maneuvers.py
python -I 形式化/tools/extract_feats.py
python -I 形式化/tools/extract_spells.py
python -I 形式化/tools/screen_relevance.py
python -I 形式化/tools/screen_feats.py
```

下一步：继续 A 档抽取（职业表、专长、种族、装备），并开始 B 档算子编译。

## 参考

- 方案（伤害链）：[方案-打桩伤害链.md](方案-打桩伤害链.md)
- 方案（职业能力/专长/法术）：[方案-职业专长法术.md](方案-职业专长法术.md)
- 数据库约定：[数据库.md](数据库.md)
- 回归用例：[回归用例.md](回归用例.md)
- 抽取脚本：[tools/](tools/)
- 术语：[../docs/术语表.md](../docs/术语表.md)
- 目标与范围：[../plans/整体目标.md](../plans/整体目标.md)
- 方案草稿（云端）：[../docs/基于通用人工智能的5z打击者构筑优化.docx](../docs/基于通用人工智能的5z打击者构筑优化.docx)
- 决策记录：[../plans/决策记录.md](../plans/决策记录.md)

## 说明

- 目录结构、文件划分和工作方法按实际需要决定，这里不预先规定。
- 出处标注：**治理层不强制，数据层强制**。本文件不把流程强加给协作者，以避免并行工作中的指令冲突；而数据的 `source` 字段由 `数据库.md` §3.2 设为必填。见决策 D-015。
