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
    style_flat = _style_flat(build, 1)
    atk = Attack(attack_bonus=resolved.derived.attack.total - 5, adv_tiers=1)
    atk.components = [
        DamageComponent("1d12", flat=flat, dtype="slashing"),       # 武器+属性+增强
        DamageComponent("0", flat=resolved.rage_bonus, dtype="slashing"),   # 狂暴
        DamageComponent("0", flat=12, dtype="slashing"),            # 威力攻击
        DamageComponent("1d6", dtype="slashing"),                   # 凶蛮打手
        DamageComponent("0", flat=style_flat, dtype="slashing"),    # 巨武器战斗 ×1 重优势
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
    # 「巨武器战斗」风格：每重优势命中后 +3。范例构筑选了它，
    # 手算路径必须一并算进来，否则两边比的就不是同一个构筑了
    style_flat = _style_flat(build, 1)                   # 骑乘冲刺给 1 重优势
    hit_dmg = (Fraction(65, 10) + flat + rage + 12
               + Fraction(7, 2) + Fraction(9, 2) + style_flat)
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


def _style_flat(build, adv_tiers):
    """「巨武器战斗」这类按优势层数给固定加值的风格，手算路径也要计入。

    必须先用 apply_choices 展开构筑的选择——原始的战斗风格算子里只有
    `options`，真正的 `scales_with_adv` 在选中的那一项下面。
    """
    import calculator as calc
    ops = calc.apply_choices(calc.load_ops(), build)
    per = 0
    for op in calc.granted_class_ops(build, ops):
        spec = (op.get("projection") or {}).get("scales_with_adv") or {}
        per += int((spec.get("per_tier") or {}).get("flat", 0))
    return per * adv_tiers


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
    # 清空专长：本组只测战技本身，被动专长会自动生效并干扰对照
    build["feats"] = []
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


def test_tables_and_formulas():
    print("\n[19] 伤害查表与公式型伤害")
    import json as _json
    import calculator as calc

    tables = calc.load_damage_tables()
    check("伤害表数量", Fraction(len(tables)), Fraction(2), tol=1e-9)

    class _Stub:
        size = "medium"
        raw = {"combat": {"material": "血肉", "speed_ft": 30}}

    # 交叉表：体型 × 材质。构筑侧用英文键，查表时映射为中文
    for size, want in (("medium", "4d6"), ("large", "8d6"), ("gargantuan", "16d6")):
        stub = type("S", (), {"size": size, "raw": _Stub.raw})()
        got = calc.resolve_damage_table(tables["tbl.泰山压顶"], stub)
        if got != want:
            FAILS.append(f"泰山压顶 {size} 应为 {want}，实际 {got}")
    print("  OK   泰山压顶 体型×材质查表（medium→4d6 … gargantuan→16d6）")

    stub_steel = type("S", (), {"size": "medium",
                                "raw": {"combat": {"material": "钢铁岩石"}}})()
    if calc.resolve_damage_table(tables["tbl.泰山压顶"], stub_steel) != "4d8":
        FAILS.append("泰山压顶 钢铁岩石 应为 4d8")
    else:
        print("  OK   泰山压顶 材质轴（钢铁岩石→4d8）")

    # 分档表：按临时生命值取档
    for hp, want in ((0, "4dS"), (5, "6dS"), (25, "10dS"), (100, "14dS"), (400, "18dS")):
        got = calc.resolve_damage_table(tables["tbl.长城"], _Stub(), {"temporary_hp": hp})
        if got != want:
            FAILS.append(f"长城 temp_hp={hp} 应为 {want}，实际 {got}")
    print("  OK   长城按临时生命值正确分档（0→4dS … 400→18dS）")

    # 公式：苍鹰击于殿 = 速度/2.5，上限 50
    ctx = {"speed": 30, "level": 5}
    check("速度 30 → 伤害", Fraction(calc.eval_formula("speed / 2.5", ctx)), Fraction(12), tol=1e-9)
    check("上限 50", Fraction(calc.eval_formula("min(speed / 2.5, 50)", {"speed": 200})),
          Fraction(50), tol=1e-9)


def _split(dice):
    """'4d6' → (4, 6)，供 Fraction 比较。"""
    import re as _re
    m = _re.match(r"(\d+)d(\d+)", str(dice))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 1)


def test_opposed_check():
    print("\n[20] 对抗检定分布")
    from combat import d20_tier_distribution, opposed_win_probability

    # 无优势时 d20 分布均匀
    d = d20_tier_distribution(0)
    check("d20 分布总和", sum(d.values()), Fraction(1), tol=1e-12)
    check("d20 均值", sum(k * p for k, p in d.items()), Fraction(21, 2), tol=1e-9)

    # 1 重优势 = 取两枚最高，均值 13.825
    d1 = d20_tier_distribution(1)
    check("1 重优势均值", sum(k * p for k, p in d1.items()), Fraction(553, 40), tol=1e-9)
    # 1 重劣势 = 取两枚最低，均值 7.175
    dn = d20_tier_distribution(-1)
    check("1 重劣势均值", sum(k * p for k, p in dn.items()), Fraction(287, 40), tol=1e-9)

    # 加值相同、平手不算胜 → 略低于 0.5
    even = opposed_win_probability(5, 5)
    if even < Fraction(1, 2):
        print(f"  OK   平手不算胜：{float(even):.4f} < 0.5")
    else:
        FAILS.append("平手应判守方胜")
        print("  FAIL 平手判定")

    # 守方不继承攻方的优势——早期实现让守方也用攻方的分布，两边抵消
    adv = opposed_win_probability(5, 5, adv_tiers=1)
    if adv > even:
        print(f"  OK   攻方 1 重优势：{float(even):.4f} → {float(adv):.4f}")
    else:
        FAILS.append("攻方优势未提高胜率")
        print("  FAIL 优势未生效")

    # 卓越骰加入检定
    with_die = opposed_win_probability(5, 5, (1, 6))
    if with_die > even:
        print(f"  OK   卓越骰加值：{float(even):.4f} → {float(with_die):.4f}")
    else:
        FAILS.append("卓越骰未提高胜率")
        print("  FAIL 卓越骰未生效")

    # 守方可在多个加值中择优 → 取对攻方最不利
    chosen = opposed_win_probability(5, [5, 8])
    if chosen <= opposed_win_probability(5, 5):
        print(f"  OK   守方择优：{float(chosen):.4f} ≤ 对等值胜率")
    else:
        FAILS.append("守方择优未降低攻方胜率")
        print("  FAIL 守方择优")

    # 战技：泰山压顶按对抗胜率加权（10.5 × 0.8717 ≈ 9.15）
    import json as _json
    import calculator as calc
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    build["feats"] = []          # 同上：隔离被动专长的影响
    build.setdefault("resources", {})["superiority"] = {
        "name": "卓越骰", "capacity": 9, "recharge": "short_rest", "die_faces": 6}
    strat = {"rounds": [{"round": 1, "buffs": ["rage"],
                         "move": {"toward": True, "ft": 25}, "attacks": 1,
                         "per_attack": {"ops": ["mv.开山流.中级.泰山压顶"]}}]}
    res = calc.evaluate(build, {"target": {"ac": 15}}, strat)
    # 期望值由构筑推导：伤害取查表结果（中型/血肉 4d6），胜率取对抗检定
    resolved = calc.resolve_build_full(build, calc.load_ops(), calc.load_rage_table())
    tables = calc.load_damage_tables()
    dmg_dice = calc.resolve_damage_table(tables["tbl.泰山压顶"], resolved)
    contest_bonus = resolved.derived.mods["str"] + resolved.derived.prof
    check("泰山压顶按胜率加权", res.total,
          dice_mean(dmg_dice) * opposed_win_probability(contest_bonus, 0, (1, 6)),
          tol=1e-9)


def test_concentration():
    print("\n[21] 专注：持续效果与冲突")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "wizard_l5.build.json"),
                            encoding="utf-8"))
    env = {"target": {"ac": 15}}

    # 施放 → 维持 → 结束：持续效果只在施放之后的回合生效
    strat = {"rounds": [
        {"round": 1, "attacks": 0,
         "save_effects": [{"spell": "sp.灼热金属", "slot_level": 2}]},
        {"round": 2, "attacks": 0},
        {"round": 3, "attacks": 0, "end_concentration": True}]}
    res = calc.evaluate(build, env, strat)
    r1, r2, r3 = (d for _, d in res.per_round)
    check("施放轮 = 维持轮的伤害", r2, r1, tol=1e-9)
    check("结束后不再结算", r3, Fraction(0), tol=1e-9)
    print(f"       施放 {float(r1):.2f}｜维持 {float(r2):.2f}｜结束 {float(r3):.2f}")

    # 冲突：同时维持两个专注应当告警
    conflict = {"rounds": [
        {"round": 1, "attacks": 0,
         "save_effects": [{"spell": "sp.灼热金属", "slot_level": 2}]},
        {"round": 2, "attacks": 0,
         "save_effects": [{"spell": "sp.灼热金属", "slot_level": 2}]}]}
    res2 = calc.evaluate(build, env, conflict)
    if any("专注" in w for w in res2.warnings):
        print("  OK   同时施放两个专注法术会告警")
    else:
        FAILS.append("专注冲突未告警")
        print("  FAIL 专注冲突未告警")


def test_extra_attack():
    print("\n[22] 命中后追加攻击")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    base = {"round": 1, "buffs": ["rage"], "move": {"toward": True, "ft": 25},
            "attacks": 1}

    def run(ac, ops):
        return calc.evaluate(build, {"target": {"ac": ac}},
                             {"rounds": [dict(base, per_attack={"ops": ops})]})

    # 疯狂连击：-5 命中，命中后追加一次攻击（同样 -5）
    for ac, should_win in ((10, True), (15, False), (19, False)):
        plain = run(ac, [])
        crazy = run(ac, ["feat.疯狂连击"])
        won = crazy.total > plain.total
        tag = "OK  " if won == should_win else "FAIL"
        print(f"  {tag} AC{ac}: 普通 {float(plain.total):.3f}｜疯狂连击 {float(crazy.total):.3f}")
        if won != should_win:
            FAILS.append(f"疯狂连击在 AC{ac} 的取舍方向不符")

    # 刺剑大师：-2 命中，本轮所有攻击都命中后追加一次
    build["combat"]["weapon"]["kind"] = "刺剑"
    for ac in (10, 15, 19):
        plain = run(ac, [])
        rapier = run(ac, ["feat.刺剑大师"])
        tag = "OK  " if rapier.total > plain.total else "FAIL"
        print(f"  {tag} AC{ac} 刺剑大师: {float(plain.total):.3f} → {float(rapier.total):.3f}")
        if rapier.total <= plain.total:
            FAILS.append(f"刺剑大师在 AC{ac} 应优于普通攻击")
    build["combat"]["weapon"]["kind"] = "巨斧"

    # 限次专长：使用次数进资源预算，超额要告警
    build["feats"].append({"id": "feat.刺剑大师", "at_level": 1})
    seen = []
    for n in (3, 4):
        rounds = []
        for i in range(1, n + 1):
            r = {"round": i, "buffs": ["rage"], "attacks": 1,
                 "per_attack": {"ops": ["feat.刺剑大师"]}}
            if i == 1:
                r["move"] = {"toward": True, "ft": 25}
            rounds.append(r)
        res = calc.evaluate(build, {"target": {"ac": 15}, "long_rests": 1},
                            {"rounds": rounds})
        over = any("超过" in w for w in res.warnings)
        seen.append(over)
    if seen == [False, True]:
        print("  OK   刺剑大师 3 轮内不超额、4 轮超额告警")
    else:
        FAILS.append(f"限次专长预算校验不符: {seen}")
        print(f"  FAIL 限次专长预算校验 {seen}")

    # 结果树叶子数应因动态插入而增加
    if run(15, ["feat.疯狂连击"]).leaves > run(15, []).leaves:
        print("  OK   动态插入攻击使结果树叶子数增加")
    else:
        FAILS.append("动态插入未影响结果树")
        print("  FAIL 动态插入未影响结果树")


def test_meta_rules():
    print("\n[23] 元规则：按多职业等级授予特性")
    import calculator as calc

    ops = calc.load_ops()
    cases = [(5, 5, 2), (6, 5, 3), (9, 8, 4)]
    for brb, mnk, want in cases:
        b = {"meta": {"level": brb + mnk},
             "classes": [{"id": "class.barbarian", "level": brb},
                         {"id": "class.monk", "level": mnk}],
             "feats": [{"id": "feat.融汇百家"}]}
        got, src = calc.derive_attack_count(b, ops)
        tag = "OK  " if got == want else "FAIL"
        print(f"  {tag} 狂战士{brb}+武僧{mnk}（和 {brb+mnk}）→ {got} 次（{src}）")
        if got != want:
            FAILS.append(f"融汇百家 {brb}+{mnk} 应为 {want} 次")

    # 没有该专长时不应获得额外次数
    b = {"meta": {"level": 11},
         "classes": [{"id": "class.barbarian", "level": 6}, {"id": "class.monk", "level": 5}],
         "feats": []}
    got, _ = calc.derive_attack_count(b, ops)
    if got == 2:
        print("  OK   无「融汇百家」时不跨职业授予")
    else:
        FAILS.append("未选专长却跨职业授予了攻击次数")
        print("  FAIL 未选专长却跨职业授予")


def test_passive_feats():
    print("\n[24] 被动专长自动生效")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    build["combat"]["weapon"]["damage_type"] = "piercing"
    build["choices"] = {"ability_bonus": "str"}
    strat = {"rounds": [{"round": 1, "buffs": ["rage"],
                         "move": {"toward": True, "ft": 25}, "attacks": 1,
                         "per_attack": {"ops": []}}]}

    def run(doc):
        return calc.evaluate(doc, {"target": {"ac": 15}}, strat)

    plain = run(build)
    build["feats"].append({"id": "feat.穿刺者", "at_level": 1})
    pierce = run(build)
    build["feats"].append({"id": "feat.重击大师", "at_level": 1})
    crit = run(build)

    # 穿刺者：每回合一次 +2 穿刺伤害。自动生效，无需策略引用
    if pierce.total > plain.total:
        print(f"  OK   穿刺者自动生效：{float(plain.total):.3f} → {float(pierce.total):.3f}")
    else:
        FAILS.append("被动专长未自动生效")
        print("  FAIL 穿刺者未自动生效")

    # 重击大师：重击范围 +1，期望应再提高
    if crit.total > pierce.total:
        print(f"  OK   重击大师：{float(pierce.total):.3f} → {float(crit.total):.3f}")
    else:
        FAILS.append("重击范围 +1 未提高期望")
        print("  FAIL 重击范围未生效")


def test_opportunity_attacks():
    print("\n[25] 借机攻击体系")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    build = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                            encoding="utf-8"))
    build["feats"] = []
    strat = {"rounds": [{"round": 1, "buffs": ["rage"],
                         "move": {"toward": True, "ft": 25}, "attacks": 1,
                         "per_attack": {"ops": []}}]}

    def run(triggers, doc=None):
        return calc.evaluate(doc or build,
                             {"target": {"ac": 15}, "enemy_triggers_per_round": triggers},
                             strat)

    base = run(0)
    one = run(1)
    many = run(5)
    if one.total > base.total:
        print(f"  OK   触发 1 次：{float(base.total):.3f} → {float(one.total):.3f}")
    else:
        FAILS.append("借机攻击未生效")
        print("  FAIL 借机攻击未生效")

    # 默认预算 1 次，触发再多也只加一次
    check("默认预算封顶", many.total, one.total, tol=1e-9)

    # 坚守阵地按力量调整值追加预算
    build["feats"].append({"id": "feat.坚守阵地", "at_level": 1})
    with_feat = run(5)
    if with_feat.total > many.total:
        print(f"  OK   坚守阵地扩大预算：{float(many.total):.3f} → {float(with_feat.total):.3f}")
    else:
        FAILS.append("借机攻击预算未扩大")
        print("  FAIL 预算未扩大")

    # ---- 触发源：环境说"敌人做了什么"，构筑决定"哪些能做反应" ----
    plain = _json.loads(_json.dumps(build))
    plain["feats"] = []

    def run_env(feats, triggers):
        doc = _json.loads(_json.dumps(plain))
        doc["feats"] = [{"id": f, "at_level": 1} for f in feats]
        return calc.evaluate(doc, {"target": {"ac": 15}, "enemy_triggers": triggers},
                             strat)

    # 敌人施法：没有巫师杀手就借不到机，且必须留下 trace 说明原因
    no_feat = run_env([], {"cast_spell": 3})
    r_wiz = run_env(["feat.巫师杀手"], {"cast_spell": 3})
    check("无解锁专长时施法不引发借机", no_feat.total, run_env([], {}) .total, tol=1e-9)
    if r_wiz.total > no_feat.total:
        print(f"  OK   巫师杀手接住施法触发：{float(no_feat.total):.3f} → {float(r_wiz.total):.3f}")
    else:
        FAILS.append("巫师杀手未接住施法触发")
        print("  FAIL 巫师杀手未接住施法触发")
    if any("缺对应专长" in t for t in no_feat.trace):
        print("  OK   未解锁的触发在 trace 中说明")
    else:
        FAILS.append("未解锁的触发没有 trace 说明")
        print("  FAIL 未解锁的触发没有 trace 说明")

    # 连续借机：单次触发打两击（预算足够时）
    r_combo = run_env(["feat.连续借机"], {"retreat": 1})
    r_single = run_env([], {"retreat": 1})
    if r_combo.total > r_single.total * 1.5:
        print(f"  OK   连续借机单次触发两击：{float(r_single.total):.3f} → {float(r_combo.total):.3f}")
    else:
        FAILS.append("连续借机未打出第二击")
        print("  FAIL 连续借机未打出第二击")

    # 罗比拉战术：拿受伤换借机，代价不进 DPR，必须告警
    r_ro = run_env(["feat.罗比拉战术"], {"attack_you": 1})
    check("罗比拉战术接住「攻击你」触发", r_ro.total, run_env([], {"retreat": 1}).total,
          tol=1e-9)
    if any("罗比拉" in w for w in r_ro.warnings):
        print("  OK   罗比拉战术的代价已告警")
    else:
        FAILS.append("罗比拉战术的代价未告警")
        print("  FAIL 罗比拉战术的代价未告警")


def test_reach_and_speed():
    print("\n[26] 触及与速度")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def build(feats=None, weapon=None, props=None):
        doc = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                              encoding="utf-8"))
        # 范例构筑是骑乘的（坐骑 60 尺），会把速度差抹平——这里改为步行
        doc["combat"].pop("mount", None)
        doc["combat"]["armor"] = "none"
        doc["feats"] = [{"id": f, "at_level": 1} for f in (feats or [])]
        if weapon:
            doc["combat"]["weapon"] = dict(weapon)
        if props is not None:
            doc["combat"]["weapon"]["properties"] = props
        return doc

    def run(doc, move_ft, trig=None):
        strat = {"rounds": [{"round": 1, "buffs": ["rage"],
                             "move": {"toward": True, "ft": move_ft},
                             "attacks": 1, "per_attack": {"ops": []}}]}
        env = {"target": {"ac": 15}}
        if trig is not None:
            env["enemy_triggers"] = trig
        return calc.evaluate(doc, env, strat)

    # 距 30 尺，只前进 20 尺 → 距目标 10 尺：触及 5 尺够不着，触及 15 尺够得着
    near = run(build(), 20)
    check("触及 5 尺时 10 尺外打不到", near.total, 0, tol=1e-9)
    outer = run(build(["feat.斗气外放"]), 20)
    if outer.total > 0:
        print(f"  OK   斗气外放（触及 +10）接上了 10 尺外的目标：{float(outer.total):.3f}")
    else:
        FAILS.append("斗气外放未延长触及")
        print("  FAIL 斗气外放未延长触及")

    # 武器门控：长度优势在巨剑上没有，在长棍上有
    pole = {"name": "长棍", "kind": "长棍", "dice": "1d8",
            "damage_type": "bludgeoning", "enhancement": 0, "ability": "str"}
    sword = dict(pole, name="巨剑", kind="巨剑")
    with_sword = run(build(["feat.长度优势"], sword), 20)
    with_pole = run(build(["feat.长度优势"], pole), 20)
    check("长度优势在巨剑上不生效", with_sword.total, 0, tol=1e-9)
    if with_pole.total > 0:
        print(f"  OK   长度优势在长棍上生效：{float(with_pole.total):.3f}")
    else:
        FAILS.append("长度优势未延长长棍触及")
        print("  FAIL 长度优势未延长长棍触及")
    if any("长度优势" in w for w in with_sword.warnings):
        print("  OK   武器不符时给出告警")
    else:
        FAILS.append("武器门控未告警")
        print("  FAIL 武器门控未告警")

    # 速度直接决定「这轮够不够得着」：目标在 40 尺外，
    # 速度 30 只能前进 30（剩 10 尺，打不到），速度 45 能走满 40（贴身）
    def run_at(doc, dist, move_ft):
        strat = {"rounds": [{"round": 1, "attacks": 1, "per_attack": {"ops": []}}]}
        env = {"target": {"ac": 15}, "initial_distance_ft": dist}
        strat["rounds"][0]["move"] = {"toward": True, "ft": move_ft}
        return calc.evaluate(doc, env, strat)

    check("速度 30 追不上 40 尺外的目标", run_at(build(), 40, 40).total, 0, tol=1e-9)
    fast = run_at(build(["feat.灵活移动"]), 40, 40)
    if fast.total > 0:
        print(f"  OK   灵活移动（45 尺）追上 40 尺外的目标：{float(fast.total):.3f}")
    else:
        FAILS.append("速度加成未影响可达性")
        print("  FAIL 速度加成未影响可达性")

    # 职业特性按**本职业等级**分段（一个特性只有一个算子，分段表放在字段里）
    doc = build()
    doc["classes"] = [{"id": "class.monk", "level": 2, "subclass": None},
                      {"id": "class.barbarian", "level": 5, "subclass": None}]
    strat = {"rounds": [{"round": 1, "attacks": 1, "per_attack": {"ops": []}}]}
    monk2 = calc.evaluate(doc, {"target": {"ac": 15}}, strat)
    doc["classes"][0]["level"] = 10
    monk10 = calc.evaluate(doc, {"target": {"ac": 15}}, strat)
    if any("速度 40 尺" in t for t in monk2.trace):
        print("  OK   武僧 2 级：30 + 无甲移动 10 = 40 尺")
    else:
        FAILS.append("武僧 2 级无甲移动未生效")
        print("  FAIL 武僧 2 级无甲移动未生效")
    if any("速度 50 尺" in t for t in monk10.trace):
        print("  OK   武僧 10 级：无甲移动升到 +20 → 50 尺")
    else:
        FAILS.append("无甲移动未按等级分段")
        print("  FAIL 无甲移动未按等级分段")


def test_adv_scaling():
    print("\n[27] 按优势层数结算")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                          encoding="utf-8"))
    base["combat"].pop("mount", None)
    base["combat"]["armor"] = "none"

    def run(feats, adv, weapon):
        doc = _json.loads(_json.dumps(base))
        doc["feats"] = [{"id": f, "at_level": 1} for f in feats]
        doc["combat"]["weapon"] = dict(weapon)
        strat = {"rounds": [{"round": 1, "move": {"toward": True, "ft": 25},
                             "attacks": 1,
                             "per_attack": {"ops": [], "adv_tiers": adv}}]}
        return float(calc.evaluate(doc, {"target": {"ac": 15}}, strat).total)

    scythe = {"name": "巨镰", "kind": "巨镰", "dice": "2d4",
              "damage_type": "slashing", "enhancement": 0, "ability": "str",
              "properties": ["双手"]}
    # 二重以下不给：2 重才开始 +2 重击范围
    check("死亡收割在 1 重优势不生效",
          run(["feat.死亡收割"], 1, scythe), run([], 1, scythe), tol=1e-9)
    d2 = run([], 2, scythe)
    d2f = run(["feat.死亡收割"], 2, scythe)
    d3f = run(["feat.死亡收割"], 3, scythe)
    d4f = run(["feat.死亡收割"], 4, scythe)
    if d2f > d2 and d3f > d2f and d4f > d3f:
        print(f"  OK   2/3/4 重优势递进：{d2:.3f} → {d2f:.3f} → {d3f:.3f} → {d4f:.3f}")
    else:
        FAILS.append("死亡收割未按优势层数递进")
        print("  FAIL 死亡收割未按优势层数递进")

    # 每重优势 +1d10（阵地狙击）
    bow = {"name": "巨弓", "kind": "巨弓", "dice": "1d8",
           "damage_type": "piercing", "enhancement": 0, "ability": "str",
           "properties": ["弹药", "双手"]}
    b1 = run(["feat.阵地狙击"], 1, bow)
    b2 = run(["feat.阵地狙击"], 2, bow)
    if b2 - b1 > 4:                      # 1d10 期望 5.5，扣命中率后仍应 > 4
        print(f"  OK   阵地狙击每重优势 +1d10：{b1:.3f} → {b2:.3f}")
    else:
        FAILS.append("阵地狙击未按层数叠加骰")
        print("  FAIL 阵地狙击未按层数叠加骰")

    # 武器不符（拿巨剑）时两条都不生效
    sword = dict(scythe, name="巨剑", kind="巨剑", dice="2d6")
    check("死亡收割在巨剑上不生效",
          run(["feat.死亡收割"], 4, sword), run([], 4, sword), tol=1e-9)


def test_sneak_attack():
    print("\n[28] 偷袭")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                          encoding="utf-8"))
    base["combat"].pop("mount", None)
    base["combat"]["armor"] = "none"
    base["feats"] = []
    # 偷袭测试拿狂战士当"无偷袭"的对照组，不能把别的职业的战斗风格带进来
    base["choices"] = {}

    def run(level, adv, ally=False, attacks=1, rogue=True):
        doc = _json.loads(_json.dumps(base))
        doc["classes"] = ([{"id": "class.rogue", "level": level, "subclass": None}]
                          if rogue else [{"id": "class.barbarian", "level": 5,
                                          "subclass": None}])
        strat = {"rounds": [{"round": 1, "move": {"toward": True, "ft": 25},
                             "attacks": attacks,
                             "per_attack": {"ops": [], "adv_tiers": adv}}]}
        env = {"target": {"ac": 15}, "ally_within_5ft_of_target": ally}
        return float(calc.evaluate(doc, env, strat).total)

    plain = run(5, 0, rogue=False)
    # 1 级盗贼还没有「决斗」：没有优势、没有友方，就吃不到偷袭
    check("无优势无友方（1 级）不偷袭", run(1, 0), run(1, 0, rogue=False), tol=1e-9)
    if run(1, 1) > run(1, 1, rogue=False):
        print(f"  OK   优势触发偷袭：{float(run(1, 1, rogue=False)):.3f} → {float(run(1, 1)):.3f}")
    else:
        FAILS.append("优势未触发偷袭")
        print("  FAIL 优势未触发偷袭")
    if run(1, 0, ally=True) > run(1, 0, rogue=False):
        print("  OK   友方夹击触发偷袭")
    else:
        FAILS.append("友方夹击未触发偷袭")
        print("  FAIL 友方夹击未触发偷袭")

    # 2 级起「决斗」：贴身单挑即可偷袭
    if run(2, 0) > run(1, 0):
        print("  OK   「决斗」让贴身无优势也能偷袭")
    else:
        FAILS.append("决斗未接到偷袭")
        print("  FAIL 决斗未接到偷袭")

    # 每回合一次：两击的偷袭增量不应翻倍
    one = run(1, 1) - run(1, 1, rogue=False)
    two = run(1, 1, attacks=2) - run(1, 1, attacks=2, rogue=False)
    if one < two < one * 1.6:
        print(f"  OK   每回合一次：一击增量 {one:.3f}，两击增量 {two:.3f}（未翻倍）")
    else:
        FAILS.append("偷袭未按每回合一次结算")
        print(f"  FAIL 偷袭每回合一次的判定异常：{one:.3f} vs {two:.3f}")

    # 骰式按职业等级成长
    if run(19, 1) > run(11, 1) > run(1, 1):
        print("  OK   偷袭骰随等级成长（1/11/19 级）")
    else:
        FAILS.append("偷袭骰未随等级成长")
        print("  FAIL 偷袭骰未随等级成长")


def test_martial_arts():
    print("\n[29] 武艺")
    import json as _json
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                          encoding="utf-8"))
    base["combat"].pop("mount", None)
    base["combat"]["armor"] = "none"
    base["feats"] = []
    fist = {"name": "徒手", "kind": "徒手", "dice": "1d4",
            "damage_type": "bludgeoning", "enhancement": 0, "ability": "str"}

    def run(cls, level, weapon):
        doc = _json.loads(_json.dumps(base))
        doc["classes"] = [{"id": cls, "level": level, "subclass": None}]
        doc["combat"]["weapon"] = dict(weapon)
        strat = {"rounds": [{"round": 1, "move": {"toward": True, "ft": 25},
                             "per_attack": {"ops": []}}]}
        r = calc.evaluate(doc, {"target": {"ac": 15}}, strat)
        return r

    m1 = run("class.monk", 1, fist)
    m11 = run("class.monk", 11, fist)
    if any("附赠动作攻击 +1" in t for t in m1.trace):
        print("  OK   武艺给出 1 次附赠徒手打击")
    else:
        FAILS.append("武艺未给出附赠徒手打击")
        print("  FAIL 武艺未给出附赠徒手打击")

    if any("1d4 → 1d8" in t for t in m11.trace):
        print("  OK   11 级武艺骰升到 1d8")
    else:
        FAILS.append("武艺骰未随等级成长")
        print("  FAIL 武艺骰未随等级成长")

    if m11.total > m1.total:
        print(f"  OK   11 级伤害 {float(m11.total):.3f} > 1 级 {float(m1.total):.3f}")
    else:
        FAILS.append("武艺骰成长未反映到伤害")
        print("  FAIL 武艺骰成长未反映到伤害")

    # 重型武器不受武艺影响
    great = {"name": "巨剑", "kind": "巨剑", "dice": "2d6",
             "damage_type": "slashing", "enhancement": 0, "ability": "str",
             "properties": ["双手", "重型"]}
    mg = run("class.monk", 1, great)
    if any("被替换" in t for t in mg.trace):
        FAILS.append("武艺错误地改写了重型武器骰")
        print("  FAIL 武艺错误地改写了重型武器骰")
    else:
        print("  OK   重型武器不受武艺骰影响")


def test_build_check():
    print("")
    print("[30] 构筑校验")

    import json as _json

    import build_check as bc
    import calculator as calc

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = _json.load(open(os.path.join(root, "examples", "barbarian_l5.build.json"),
                           encoding="utf-8"))
    ops = calc.load_ops()

    def check_codes(doc, expect_error=None, expect_warn=None, label=""):
        rep = bc.check_build(doc, ops)
        errs = [i["code"] for i in rep.errors()]
        warns = [i["code"] for i in rep.warnings()]
        ok = True
        if expect_error and not any(expect_error in c for c in errs):
            ok = False
        if expect_warn and not any(expect_warn in c for c in warns):
            ok = False
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        if not ok:
            FAILS.append("构筑校验未捕获：" + label)
            print("        errors=" + str(errs) + " warnings=" + str(warns))
        return rep

    doc = _json.loads(_json.dumps(base))
    doc["classes"][0]["id"] = "class.bardd"
    check_codes(doc, expect_error="classes", label="职业 id 拼错 → ERROR")

    doc = _json.loads(_json.dumps(base))
    doc["meta"]["level"] = 9
    check_codes(doc, expect_error="meta.level", label="总等级与职业等级之和不一致 → ERROR")

    doc = _json.loads(_json.dumps(base))
    doc["scores"]["base"]["strengh"] = 17
    check_codes(doc, expect_error="scores.base.strengh", label="属性键拼错 → ERROR")

    doc = _json.loads(_json.dumps(base))
    doc["classes"][0]["subclass"] = "path.punisher"
    check_codes(doc, expect_warn="subclass", label="子职名与数据对不上 → WARN")

    doc = _json.loads(_json.dumps(base))
    doc["choices"] = {"fighting_stile": "对决"}
    check_codes(doc, expect_error="choices.fighting_stile", label="选择键拼错 → ERROR")

    doc = _json.loads(_json.dumps(base))
    doc["choices"] = {"fighting_style": "冲锋陷阵"}
    check_codes(doc, expect_warn="choices.fighting_style", label="选项不存在 → WARN")

    doc = _json.loads(_json.dumps(base))
    doc["combat"]["weapon"]["properties"] = ["重形"]
    check_codes(doc, expect_warn="combat.weapon.properties", label="武器属性拼错 → WARN")

    doc = _json.loads(_json.dumps(base))
    doc["feats"].append({"id": "feat.威力攻擊", "at_level": 1})
    check_codes(doc, expect_error="feats[", label="专长 id 拼错 → ERROR")

    doc = _json.loads(_json.dumps(base))
    doc["feats"].append({"id": "feat.圣堂审判官", "at_level": 1})
    check_codes(doc, expect_warn="prerequisite", label="先决条件未满足 → WARN")

    rep = bc.check_build(_json.loads(_json.dumps(base)), ops)
    if rep.ok():
        print("  OK   范例构筑无 ERROR")
    else:
        FAILS.append("范例构筑报了 ERROR")
        print("  FAIL 范例构筑报了 ERROR：" + str([i["code"] for i in rep.errors()]))


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
    test_tables_and_formulas()
    test_opposed_check()
    test_concentration()
    test_extra_attack()
    test_meta_rules()
    test_passive_feats()
    test_opportunity_attacks()
    test_reach_and_speed()
    test_adv_scaling()
    test_sneak_attack()
    test_martial_arts()
    test_build_check()
    print("\n" + "=" * 58)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
