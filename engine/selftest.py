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
from combat import (Budget, CombatState, RangeProfile, ResourcePool, Step,
                    expected_save_damage, save_fail_probability,
                    super_maneuver_uses, validate_turn)
from character import (Build, ability_mod, armor_class, derive, max_hp,
                       proficiency_bonus)
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


def test_character():
    print("\n[10] 角色基础数值")
    for lv, want in ((1, 2), (4, 2), (5, 3), (9, 4), (13, 5), (17, 6), (21, 7)):
        check(f"熟练加值 L{lv}", Fraction(proficiency_bonus(lv)), Fraction(want), tol=1e-9)
    for score, want in ((8, -1), (11, 0), (13, 1), (16, 3)):
        check(f"调整值 {score}", Fraction(ability_mod(score)), Fraction(want), tol=1e-9)

    # AC：5z 的 AC 含熟练加值（与 5e 不同）
    check("无甲 AC", Fraction(armor_class("none", 2, 2)), Fraction(12), tol=1e-9)
    check("轻甲 AC", Fraction(armor_class("light", 2, 3)), Fraction(15), tol=1e-9)
    check("中甲 敏捷上限2", Fraction(armor_class("medium", 2, 4)), Fraction(17), tol=1e-9)
    check("重甲 忽略敏捷", Fraction(armor_class("heavy", 2, 5)), Fraction(18), tol=1e-9)
    check("狂战士无甲防御", Fraction(armor_class("barbarian_unarmored", 2, 3, con_mod=3)),
          Fraction(16), tol=1e-9)
    check("盾牌 +2", Fraction(armor_class("light", 2, 3, shield=True)), Fraction(17), tol=1e-9)

    # 生命值：10 + 等级 ×（生命骰期望 + 体质调整值）
    check("L1 d12 体质+3", Fraction(max_hp(1, 12, 3)), Fraction(19), tol=1e-9)
    check("L4 d12 体质+3", Fraction(max_hp(4, 12, 3)), Fraction(46), tol=1e-9)

    # 派生值的可追溯性：分项之和必须等于总值
    b = Build(level=4, base_scores={"str": 17, "dex": 13, "con": 16},
              hit_die_faces=12, armor="barbarian_unarmored",
              proficient_saves=("str", "con"), weapon_enhancement=1)
    d = derive(b)
    check("攻击加值合计", Fraction(d.attack.total), Fraction(6), tol=1e-9)
    check("分项与总值一致", Fraction(sum(p.value for p in d.attack.parts)),
          Fraction(d.attack.total), tol=1e-9)
    print(f"       {d.attack.explain()}")
    check("力量豁免含熟练", Fraction(d.saves["str"]), Fraction(5), tol=1e-9)
    check("敏捷豁免无熟练", Fraction(d.saves["dex"]), Fraction(1), tol=1e-9)


def test_resources():
    print("\n[11] 资源时钟")
    dice = ResourcePool(id="superiority", name="卓越骰", capacity=3,
                        recharge="short_rest", die_faces=6)
    check("短休池 无短休", Fraction(dice.effective_capacity(short_rests=0)), Fraction(3), tol=1e-9)
    check("短休池 1 次短休", Fraction(dice.effective_capacity(short_rests=1)), Fraction(6), tol=1e-9)
    check("骰面均值 d6", dice.die_mean(), Fraction(7, 2), tol=1e-9)
    check("消耗 2 枚的骰值", dice.spend_value(2), Fraction(7), tol=1e-9)

    # 狂暴次数：17 级起为「无」
    rage = ResourcePool(id="rage", name="狂暴次数", capacity=5,
                        recharge="long_rest", unlimited_from_level=17)
    check("16 级有限", Fraction(rage.effective_capacity(level=16)), Fraction(5), tol=1e-9)
    check("17 级无限", Fraction(rage.effective_capacity(level=17) == float("inf")),
          Fraction(1), tol=1e-9)

    # 超级战技：次数 = floor(可用卓越骰 / 10)，是派生量
    sup = ResourcePool(id="super_maneuver", name="超级战技", capacity=1,
                       recharge="per_n_long_rests", interval=2,
                       derived_from=("superiority", 10))
    check("超级战技次数", Fraction(super_maneuver_uses(25)), Fraction(2), tol=1e-9)

    budget = Budget(pools={"superiority": dice}, short_rests=1, level=4)
    check("预算内", Fraction(len(budget.check_schedule(
        [(1, "superiority", 3), (2, "superiority", 3)]))), Fraction(0), tol=1e-9)
    check("超总量", Fraction(len(budget.check_schedule(
        [(1, "superiority", 4), (2, "superiority", 4)]))), Fraction(1), tol=1e-9)

    # 派生池解析：卓越骰 3 枚 × (1+1 次短休) = 6 → 超级战技 0 次
    budget2 = Budget(pools={"superiority": dice, "super_maneuver": sup},
                     short_rests=1, level=4)
    check("派生池解析", Fraction(budget2.available("super_maneuver")), Fraction(0), tol=1e-9)

    # 未声明的池必须报错，不能静默通过
    check("未声明池报警", Fraction(len(budget2.check_schedule([(1, "unknown", 1)]))),
          Fraction(1), tol=1e-9)


def test_calculator():
    print("\n[12] 伤害计算器（三份输入 → 期望伤害）")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = os.path.join(root, "examples")

    def load(name):
        with open(os.path.join(ex, name), encoding="utf-8") as fh:
            return _json.load(fh)

    build, env, strategy = (load("barbarian_l4.build.json"),
                            load("dummy_ac15.env.json"),
                            load("power_attack.strategy.json"))
    res = calc.evaluate(build, env, strategy)

    # 派生值。攻击加值 = 力量调整值 4 + 熟练 2 + 武器增强 1 = 7。
    # 力量 4 来自购点 17 + 凶蛮打手的 +1（构筑期效果）。
    resolved = calc.resolve_build_full(build, calc.load_ops(), calc.load_rage_table())
    check("力量调整值含凶蛮打手", Fraction(resolved.derived.mods["str"]), Fraction(4), tol=1e-9)
    check("计算器攻击加值", Fraction(resolved.derived.attack.total), Fraction(7), tol=1e-9)
    check("计算器 AC", Fraction(resolved.derived.ac), Fraction(14), tol=1e-9)
    check("计算器 HP", Fraction(resolved.derived.hp), Fraction(46), tol=1e-9)
    check("狂暴伤害加值", Fraction(resolved.rage_bonus), Fraction(2), tol=1e-9)

    # 与"手工搭 Attack"的路径交叉验证：两条路必须给出同一个数。
    # 各项由构筑推导而非写死——凶蛮打手的 +1 属性会改变力量调整值，
    # 写死常数会让这个测试在构筑改动后失真。
    from evaluator import Attack, DamageComponent, TargetProfile
    target = TargetProfile(ac=15)
    flat = resolved.derived.mods["str"] + resolved.weapon["enhancement"]
    atk = Attack(attack_bonus=resolved.derived.attack.total - 5, adv_tiers=1)
    atk.components = [
        DamageComponent("1d12", flat=flat, dtype="slashing"),       # 武器+属性+增强
        DamageComponent("0", flat=resolved.rage_bonus, dtype="slashing"),   # 狂暴
        DamageComponent("0", flat=12, dtype="slashing"),            # 威力攻击
        DamageComponent("1d6", dtype="slashing"),                   # 凶蛮打手
    ]
    manual, _ = expected_damage(atk, target)
    check("计算器与手工路径一致", res.total, manual, tol=1e-9)
    print(f"       期望伤害 {float(res.total):.3f}｜结果树叶子 {res.leaves}")


def test_spells():
    print("\n[13] 法术：等级成长 / 升环 / 豁免")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "examples", "wizard_l5.build.json"), encoding="utf-8") as fh:
        build = _json.load(fh)
    resolved = calc.resolve_build_full(build, calc.load_ops(), calc.load_rage_table())
    ops = calc.load_ops()

    # 法师 L5：智力 18（+4）+ 熟练 3 → 法术攻击 +7，豁免 DC 15
    check("法术攻击加值", Fraction(resolved.derived.spell_attack.total), Fraction(7), tol=1e-9)
    check("法术豁免 DC", Fraction(resolved.derived.spell_dc), Fraction(15), tol=1e-9)

    # 等级成长：戏法在 5 级升为 2d8
    check("冻寒之触 L5 骰式",
          Fraction(int(calc.spell_dice(ops["sp.冻寒之触"], 5).split("d")[0])),
          Fraction(2), tol=1e-9)
    check("冻寒之触 L1 骰式",
          Fraction(int(calc.spell_dice(ops["sp.冻寒之触"], 1).split("d")[0])),
          Fraction(1), tol=1e-9)

    # 升环：火球术每高一环 +2d6
    check("火球术 3 环", Fraction(int(calc.spell_dice(ops["sp.火球术"], 5, 3).split("d")[0])),
          Fraction(8), tol=1e-9)
    check("火球术 5 环", Fraction(int(calc.spell_dice(ops["sp.火球术"], 5, 5).split("d")[0])),
          Fraction(12), tol=1e-9)

    # 期望值：攻击型 vs 豁免型
    atk = calc.build_spell_attack(resolved, ops["sp.冻寒之触"])
    tgt = TargetProfile(ac=15)
    p_hit, p_crit = atk.probs(15)
    expected_atk = p_hit * 9 + p_crit * 9          # 2d8 = 9
    got_atk, _ = expected_damage(atk, tgt)
    check("攻击型法术期望", got_atk, expected_atk, tol=1e-9)

    # 豁免型：DC 15 vs 敏捷豁免 0 → 失败率 14/20；无半伤
    dmg, dice = calc.save_effect_from_op(resolved, ops["sp.圣火术"], tgt)
    check("豁免型法术期望", dmg, Fraction(14, 20) * 9, tol=1e-9)

    # 半伤：0.7×28 + 0.3×14 = 23.8
    dmg2, dice2 = calc.save_effect_from_op(resolved, ops["sp.火球术"], tgt, slot_level=3)
    check("半伤法术期望", dmg2, Fraction(7, 10) * 28 + Fraction(3, 10) * 14, tol=1e-9)


def test_composite_and_contingency():
    print("\n[14] 复合动作与条件策略")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = os.path.join(root, "examples")

    def load(name):
        with open(os.path.join(ex, name), encoding="utf-8") as fh:
            return _json.load(fh)

    build = load("barbarian_l5.build.json")   # 5 级才有额外攻击，才能检验覆盖
    env = load("dummy_ac15.env.json")
    strat = load("mounted_charge.strategy.json")
    res = calc.evaluate(build, env, strat)

    # 骑乘冲刺把额外攻击给出的 2 次压成 1 次，并附加 1 重优势
    charge_line = next((t for t in res.trace if "覆盖额外攻击" in t), "")
    if "2 次 → 1 次" in charge_line:
        print("  OK   覆盖额外攻击：2 次 → 1 次")
    else:
        FAILS.append("骑乘冲刺未覆盖额外攻击")
        print("  FAIL 覆盖额外攻击")

    # 手算校验。各项由构筑推导，不写死等级常数，避免换等级后测试失真。
    resolved = calc.resolve_build_full(build, calc.load_ops(), calc.load_rage_table())
    op_ops = calc.load_ops()
    pa = op_ops["feat.威力攻击"]["projection"]["attack_bonus"]
    atk_bonus = resolved.derived.attack.total + pa        # 威力攻击
    ac = 15
    t_hit = ac - atk_bonus
    single_hit = _clamp01(Fraction(21 - t_hit, 20))
    single_both = _clamp01(Fraction(21 - max(t_hit, 20), 20))
    p_hit = 1 - (1 - single_hit) ** 2                    # 1 重优势（骑乘冲刺）
    p_crit = 1 - (1 - single_both) ** 2

    rage = resolved.rage_bonus
    flat = resolved.derived.mods["str"] + resolved.weapon["enhancement"]
    hit_dmg = Fraction(65, 10) + flat + rage + 12 + Fraction(7, 2) + Fraction(9, 2)
    crit_extra = Fraction(65, 10) + Fraction(7, 2) + Fraction(9, 2)
    expected = (p_hit * hit_dmg + p_crit * crit_extra) * 2   # 骑乘战斗易伤
    check("骑乘冲刺期望伤害", res.total, expected, tol=1e-9)

    # 条件策略的消耗被记录
    check("条件策略消耗入账", Fraction(len(res.spend_report)), Fraction(1), tol=1e-9)

    # 移动不足 30 尺时冲刺不生效
    short = _json.loads(_json.dumps(strat))
    short["rounds"][0]["move"] = {"toward": True, "ft": 20}
    res2 = calc.evaluate(build, env, short)
    if any("要求本回合直线移动" in w for w in res2.warnings) and res2.total == 0:
        print("  OK   移动不足 30 尺时冲刺不生效")
    else:
        FAILS.append("移动不足时冲刺未拦截")
        print("  FAIL 移动不足时冲刺未拦截")


def _clamp01(x):
    return max(Fraction(0), min(Fraction(1), x))


def test_charge_cycle():
    print("\n[15] 拉冲循环与坐骑速度")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = os.path.join(root, "examples")
    build = _json.load(open(os.path.join(ex, "barbarian_l4.build.json"), encoding="utf-8"))
    env = _json.load(open(os.path.join(ex, "dummy_ac15.env.json"), encoding="utf-8"))
    strat = _json.load(open(os.path.join(ex, "charge_cycle.strategy.json"), encoding="utf-8"))

    # 挽马 60 尺：冲刺 30 + 退开 30 → 每轮都能冲
    res60 = calc.evaluate(build, env, strat)
    hits60 = sum(1 for _, d in res60.per_round if d > 0)
    check("60 尺坐骑 3 轮全冲", Fraction(hits60), Fraction(3), tol=1e-9)

    # 矮马 50 尺：冲刺 30 后只剩 20 尺退开，下一轮空间不足 → 隔轮才能冲
    pony = _json.loads(_json.dumps(build))
    pony["combat"]["mount"] = {"name": "矮马", "speed_ft": 50, "controllable": True}
    res50 = calc.evaluate(pony, env, strat)
    hits50 = sum(1 for _, d in res50.per_round if d > 0)
    check("50 尺坐骑 3 轮只冲 2 次", Fraction(hits50), Fraction(2), tol=1e-9)
    if res50.total < res60.total:
        print(f"  OK   坐骑速度影响输出：50 尺 {float(res50.total):.2f} < 60 尺 {float(res60.total):.2f}")
    else:
        FAILS.append("坐骑速度未影响输出")
        print("  FAIL 坐骑速度未影响输出")

    # 靠近时实际位移受距离限制：距 20 尺走 30 尺，只应移动 20 尺
    s = CombatState(distance_ft=20, speed_ft=50)
    s.start_turn()
    moved = s.move(30, toward=True)
    check("位移受距离限制", Fraction(moved), Fraction(20), tol=1e-9)
    check("记录的实际移动量", Fraction(s.travel_this_turn), Fraction(20), tol=1e-9)


def test_expanded_ops():
    print("\n[16] 扩面算子：自动命中 / 多射线 / 豁免修正")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = os.path.join(root, "examples")
    build = _json.load(open(os.path.join(ex, "wizard_l5.build.json"), encoding="utf-8"))
    target = TargetProfile(ac=15)
    target.saves = {"dex": 0, "con": 0, "wis": 0, "int": 0, "str": 0, "cha": 0}
    resolved = calc.resolve_build_full(build, calc.load_ops(), calc.load_rage_table())
    ops = calc.load_ops()

    # 自动命中：3 枚 1d6，不掷攻击、不能重击 → 3×3.5 = 10.5
    res1 = calc.evaluate(build, {"target": {"ac": 15}}, {
        "rounds": [{"round": 1, "attacks": 1,
                    "per_attack": {"kind": "spell_attack", "spell": "sp.魔法飞弹",
                                   "slot_level": 1}}]})
    check("魔法飞弹 1 环", res1.total, Fraction(21, 2), tol=1e-9)

    # 升环加枚数（不是加骰）：2 环 5 枚 → 17.5
    res2 = calc.evaluate(build, {"target": {"ac": 15}}, {
        "rounds": [{"round": 1, "attacks": 1,
                    "per_attack": {"kind": "spell_attack", "spell": "sp.魔法飞弹",
                                   "slot_level": 2}}]})
    check("魔法飞弹 2 环（5 枚）", res2.total, Fraction(35, 2), tol=1e-9)

    # 多射线：每道 2d6，法术攻击 +7 对 AC 15
    single = Fraction(13, 20) * 7 + Fraction(1, 20) * 7
    res3 = calc.evaluate(build, {"target": {"ac": 15}}, {
        "rounds": [{"round": 1, "attacks": 1,
                    "per_attack": {"kind": "spell_attack", "spell": "sp.灼热射线",
                                   "slot_level": 2}}]})
    check("灼热射线 3 道", res3.total, single * 3, tol=1e-9)
    res4 = calc.evaluate(build, {"target": {"ac": 15}}, {
        "rounds": [{"round": 1, "attacks": 1,
                    "per_attack": {"kind": "spell_attack", "spell": "sp.灼热射线",
                                   "slot_level": 4}}]})
    check("灼热射线 4 环（5 道）", res4.total, single * 5, tol=1e-9)

    # 威力豁免：DC −5 换取豁免失败时额外伤害
    normal, _ = calc.save_effect_from_op(resolved, ops["sp.火球术"], target, 3)
    power, _ = calc.save_effect_from_op(resolved, ops["sp.火球术"], target, 3,
                                        dc_modifier=-5, on_fail_extra={"expr": "max_die(base_damage)", "cap": 30})
    # 火球术：基础骰多、半伤，降 DC 的损失大于 +6 的收益 → 反而更差
    if power < normal:
        print(f"  OK   威力豁免对火球术是负收益：{float(normal):.2f} → {float(power):.2f}")
    else:
        FAILS.append("威力豁免对火球术的预期收益不符")
        print("  FAIL 威力豁免对火球术")

    # 圣火术：骰数少、无半伤，额外伤害更值 → 正收益
    normal2, _ = calc.save_effect_from_op(resolved, ops["sp.圣火术"], target)
    power2, _ = calc.save_effect_from_op(resolved, ops["sp.圣火术"], target,
                                         dc_modifier=-5, on_fail_extra={"expr": "max_die(base_damage)", "cap": 30})
    if power2 > normal2:
        print(f"  OK   威力豁免对圣火术是正收益：{float(normal2):.2f} → {float(power2):.2f}")
    else:
        FAILS.append("威力豁免对圣火术应为正收益")
        print("  FAIL 威力豁免对圣火术")


def test_gates():
    print("\n[17] 算子前置条件校验")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    base = {"round": 1, "move": {"toward": True, "ft": 25}, "attacks": 1,
            "per_attack": {"ops": ["cf.狂战士.神性之怒"]}}

    no_rage = calc.evaluate(build, {"target": {"ac": 15}},
                            {"rounds": [dict(base, buffs=[])]})
    with_rage = calc.evaluate(build, {"target": {"ac": 15}},
                              {"rounds": [dict(base, buffs=["rage"])]})

    if any("前置条件不满足" in w for w in no_rage.warnings):
        print("  OK   未开狂暴时「神性之怒」被告警并跳过")
    else:
        FAILS.append("gate 未拦截缺狂暴的算子")
        print("  FAIL 未开狂暴时未拦截")

    if any("前置条件不满足" in w for w in with_rage.warnings):
        FAILS.append("开狂暴后不应再告警")
        print("  FAIL 开狂暴后仍告警")
    else:
        print("  OK   开狂暴后正常生效")

    if with_rage.total > no_rage.total:
        print(f"  OK   生效前后差异：{float(no_rage.total):.3f} → {float(with_rage.total):.3f}")
    else:
        FAILS.append("狂暴前后伤害无差异")
        print("  FAIL 狂暴前后伤害无差异")


def test_maneuvers():
    print("\n[18] 战技：卓越骰伤害 / 不可减伤")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    build["resources"]["superiority"] = {
        "name": "卓越骰", "capacity": 4, "recharge": "short_rest", "die_faces": 6}
    ops = calc.load_ops()

    def run(op_ids, target=None):
        s = {"rounds": [{"round": 1, "buffs": ["rage"],
                         "move": {"toward": True, "ft": 25}, "attacks": 1,
                         "per_attack": {"ops": op_ids}}]}
        return calc.evaluate(build, {"target": target or {"ac": 15}}, s)

    base = run([])
    # 5 级卓越骰为 d6，1 枚 = 3.5
    check("卓越骰面 d6", Fraction(calc.superiority_faces(5)), Fraction(6), tol=1e-9)
    for lv, faces in ((1, 4), (5, 6), (11, 8), (17, 10)):
        if calc.superiority_faces(lv) != faces:
            FAILS.append(f"卓越骰面 L{lv} 应为 d{faces}")
    print("  OK   卓越骰面随等级 d4→d6→d8→d10")

    with_die = run(["mv.开山流.低级.英勇冲锋"])
    if with_die.total > base.total:
        print(f"  OK   英勇冲锋加卓越骰：{float(base.total):.3f} → {float(with_die.total):.3f}")
    else:
        FAILS.append("英勇冲锋未增加伤害")
        print("  FAIL 英勇冲锋未增加伤害")

    # 「dS」要按等级解析，不能写死骰面
    if calc.resolve_superiority_dice("2dS", 11) == "2d8":
        print("  OK   dS 按等级解析为 2d8（11 级）")
    else:
        FAILS.append("dS 解析错误")
        print("  FAIL dS 解析")

    # 力劈华山：不可减伤，对斩击抗性目标应与无抗性相同
    irreducible = run(["mv.开山流.高级.力劈华山"],
                      {"ac": 15, "resist": ["slashing"]})
    plain = run([], {"ac": 15, "resist": ["slashing"]})
    no_resist = run([])
    check("不可减伤无视抗性", irreducible.total, no_resist.total, tol=1e-9)
    if plain.total < no_resist.total:
        print(f"  OK   普通攻击受抗性影响：{float(no_resist.total):.3f} → {float(plain.total):.3f}")
    else:
        FAILS.append("普通攻击未受抗性影响")
        print("  FAIL 抗性未生效")


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
    test_character()
    test_resources()
    test_calculator()
    test_spells()
    test_composite_and_contingency()
    test_charge_cycle()
    test_expanded_ops()
    test_gates()
    test_maneuvers()
    print("\n" + "=" * 58)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
