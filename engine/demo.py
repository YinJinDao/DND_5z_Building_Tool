#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端演示：狂战士 4 级，构筑 → 派生值 → 期望伤害。

只使用**能完整归因**的元素——每一项伤害都指得回规则书原文。
T1 范例卡的骰式缺少逐项归因，不作为验收用例（见 `形式化/回归用例.md` §5.1）。

运行：
    python -I engine/demo.py
"""

import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from character import Bonus, Build, Contribution, derive
from evaluator import Attack, DamageComponent, TargetProfile, expected_damage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(REPO, "形式化", "ops")

# 狂战士职业表（parse_class_tables.py 从规则书 <table> 抽取）
RAGE_BONUS = {1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 9: 3, 10: 3,
              11: 4, 12: 4, 13: 4, 14: 4, 15: 4, 16: 5, 17: 5, 18: 5, 19: 6, 20: 6}


def load_op(name):
    path = os.path.join(OPS, f"feat.{name}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"缺少算子: {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def make_build(level, str_score, weapon_enhancement):
    return Build(
        level=level,
        base_scores={"str": str_score, "dex": 13, "con": 16},
        hit_die_faces=12,                       # 狂战士 D12
        armor="barbarian_unarmored",            # 狂战士无甲防御
        proficient_saves=("str", "con"),
        weapon_attack_ability="str",
        weapon_enhancement=weapon_enhancement,
    )


def make_attack(derived, level, weapon_dice, use_power_attack, adv_tiers):
    """组装一次攻击：伤害构成与攻击加值都保留来源。"""
    parts = list(derived.attack.parts)
    # 魔法武器的增强加值同时作用于攻击检定与伤害（原先只加在攻击加值上，是 bug）
    enhancements = sum(p.value for p in derived.attack.parts if p.source == "武器增强")
    components = [
        DamageComponent(weapon_dice,
                        flat=derived.mods["str"] + enhancements + RAGE_BONUS[level],
                        dtype="slashing", slot="weapon+str+enhancement+rage"),
    ]

    if use_power_attack:
        proj = load_op("威力攻击")["projection"]
        max_die = int(weapon_dice.split("d")[1])
        cap = proj["damage_slots"][0]["cap"]
        for step in proj["damage_slots"][0].get("cap_scaling", []):
            if level >= step["level"]:
                cap = step["cap"]
        components.append(DamageComponent("0", flat=min(max_die, cap),
                                          dtype="slashing", slot="feat.威力攻击"))
        parts.append(Contribution("威力攻击", proj["attack_bonus"]))

    bonus = Bonus(parts)
    return Attack(attack_bonus=bonus.total, adv_tiers=adv_tiers,
                  components=components), bonus


def main():
    level, str_score, weapon, enh = 4, 17, "1d12", 1      # 巨斧 +1
    d = derive(make_build(level, str_score, enh))

    print("=" * 70)
    print(f"狂战士 L{level}｜巨斧 {weapon} +{enh}｜目标 AC 15")
    print("=" * 70)
    print(d.summary())
    print("-" * 70)

    target = TargetProfile(ac=15)
    print(f"{'':28s}{'攻击加值':>10s}{'命中率':>10s}{'重击率':>10s}{'期望伤害':>10s}")
    print("-" * 70)

    rows = []
    for adv_label, adv in (("常规", 0), ("鲁莽攻击(1重优势)", 1)):
        for pa_label, use_pa in (("不用威力攻击", False), ("用威力攻击", True)):
            atk, bonus = make_attack(d, level, weapon, use_pa, adv)
            p_hit, p_crit = atk.probs(target.ac)
            dmg, _ = expected_damage(atk, target)
            label = f"{adv_label}·{pa_label}"
            print(f"{label:28s}{bonus.total:>+9d}{float(p_hit):>9.2%}"
                  f"{float(p_crit):>10.2%}{float(dmg):>10.3f}")
            rows.append((label, dmg))

    base = rows[0][1]
    print("-" * 70)
    print("相对基准（常规·不用威力攻击）的变化：")
    for label, dmg in rows:
        print(f"  {label:28s} {float((dmg - base) / base):+.1%}")

    # 结论由计算结果推出，不写死数字——否则参数一变结论就失真
    no_adv_gain = (rows[1][1] - rows[0][1]) / rows[0][1]
    adv_gain = (rows[3][1] - rows[2][1]) / rows[2][1]
    print("\n关键结论：")
    print(f"  威力攻击在无优势时 {float(no_adv_gain):+.1%}，"
          f"在鲁莽攻击（1 重优势）下 {float(adv_gain):+.1%}。")
    print("  -5 命中被优势抬高的命中率部分抵消，而伤害加值是固定的——")
    print("  两者之比决定了造优与威力攻击的互补程度，这不是线性叠加。")


if __name__ == "__main__":
    main()
