# 规则形式化

> 状态：A 档编译完成（904/904） | 最后更新：2026-10-24
> 本目录用于把 dnd5z 规则书转成可计算、可校验、可被工具和 AI 使用的结构化规则。原始资料在 `docs/`。

## 范围

当前只覆盖打击者（`Striker`）伤害链路所需的规则子集，例如攻击、AC、豁免、伤害类型、行动经济、资源等。范围外的规则按需扩展。

## 当前状态

- **方案 v0.2**：`方案-打桩伤害链.md`（打桩契约含距离模型、数据模型、结算管线、期望法/模拟法取舍、关键语义清单）。
- **数据库**：`数据库.md` 定义存储与字段约定；首批 A 档数据已落地。
- **回归用例**：`回归用例.md`（来自 `docs/【碗】5z狂战士、战士手册.docx` 第七节，含验算、口径换算与待补假设）。

首批数据：

| 表 | 条数 | 说明 |
| --- | --- | --- |
| `data/maneuvers.jsonl` | 258 | 战技，A 档 64 条 |
| `data/class_features.jsonl` | 607 | 全部 14 个玩家职业（召唤物除外） |
| `data/class_tables.jsonl` | 180 | 等级→特性 + 资源曲线 |
| `data/feats.jsonl` | 672 | 专长，A 档 181 条 |
| `data/spells.jsonl` | 1103 | 法术，读规则书正文（已弃用 1.49 版 xlsx） |
| `data/relevance.jsonl` | 2640 | 相关性粗筛（A 档 904） |

**A 档 904 条已全部编译完成，`needs_ruling` 归零：**

| 类别 | 总数 | modeled | out_of_scope |
| --- | ---: | ---: | ---: |
| 职业特性 | 192 | 78 | 114 |
| 专长 | 181 | 64 | 117 |
| 战技 | 64 | 57 | 7 |
| 法术 | 467 | 289 | 178 |
| **合计** | **904** | **488** | **416** |

「编译完成」不等于「都能算伤害」：416 条 out_of_scope 每一条都在算子里写了
`scope_reason`，按性质分为召唤/部署物、纯防御、控制减益、属性损伤、
形态替换、斩杀与自杀招式、多目标退化、法术与装备专属改写。

算子总数 **757**（`ops/*.json`），每一条都有 `source`（书/版本/文件/锚点）。

三道闸门：

```bash
python -I 形式化/tools/check_interfaces.py   # A1–A13 接口与词汇表
python -I 形式化/tools/verify_ops.py         # 每个算子装上/卸下对拍
python -I engine/build_check.py examples/*.build.json   # 构筑书写校验
```

`verify_ops` 的分类：CHANGED（生效）/ INERT（装了没用，需人工确认）/
SCOPED（已判定 out_of_scope，本就不该产生伤害）/ ERROR / SKIP。

伤害引擎在 [../engine/](../engine/)，自检 `python -I engine/selftest.py`（30 组）。

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

数据重建后若专长的先决条件有变动，需重跑
`python -I 形式化/tools/sync_feat_prereq.py --write`
把先决条件同步进算子——构筑校验读的是算子那一层。

下一步：B 档条目复核、收敛已标注的高估点、多实例结算与状态化触发。

## 参考

- 方案（伤害链）：[方案-打桩伤害链.md](方案-打桩伤害链.md)
- 方案（职业能力/专长/法术）：[方案-职业专长法术.md](方案-职业专长法术.md)
- **输入格式规范（一期计算器）**：[输入格式.md](输入格式.md)
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
