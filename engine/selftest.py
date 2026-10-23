#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎自检：解析解 vs 蒙特卡洛。

按 D-019，蒙特卡洛不作为生产路径，只作**独立校验器**：两套实现收敛到同一数值，
才算引擎语义正确。相对误差超过阈值即失败。

运行：
    python -I engine/selftest.py
"""

import os
import random
import sys
from fractions import Fraction

# -I（隔离模式）不会把脚本目录放进 sys.path，这里显式补上，
# 以便 `python -I engine/selftest.py` 也能跑。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluator import (Attack, DamageComponent, TargetProfile,
                       apply_target_pipeline, dice_mean, expected_damage)
from combat import (CombatState, RangeProfile, Step, expected_save_damage,
                    save_fail_probability, validate_turn)
from strategy import CRIT, HIT, MISS, Rule, check_feasible, evaluate_turn

N = 200_000
SIGMA = 4.0          # 允许的偏离：4 倍蒙特卡洛标准误
random.seed(20261023)

FAILS = []


def check(name, got, want, tol=None):
    """与蒙特卡洛结果比对。

    不用固定相对阈值——抽样噪声随方差变化，固定阈值会把正常抖动误判为失败
    （实测 0.36% 的偏差对应约 1.2 倍标准误）。改为按标准误判定，
    即偏离不超过 SIGMA 倍 SE 即视为一致。
    """
    if tol is not None:                       # 精确断言（无抽样）
        ok = abs(float(got) - float(want)) <= tol
        print(f"  {'OK  ' if ok else 'FAIL'} {name}: 解析={float(got):.4f} 精确={float(want):.4f}")
    else:
        mean, se = want
        ok = abs(float(got) - mean) <= SIGMA * se
        ratio = abs(float(got) - mean) / se if se else 0
        print(f"  {'OK  ' if ok else 'FAIL'} {name}: 解析={float(got):.4f} "
              f"模拟={mean:.4f}±{se:.4f} ({ratio:.1f}σ)")
    if not ok:
        FAILS.append(name)


def roll_dice(expr, rng):
    """骰式求值（用于模拟）。"""
    import re
    m = re.match(r"^\s*(?:(\d+)\s*)?d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$", str(expr))
    if m:
        n = int(m.group(1) or 1)
        faces = int(m.group(2))
        flat = int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)
        return sum(rng.randint(1, faces) for _ in range(n)) + flat
    return int(expr)


def sim_attack(attack, target, n=N):
    """返回 (均值, 标准误)。"""
    rng = random.Random(1)
    total = 0.0
    total_sq = 0.0
    for _ in range(n):
        throws = max(1, attack.adv_tiers + 1) if attack.adv_tiers >= 0 else 1 - attack.adv_tiers
        d20s = [rng.randint(1, 20) for _ in range(throws)]
        roll = max(d20s) if attack.adv_tiers >= 0 else min(d20s)
        dmg_this = 0.0
        if attack.auto_hit or roll + attack.attack_bonus >= target.ac:
            crit = attack.auto_hit or roll >= 21 - attack.crit_range
            by_type = {}
            for c in attack.components:
                v = roll_dice(c.dice, rng) * (2 if (crit and c.crit_doubles) else 1) + c.flat
                by_type[c.dtype] = by_type.get(c.dtype, 0.0) + v
            for dtype, amount in by_type.items():
                dmg, _ = apply_target_pipeline(
                    Fraction(amount).limit_denominator(10 ** 6), dtype, target)
                dmg_this += float(dmg)
        total += dmg_this
        total_sq += dmg_this * dmg_this
    mean = total / n
    var = max(0.0, total_sq / n - mean * mean)
    return mean, (var / n) ** 0.5


def test_hit_probability():
    print("\n[1] 命中率与优势层数")
    for adv in (-2, -1, 0, 1, 2, 3):
        atk = Attack(attack_bonus=5, adv_tiers=adv,
                     components=[DamageComponent("1d8", dtype="slashing")])
        tgt = TargetProfile(ac=15)
        analytic, _ = expected_damage(atk, tgt)
        check(f"adv={adv:+d}", analytic, sim_attack(atk, tgt, 120_000))


def test_crit():
    print("\n[2] 重击范围与骰子翻倍")
    for cr in (1, 2, 3):
        atk = Attack(attack_bonus=7, crit_range=cr, components=[
            DamageComponent("2d6", flat=4, dtype="slashing"),
            DamageComponent("1d8", dtype="fire", crit_doubles=True),
        ])
        tgt = TargetProfile(ac=16)
        analytic, _ = expected_damage(atk, tgt)
        check(f"crit_range={cr}", analytic, sim_attack(atk, tgt, 150_000))


def test_target_pipeline():
    print("\n[3] 目标端结算顺序（规则书证据）")
    # 25 点钝击、所有伤害减少 5 的灵光、具抗性 → 先减 5 得 20，再减半得 10
    t = TargetProfile(resist=("bludgeoning",), pre_reduction={"all": 5})
    dmg, _ = apply_target_pipeline(Fraction(25), "bludgeoning", t)
    check("灵光先于抗性", dmg, Fraction(10), tol=1e-9)

    # 抗性+易伤 = 奇数伤害 -1，偶数不变
    t = TargetProfile(resist=("fire",), vuln=("fire",))
    for raw, want in ((25, 24), (24, 24), (7, 6)):
        dmg, _ = apply_target_pipeline(Fraction(raw), "fire", t)
        check(f"抗性+易伤 {raw}", dmg, Fraction(want), tol=1e-9)

    # 吸收+易伤 = 回复双倍（规则书原例：5 点火焰 → 回复 10）
    t = TargetProfile(vuln=("fire",), absorb=("fire",), immune=("fire",))
    dmg, heal = apply_target_pipeline(Fraction(5), "fire", t)
    check("吸收+易伤 治疗量", heal, Fraction(10), tol=1e-9)
    check("吸收时伤害为0", dmg, Fraction(0), tol=1e-9)

    # 抗性后减伤在抗性之后
    t = TargetProfile(resist=("slashing",), post_reduction={"slashing": 3})
    dmg, _ = apply_target_pipeline(Fraction(20), "slashing", t)
    check("抗性后减伤", dmg, Fraction(7), tol=1e-9)


def test_dice():
    print("\n[4] 骰式解析")
    for expr, want in (("2d6", 7), ("1d8", 4.5), ("1d12", 6.5), ("1d20", 10.5), ("3", 3)):
        check(f"{expr}", dice_mean(expr), Fraction(want), tol=1e-9)
    check("1d8+2", dice_mean("1d8+2"), Fraction(6.5), tol=1e-9)


def test_expected_value_identity():
    print("\n[5] 期望恒等式：E = P_hit×总值 + P_crit×翻倍骰")
    atk = Attack(attack_bonus=6, adv_tiers=1, crit_range=2, components=[
        DamageComponent("1d10", flat=3, dtype="slashing"),
    ])
    tgt = TargetProfile(ac=17)
    p_hit, p_crit = atk.probs(tgt.ac)
    manual = p_hit * (Fraction(11, 2) + 3) + p_crit * Fraction(11, 2)
    analytic, _ = expected_damage(atk, tgt)
    check("恒等式", analytic, manual, tol=1e-9)


def test_movement():
    print("\n[6] 距离与移动")
    s = CombatState(distance_ft=30, speed_ft=30)
    s.start_turn()
    melee = RangeProfile(reach_ft=5)
    if melee.can_reach(s.distance_ft):
        FAILS.append("初始距离 30 尺不应能近战触及")
        print("  FAIL 初始距离判定")
    else:
        print("  OK   初始 30 尺不可近战触及")
    moved = s.move(25, toward=True)
    check("移动 25 尺后剩余移动力", Fraction(s.movement_left), Fraction(5), tol=1e-9)
    check("冲刺后距离", Fraction(s.distance_ft), Fraction(5), tol=1e-9)
    if melee.can_reach(s.distance_ft):
        print(f"  OK   移动 {moved} 尺后距离 {s.distance_ft} 尺，已进入触及")
    else:
        FAILS.append("移动 25 尺后应进入 5 尺触及")
        print("  FAIL 移动后仍未进入触及")

    # 移动力耗尽后不能再移动
    s.move(10, toward=True)
    check("移动力耗尽后不再移动", Fraction(s.movement_left), Fraction(0), tol=1e-9)

    # 冲锋：本回合直线移动需 ≥30 尺
    s2 = CombatState(distance_ft=60, speed_ft=30)
    s2.start_turn()
    s2.move(30, toward=True)
    check("冲锋所需移动量", Fraction(s2.travel_this_turn), Fraction(30), tol=1e-9)


def test_action_economy():
    print("\n[7] 动作经济校验")
    s = CombatState()
    s.start_turn()
    s.attack_instances = 2

    ok = [Step("攻击", {"action": 1}, group="atk"),
          Step("替换攻击", {"replace_attack": 1}, group="atk"),
          Step("替换攻击", {"replace_attack": 1}, group="atk")]
    check("合法回合", Fraction(len(validate_turn(ok, s))), Fraction(0), tol=1e-9)

    dup = [Step("攻击", {"action": 1}), Step("再攻击", {"action": 1})]
    check("重复使用动作", Fraction(len(validate_turn(dup, s))), Fraction(1), tol=1e-9)

    insert = [Step("攻击", {"action": 1}, group="atk"),
              Step("狂暴", {"bonus_action": 1}),
              Step("额外攻击", {"replace_attack": 1}, group="atk")]
    check("动作中插入附赠动作", Fraction(len(validate_turn(insert, s))), Fraction(1), tol=1e-9)

    over = [Step("攻击", {"action": 1}, group="atk"),
            Step("替换一", {"replace_attack": 1}, group="atk"),
            Step("替换二", {"replace_attack": 1}, group="atk"),
            Step("替换三", {"replace_attack": 1}, group="atk")]
    check("替换超出攻击次数", Fraction(len(validate_turn(over, s))), Fraction(1), tol=1e-9)


def test_saves():
    print("\n[8] 豁免型伤害")
    # DC 15，豁免加值 +5 → 需 d20 ≥ 10 → 成功概率 11/20，失败 9/20
    check("豁免失败概率", save_fail_probability(15, 5), Fraction(9, 20), tol=1e-9)
    check("无半伤", expected_save_damage(15, 5, Fraction(20)),
          Fraction(9, 20) * 20, tol=1e-9)
    # 半伤：9/20×20 + 11/20×10 = 9 + 5.5 = 14.5
    check("半伤", expected_save_damage(15, 5, Fraction(20), half_on_save=True),
          Fraction(29, 2), tol=1e-9)
    # 奇数伤害半伤向下取整：7 → 3
    check("半伤取整", expected_save_damage(15, 5, Fraction(7), half_on_save=True),
          Fraction(9, 20) * 7 + Fraction(11, 20) * 3, tol=1e-9)


def test_strategy():
    print("\n[9] 条件策略与结果树")
    tgt = TargetProfile(ac=15)
    atk = Attack(attack_bonus=5, components=[
        DamageComponent("1d12", flat=5, dtype="slashing")])

    # 无规则：两次攻击应等于单次期望 ×2（攻击间独立）
    one, _ = evaluate_turn([atk], [], tgt)
    two, leaves = evaluate_turn([atk, atk], [], tgt)
    check("两击 = 单击×2", two, one * 2, tol=1e-9)
    check("两击叶子数", Fraction(leaves), Fraction(9), tol=1e-9)

    # 与 evaluator 的单次实现交叉验证
    single, _ = expected_damage(atk, tgt)
    check("与单次实现一致", one, single, tol=1e-9)

    # 条件规则：每次命中额外 1d6（模拟"命中后才花卓越骰"）
    rule = Rule(trigger="hit_or_crit", name="命中后加骰",
                extra_dice=[DamageComponent("1d6", dtype="slashing")])
    with_rule, _ = evaluate_turn([atk], [rule], tgt)
    if with_rule <= one:
        FAILS.append("条件加骰应提高期望")
        print("  FAIL 条件加骰未提高期望")
    else:
        print(f"  OK   命中后加 1d6：{float(one):.3f} → {float(with_rule):.3f}")

    # 失手后给下一次攻击优势
    rule2 = Rule(trigger="miss", on_attack=0, name="失手后造优", next_adv=1)
    boosted, _ = evaluate_turn([atk, atk], [rule2], tgt)
    if boosted <= one * 2:
        FAILS.append("失手造优应提高总期望")
        print("  FAIL 失手造优未生效")
    else:
        print(f"  OK   首击失手后次击有优势：{float(one*2):.3f} → {float(boosted):.3f}")

    # 资源可行性
    problems = check_feasible([Rule(trigger=HIT, spends={"卓越骰": 2})], {"卓越骰": 1})
    check("资源不足报警", Fraction(len(problems)), Fraction(1), tol=1e-9)


def main():
    print("=" * 58)
    print("5z 打桩伤害引擎自检")
    print("=" * 58)
    test_dice()
    test_hit_probability()
    test_crit()
    test_target_pipeline()
    test_expected_value_identity()
    test_movement()
    test_action_economy()
    test_saves()
    test_strategy()
    print("\n" + "=" * 58)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
