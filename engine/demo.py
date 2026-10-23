#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端演示：狂战士 4 级单次攻击的期望伤害，并与蒙特卡洛对拍。

只使用**能完整归因**的元素——每一项伤害都指得回规则书原文。
T1 范例卡的骰式（4d12+16+1d6+1d6+2+5）缺少逐项归因，因此不在此复现，
见 `形式化/回归用例.md` §6 的待确认问题。

运行：
    python -I engine/demo.py
"""

import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluator import Attack, DamageComponent, TargetProfile, expected_damage

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS = os.path.join(REPO, "形式化", "ops")

# 狂战士职业表：L1-5 狂暴伤害加值 +2，L6-10 +3，L11-15 +4
RAGE_BONUS = {1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3, 8: 3, 9: 3, 10: 3,
              11: 4, 12: 4, 13: 4, 14: 4, 15: 4, 16: 5, 17: 5, 18: 5, 19: 6, 20: 6}


def load_op(name):
    path = os.path.join(OPS, f"feat.{name}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"缺少算子: {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_attack(level, str_mod, weapon_dice, use_power_attack, adv_tiers,
                 target: TargetProfile):
    """狂战士单次近战武器攻击。

    可归因的伤害构成：
      武器骰     = weapon_dice
      属性       = 力量调整值
      狂暴       = 狂战士职业表的「狂暴伤害加值」
      专长       = 威力攻击的加值（= 基础伤害骰最大值，上限随等级提高）
    """
    prof = 2 + (level - 1) // 4
    attack_bonus = str_mod + prof
    components = [
        DamageComponent(weapon_dice, flat=str_mod + RAGE_BONUS[level],
                        dtype="slashing", slot="weapon+str+rage"),
    ]

    if use_power_attack:
        pa = load_op("威力攻击")
        proj = pa["projection"]
        attack_bonus += proj["attack_bonus"]
        max_die = int(weapon_dice.split("d")[1])
        cap = proj["damage_slots"][0]["cap"]
        for step in proj["damage_slots"][0].get("cap_scaling", []):
            if level >= step["level"]:
                cap = step["cap"]
        components.append(
            DamageComponent("0", flat=min(max_die, cap), dtype="slashing", slot="feat.威力攻击"))

    return Attack(attack_bonus=attack_bonus, adv_tiers=adv_tiers, components=components)


def main():
    level, str_mod, weapon = 4, 3, "1d12"      # 巨斧
    target = TargetProfile(ac=15)

    print("=" * 66)
    print(f"狂战士 L{level}｜力量调整值 +{str_mod}｜巨斧 {weapon}｜目标 AC {target.ac}")
    print("=" * 66)
    print(f"{'':26s}{'命中率':>10s}{'重击率':>10s}{'期望伤害':>12s}")
    print("-" * 66)

    rows = []
    for adv_label, adv in (("常规", 0), ("鲁莽攻击(1重优势)", 1)):
        for pa_label, use_pa in (("不用威力攻击", False), ("用威力攻击", True)):
            atk = build_attack(level, str_mod, weapon, use_pa, adv, target)
            p_hit, p_crit = atk.probs(target.ac)
            dmg, _ = expected_damage(atk, target)
            label = f"{adv_label}·{pa_label}"
            print(f"{label:26s}{float(p_hit):>9.2%}{float(p_crit):>10.2%}{float(dmg):>12.3f}")
            rows.append((label, dmg))

    base = rows[0][1]
    print("-" * 66)
    print("相对基准（常规·不用威力攻击）的变化：")
    for label, dmg in rows:
        delta = float((dmg - base) / base)
        print(f"  {label:26s} {delta:+.1%}")

    print("\n说明：")
    print("  · 命中率/重击率按优势层数逐层计算（n 重优势 = 掷 n+1 枚 d20 取最高）")
    print("  · 重击只翻伤害骰，不翻固定加值")
    print("  · 威力攻击的加值 = 基础伤害骰最大值（1d12 → 12），上限 15（7 级起 20）")
    print("  · 仅单次攻击；凶蛮打手的「每回合一次」需多攻击场景才能体现，本例未计入")


if __name__ == "__main__":
    main()
