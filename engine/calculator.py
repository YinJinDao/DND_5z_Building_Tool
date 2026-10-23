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
import sys
from dataclasses import dataclass, field
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from character import Bonus, Build, Contribution, derive
from evaluator import Attack, DamageComponent, TargetProfile, expected_damage
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

@dataclass
class Result:
    per_round: list
    total: Fraction
    leaves: int
    trace: list


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

    trace = list(resolved.trace) + [f"目标 AC {target.ac}"]
    per_round = []

    for rnd in strategy_doc.get("rounds", []):
        buffs = set(rnd.get("buffs", []))
        attacks_n = int(rnd.get("attacks", 1))
        per_attack = rnd.get("per_attack", {})
        op_ids = per_attack.get("ops", [])
        adv = int(per_attack.get("adv_tiers", 0))

        attacks, all_rules = [], []
        for i in range(attacks_n):
            a = build_base_attack(resolved, adv_tiers=adv,
                                  rage_active="rage" in buffs)
            for oid in op_ids:
                if oid not in ops:
                    raise KeyError(f"未知算子: {oid}（检查 形式化/ops/）")
                a, rs = apply_op(ops[oid], a, resolved, attack_index=i)
                all_rules.extend(rs)
            attacks.append(a)

        dmg, leaves = evaluate_turn(attacks, all_rules, target)
        per_round.append((rnd.get("round", len(per_round) + 1), dmg))
        trace.append(f"第 {rnd.get('round')} 轮：{attacks_n} 次攻击"
                     f"{'（狂暴）' if 'rage' in buffs else ''}"
                     f"{'，' + '、'.join(op_ids) if op_ids else ''} → {float(dmg):.3f}")

    total = sum((d for _, d in per_round), Fraction(0))
    rounds = len(per_round)
    return Result(per_round=per_round, total=total, leaves=leaves, trace=trace)


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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
