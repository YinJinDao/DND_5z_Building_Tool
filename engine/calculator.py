#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伤害计算器：构筑 + 输出策略 + 输出环境 → 期望伤害。

这是**一期（伤害计算器）**的完整内部构造。三份输入均为固定格式，
格式定义见 `形式化/输入格式.md`。

内部流程：

    Build ──derive──► Derived（属性/熟练/AC/HP/豁免/攻击加值）
      │                    │
      │                    └── + Environment.target ──► 命中率/重击率
      └── combat.weapon ──► 基础攻击 ──apply ops──► Attack
                                          │
    Strategy ──每轮 buffs/攻击次数/每击修饰──►  [Attack, ...]
                                          │
                                          └──evaluate_turn──► 每轮期望伤害
                                                                  │
                                                          rounds 求和

算子从 `形式化/ops/*.json` 读取，不写死在代码里——规则变更只改数据。
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from character import Bonus, Build, Contribution, derive
from combat import (Budget, CombatState, RangeProfile, ResourcePool,
                    expected_save_damage)
from evaluator import (Attack, DamageComponent, TargetProfile,
                       apply_target_pipeline, dice_mean, expected_damage)
from strategy import Rule, evaluate_turn

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS_DIR = os.path.join(REPO, "形式化", "ops")


# ---------------------------------------------------------------- 输入装载

def load_ops(ops_dir=OPS_DIR):
    """读取算子目录。id 与文件名去扩展名后一致。"""
    ops = {}
    if not os.path.isdir(ops_dir):
        return ops
    for fname in sorted(os.listdir(ops_dir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(ops_dir, fname), encoding="utf-8") as fh:
            op = json.load(fh)
        ops[op["id"]] = op
    return ops


def _bonus_dict(src):
    """规范化「属性 → 加值」字典。"""
    return {k: int(v) for k, v in (src or {}).items()}


def load_rage_table(class_name="狂战士", path=None):
    """从 class_tables.jsonl 读取等级 → 狂暴伤害加值。不写死在代码里。"""
    path = path or os.path.join(REPO, "形式化", "data", "class_tables.jsonl")
    table = {}
    if not os.path.isfile(path):
        return table
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("class") != class_name:
                continue
            raw = row.get("columns", {}).get("狂暴伤害加值")
            if raw:
                table[str(row["level"])] = int(str(raw).replace("+", "").strip())
    return table


# ---------------------------------------------------------------- 构筑 → 派生

@dataclass
class ResolvedBuild:
    raw: dict
    build: Build
    derived: object
    weapon: dict
    rage_bonus: int
    trace: list = field(default_factory=list)


def resolve_build(build_doc, rage_table=None):
    """把构筑文档解析成可计算的派生值。"""
    meta = build_doc.get("meta", {})
    combat = build_doc.get("combat", {})
    scores = build_doc.get("scores", {})

    level = int(meta.get("level") or sum(c.get("level", 0)
                                         for c in build_doc.get("classes", [])) or 1)
    b = Build(
        level=level,
        base_scores=_bonus_dict(scores.get("base")),
        racial=_bonus_dict(scores.get("racial")),
        level_ups=_bonus_dict(scores.get("level_ups")),
        feat_bonus=_bonus_dict(scores.get("feat")),
        item_bonus=_bonus_dict(scores.get("item")),
        hit_die_faces=int(combat.get("hit_die_faces", 8)),
        armor=combat.get("armor", "none"),
        shield=bool(combat.get("shield", False)),
        proficient_saves=tuple(combat.get("proficient_saves", ())),
        weapon_attack_ability=combat.get("weapon", {}).get("ability", "str"),
        weapon_enhancement=int(combat.get("weapon", {}).get("enhancement", 0)),
        casting_ability=combat.get("casting_ability"),
    )
    d = derive(b)

    weapon = {
        "dice": combat.get("weapon", {}).get("dice", "1d4"),
        "damage_type": combat.get("weapon", {}).get("damage_type", "bludgeoning"),
        "enhancement": int(combat.get("weapon", {}).get("enhancement", 0)),
    }
    # 狂暴伤害加值取自职业表；是否生效由策略的 buffs 决定
    rage_bonus = int((rage_table or {}).get(str(level), 0))

    trace = [f"等级 {level}", f"熟练加值 +{d.prof}",
             f"AC {d.ac}", f"HP {d.hp}",
             f"攻击加值：{d.attack.explain()}"]
    if d.spell_attack is not None:
        trace.append(f"法术攻击加值：{d.spell_attack.explain()}｜"
                     f"法术豁免 DC {d.spell_dc}")
    return ResolvedBuild(raw=build_doc, build=b, derived=d, weapon=weapon,
                         rage_bonus=rage_bonus, trace=trace)


# ---------------------------------------------------------------- 算子应用

def _max_die(weapon_dice):
    """'1d12' → 12。威力攻击的加值取基础伤害骰最大值。"""
    try:
        return int(str(weapon_dice).lower().split("d")[1])
    except (IndexError, ValueError):
        return 0


def _eval_expr(expr, resolved):
    """算子表达式求值。当前只支持少量具名表达。"""
    if isinstance(expr, (int, float)):
        return int(expr)
    e = str(expr).strip()
    if e == "max_die(base_damage)":
        return _max_die(resolved.weapon["dice"])
    if e.isdigit():
        return int(e)
    raise ValueError(f"不支持的算子表达式: {expr!r}（可在 calculator._eval_expr 扩展）")


def build_base_attack(resolved, adv_tiers=0, rage_active=False):
    """武器基础攻击：武器骰 + 属性调整值 + 武器增强 + 狂暴加值。"""
    d = resolved.derived
    flat = d.mods["str"] + resolved.weapon["enhancement"]
    components = [DamageComponent(resolved.weapon["dice"], flat=flat,
                                  dtype=resolved.weapon["damage_type"],
                                  slot="weapon+str+enhancement")]
    if rage_active and resolved.rage_bonus:
        components.append(DamageComponent("0", flat=resolved.rage_bonus,
                                          dtype=resolved.weapon["damage_type"],
                                          slot="class.rage"))
    return Attack(attack_bonus=d.attack.total, adv_tiers=adv_tiers,
                  components=components)


def apply_op(op, attack, resolved, attack_index=0):
    """把一个算子作用到攻击上。返回 (attack, 额外的规则列表)。"""
    proj = op.get("projection", {})
    rules = []

    if "attack_bonus" in proj:
        attack.attack_bonus += int(proj["attack_bonus"])

    if "adv_tiers" in proj:
        attack.adv_tiers += int(proj["adv_tiers"])

    for slot in proj.get("damage_slots", []):
        if slot.get("applies_on", "on_hit") not in ("on_hit", "always"):
            continue
        value = _eval_expr(slot["expr"], resolved)
        cap = slot.get("cap")
        for step in slot.get("cap_scaling", []):
            if resolved.derived.level >= step["level"]:
                cap = step["cap"]
        if cap is not None:
            value = min(value, int(cap))
        dtype = slot.get("type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        attack.components.append(
            DamageComponent("0", flat=value, dtype=dtype, slot=op["id"]))

    for extra in proj.get("extra_dice", []):
        dice = extra["dice"]
        for step in extra.get("dice_scaling", []):
            if resolved.derived.level >= step["level"]:
                dice = step["dice"]
        dtype = extra.get("type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        comp = DamageComponent(dice, flat=int(extra.get("flat", 0)), dtype=dtype,
                               slot=op["id"],
                               crit_doubles=bool(extra.get("crit_doubles", True)))
        if extra.get("limit") == "每回合一次":
            # 每回合一次：只有本回合第一次攻击享有，其余攻击靠结果树分支处理
            rules.append(Rule(trigger="hit_or_crit", on_attack=attack_index,
                              extra_dice=[comp], name=op["id"]))
        else:
            attack.components.append(comp)
    return attack, rules


# ---------------------------------------------------------------- 主计算

# ---------------------------------------------------------------- 法术

def _split_dice(expr):
    """'8d6' → (8, 6)；'3' → (0, 0, 3)。"""
    s = str(expr).strip().lower()
    if "d" in s:
        n, _, faces = s.partition("d")
        return int(n or 1), int(faces)
    return 0, 0


def _add_dice(base, extra, times=1):
    """把 extra 叠加 times 次到 base 上。'8d6' + '2d6'×2 → '12d6'。"""
    n1, f1 = _split_dice(base)
    n2, f2 = _split_dice(extra)
    if f1 and f2 and f1 != f2:
        raise ValueError(f"骰面不一致，无法合并: {base} + {extra}")
    faces = f1 or f2
    return f"{n1 + n2 * times}d{faces}"


def spell_dice(op, char_level, slot_level=None):
    """按角色等级应用 scaling，按法术位应用 upcast，返回最终骰式。

    scaling 一律写在算子**顶层**。早期把攻击型法术的 scaling 写进了
    `attack` 内，导致等级成长静默失效（5 级仍算 1d8），现统一到顶层，
    并在此对嵌套位置做一次兼容读取，避免旧数据静默出错。
    """
    scaling = op.get("scaling") or (op.get("attack") or {}).get("scaling") or []
    if "attack" in op:
        base = op["attack"]["damage"]["dice"]
    else:
        base = op["damage"]["dice"]

    for step in scaling:
        if char_level >= step["level"]:
            base = step["dice"]

    upcast = op.get("upcast")
    if upcast and slot_level and slot_level > upcast["per_slot_above"]:
        base = _add_dice(base, upcast["add_dice"],
                         slot_level - upcast["per_slot_above"])
    return base


def build_spell_attack(resolved, op, adv_tiers=0, slot_level=None):
    """攻击型法术（如冻寒之触）：法术攻击加值 + 法术伤害骰。"""
    if resolved.derived.spell_attack is None:
        raise ValueError(f"构筑未声明 casting_ability，无法施放 {op['id']}")
    spec = op["attack"]
    dice = spell_dice(op, resolved.derived.level, slot_level)
    comp = DamageComponent(dice, flat=0,
                           dtype=spec["damage"]["damage_type"],
                           slot=op["id"])
    return Attack(attack_bonus=resolved.derived.spell_attack.total,
                  adv_tiers=adv_tiers, components=[comp])


def save_effect_from_op(resolved, op, target, slot_level=None):
    """豁免型法术的期望伤害。DC 取自角色，豁免加值取自目标。"""
    if resolved.derived.spell_dc is None:
        raise ValueError(f"构筑未声明 casting_ability，无法施放 {op['id']}")
    spec = op.get("save") or {}
    ability = spec.get("ability", "dex")
    dice = spell_dice(op, resolved.derived.level, slot_level)
    amount = dice_mean(dice)
    half = bool(op.get("damage", {}).get("half_on_save", False))
    save_bonus = int(getattr(target, "saves", {}).get(ability, 0))
    exp = expected_save_damage(resolved.derived.spell_dc, save_bonus, amount,
                               half_on_save=half)
    dtype = op["damage"]["damage_type"]
    dmg, _ = apply_target_pipeline(exp, dtype, target)
    return dmg, dice


@dataclass
class Result:
    per_round: list
    total: Fraction
    leaves: int
    trace: list
    spend_report: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def load_budget(build_doc, env_doc):
    """从构筑的 resources 声明 + 环境的短休次数构造资源预算。"""
    pools = {}
    for pid, spec in (build_doc.get("resources") or {}).items():
        pools[pid] = ResourcePool(
            id=pid,
            name=spec.get("name", pid),
            capacity=int(spec.get("capacity", 0)),
            recharge=spec.get("recharge", "long_rest"),
            interval=int(spec.get("interval", 1)),
            die_faces=spec.get("die_faces"),
            unlimited_from_level=spec.get("unlimited_from_level"),
            derived_from=tuple(spec["derived_from"]) if spec.get("derived_from") else None,
            per_circle=spec.get("per_circle"),
        )
    level = int(build_doc.get("meta", {}).get("level", 1))
    return Budget(pools=pools, short_rests=int(env_doc.get("short_rests", 0)),
                  long_rests=int(env_doc.get("long_rests", 1)), level=level)


RANGE_RE = re.compile(r"(\d+)\s*尺")


def _range_ft(text):
    """'120尺' → 120；'自身' → None（自身法术无需距离判定）。"""
    m = RANGE_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def _save_effect_damage(effect, target, resolved):
    """豁免型效果的单次期望伤害。"""
    dc = int(effect["dc"])
    save_bonus = int(getattr(target, "saves", {}).get(effect.get("save", "dex"), 0))
    amount = dice_mean(effect["dice"])
    half = bool(effect.get("half_on_save", False))
    exp = expected_save_damage(dc, save_bonus, amount, half_on_save=half)
    dtype = effect.get("damage_type", "force")
    dmg, _ = apply_target_pipeline(exp, dtype, target)
    return dmg


def evaluate(build_doc, env_doc, strategy_doc, ops=None, rage_table=None):
    """三份输入 → 期望伤害。"""
    ops = ops or load_ops()
    if rage_table is None:
        rage_table = load_rage_table()
    resolved = resolve_build(build_doc, rage_table)

    target_doc = env_doc.get("target", {})
    target = TargetProfile(
        ac=int(target_doc.get("ac", 10)),
        resist=tuple(target_doc.get("resist", ())),
        vuln=tuple(target_doc.get("vuln", ())),
        immune=tuple(target_doc.get("immune", ())),
        absorb=tuple(target_doc.get("absorb", ())),
        pre_reduction=target_doc.get("pre_reduction", {}),
        post_reduction=target_doc.get("post_reduction", {}),
    )
    # 豁免加值挂在目标上，供豁免型效果使用
    target.saves = dict(target_doc.get("saves", {}))

    combat = build_doc.get("combat", {})
    weapon_reach = int(combat.get("weapon", {}).get("reach_ft", 5))
    weapon_range = combat.get("weapon", {}).get("range_ft")
    range_profile = RangeProfile(reach_ft=weapon_reach, range_ft=weapon_range)

    state = CombatState(distance_ft=int(env_doc.get("initial_distance_ft", 30)),
                        speed_ft=int(combat.get("speed_ft", 30)))
    budget = load_budget(build_doc, env_doc)

    trace = list(resolved.trace) + [
        f"目标 AC {target.ac}",
        f"初始距离 {state.distance_ft} 尺｜速度 {state.speed_ft} 尺"
        f"｜触及 {weapon_reach} 尺" + (f"｜射程 {weapon_range} 尺" if weapon_range else ""),
    ]
    warnings = []
    per_round = []
    spends = []

    for rnd in strategy_doc.get("rounds", []):
        rnum = rnd.get("round", len(per_round) + 1)
        buffs = set(rnd.get("buffs", []))

        # 资源消耗
        for pid, amount in (rnd.get("spends") or {}).items():
            spends.append((rnum, pid, int(amount)))

        # 每轮重置移动力与槽位
        if per_round:
            state.next_round()
        else:
            state.start_turn()

        mv = rnd.get("move")
        if mv:
            moved = state.move(int(mv.get("ft", 0)), toward=bool(mv.get("toward", True)))
            trace.append(f"第 {rnum} 轮移动 {moved} 尺 → 距离 {state.distance_ft} 尺")

        per_attack = rnd.get("per_attack", {})
        op_ids = per_attack.get("ops", [])
        adv = int(per_attack.get("adv_tiers", 0))

        kind = per_attack.get("kind", "weapon")
        spell_id = per_attack.get("spell")
        slot_level = per_attack.get("slot_level")

        # 距离判定要用**本动作自己的**射程：武器用触及/射程，法术用该法术的施法距离
        if kind == "spell_attack" and spell_id in ops:
            spell_range = _range_ft(ops[spell_id].get("range"))
            profile = RangeProfile(reach_ft=10 ** 6, range_ft=spell_range)
        else:
            profile = range_profile
        in_range = profile.can_reach(state.distance_ft)

        attacks_n = int(rnd.get("attacks", 1))
        if attacks_n and not in_range:
            warnings.append(f"第 {rnum} 轮距离 {state.distance_ft} 尺超出触及/射程，本回合无攻击")
            attacks_n = 0

        attacks, all_rules = [], []
        for i in range(attacks_n):
            if kind == "spell_attack":
                if spell_id not in ops:
                    raise KeyError(f"未知法术算子: {spell_id}")
                a = build_spell_attack(resolved, ops[spell_id], adv_tiers=adv,
                                       slot_level=slot_level)
            else:
                a = build_base_attack(resolved, adv_tiers=adv,
                                      rage_active="rage" in buffs)
            for oid in op_ids:
                if oid not in ops:
                    raise KeyError(f"未知算子: {oid}（检查 形式化/ops/）")
                a, rs = apply_op(ops[oid], a, resolved, attack_index=i)
                all_rules.extend(rs)
            attacks.append(a)

        dmg, leaves = (evaluate_turn(attacks, all_rules, target) if attacks
                       else (Fraction(0), 0))

        # 豁免型效果：可按法术算子 id 引用，也可内联写明
        for eff in rnd.get("save_effects", []):
            if "spell" in eff:
                sid = eff["spell"]
                if sid not in ops:
                    raise KeyError(f"未知法术算子: {sid}")
                srng = _range_ft(ops[sid].get("range"))
                if srng is not None and state.distance_ft > srng:
                    warnings.append(
                        f"第 {rnum} 轮 {ops[sid]['name']} 距离 {state.distance_ft} 尺"
                        f"超出施法距离 {srng} 尺，未计入")
                    continue
                sd, dice = save_effect_from_op(resolved, ops[sid], target,
                                               eff.get("slot_level"))
                trace.append(f"第 {rnum} 轮 {ops[sid]['name']}（{dice}，豁免）"
                             f" → {float(sd):.3f}")
            else:
                sd = _save_effect_damage(eff, target, resolved)
                trace.append(f"第 {rnum} 轮豁免效果 {eff.get('dice')} → {float(sd):.3f}")
            dmg += sd

        per_round.append((rnum, dmg))
        trace.append(f"第 {rnum} 轮：{attacks_n} 次攻击"
                     f"{'（狂暴）' if 'rage' in buffs else ''}"
                     f"{'，' + '、'.join(op_ids) if op_ids else ''} → {float(dmg):.3f}")

    total = sum((d for _, d in per_round), Fraction(0))
    budget_errors = budget.check_schedule(spends) if spends else []
    return Result(per_round=per_round, total=total, leaves=leaves, trace=trace,
                  spend_report=budget.spend_report(spends) if spends else [],
                  warnings=warnings + budget_errors)


# ---------------------------------------------------------------- CLI

def main(argv):
    if len(argv) < 4:
        print("用法: python -I engine/calculator.py <build.json> <env.json> <strategy.json>")
        return 2
    docs = []
    for path in argv[1:4]:
        with open(path, encoding="utf-8") as fh:
            docs.append(json.load(fh))
    res = evaluate(*docs)
    print("=" * 60)
    print("输入追溯")
    print("=" * 60)
    for line in res.trace:
        print("  " + line)
    print("=" * 60)
    print(f"期望伤害合计: {float(res.total):.3f}")
    if res.per_round:
        print(f"平均每轮:     {float(res.total / len(res.per_round)):.3f}")
    print(f"结果树叶子:   {res.leaves}")
    if res.spend_report:
        print("资源消耗:")
        for line in res.spend_report:
            print("  " + line)
    if res.warnings:
        print("警告:")
        for w in res.warnings:
            print("  ! " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
