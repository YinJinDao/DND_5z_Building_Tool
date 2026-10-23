#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算子自动对拍：每个算子装上/卸下，伤害是否按预期变化。

用法：
    python verify_ops.py [--kind feat|maneuver|spell] [--verbose]

**这是多 agent 协作编译的验收闸门。** 人肉算数无法规模化——N 个 agent
编完之后无法逐条手算；而"装了算子数值没变"恰恰是这类工作最常见的故障：
字段名写错、条件永不满足、投影不被引擎消费，全部表现为**静默无效**。

判定：
  CHANGED   装上后期望伤害变化 —— 正常
  INERT     装有卸下数值相同 —— 需人工确认（可能是纯防御/控制/资源类）
  ERROR     求值异常 —— 必须修复
"""

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "engine"))

import calculator as calc  # noqa: E402

# 通用基线构筑：不含任何会干扰对照的专长，属性取偶数避免调整值边界
BASE_BUILD = {
    "meta": {"rules_version": "1.63", "level": 5},
    "classes": [{"id": "class.barbarian", "level": 5, "subclass": None}],
    "feats": [],
    "maneuvers": {"known": [], "prepared": []},
    "spells": {"known": [], "prepared": []},
    "items": [],
    "scores": {"base": {"str": 16, "dex": 14, "con": 14,
                        "int": 12, "wis": 12, "cha": 10}},
    "choices": {"ability_bonus": "str"},
    "variants": [],
    "resources": {"superiority": {"name": "卓越骰", "capacity": 5,
                                  "recharge": "short_rest", "die_faces": 6}},
    "combat": {
        "weapon": {"name": "巨剑", "kind": "巨剑", "dice": "2d6",
                   "damage_type": "slashing", "enhancement": 0, "ability": "str"},
        "armor": "none", "shield": False, "hit_die_faces": 12,
        "proficient_saves": ["str", "con"],
        "casting_ability": "int", "speed_ft": 30, "size": "medium",
        "material": "血肉",
    },
}
ENV = {"target": {"ac": 15, "saves": {"str": 0, "dex": 0, "con": 0,
                                      "int": 0, "wis": 0, "cha": 0}},
       "initial_distance_ft": 5}


def scenario(op):
    """为每个算子构造能触发它的最小场景。"""
    kind = op.get("kind")
    build = json.loads(json.dumps(BASE_BUILD))
    if kind == "feat":
        build["feats"] = [{"id": op["id"], "at_level": 1}]
        strat = {"rounds": [{"round": 1, "buffs": ["rage"], "attacks": 1,
                             "per_attack": {"ops": []}}]}
    elif kind == "maneuver":
        strat = {"rounds": [{"round": 1, "buffs": ["rage"], "attacks": 1,
                             "per_attack": {"ops": [op["id"]]}}]}
    elif kind == "spell":
        if "save" in op:
            strat = {"rounds": [{"round": 1, "attacks": 0,
                                 "save_effects": [{"spell": op["id"],
                                                   "slot_level": op.get("level") or 1}]}]}
        elif "attack" in op:
            strat = {"rounds": [{"round": 1, "attacks": 1,
                                 "per_attack": {"kind": "spell_attack",
                                                "spell": op["id"],
                                                "slot_level": op.get("level") or 0}}]}
        else:
            return None
    elif kind == "class_feature":
        # 职业特性靠等级授予，基线构筑没有对应职业时无法触发
        return None
    else:
        return None
    return build, strat


def scenario_env(op):
    """有些算子只有在特定环境事件下才可能有收益（借机攻击系）——
    不给它那个事件，对拍结果会是 INERT，看起来像缺陷，其实只是场景没搭。"""
    env = json.loads(json.dumps(ENV))
    proj = op.get("projection") or {}
    srcs = proj.get("opportunity_trigger_source") or []
    if (proj.get("opportunity_attack_budget_add") or srcs
            or proj.get("opportunity_attack_adv")):
        env["enemy_triggers"] = {s: 3 for s in (srcs or ["retreat"])}
    return env


def evaluate(build, strat, env=None):
    return float(calc.evaluate(build, env or ENV, strat).total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    ops = []
    for p in sorted(glob.glob(os.path.join(REPO, "形式化", "ops", "*.json"))):
        op = json.load(open(p, encoding="utf-8"))
        if args.kind and op.get("kind") != args.kind:
            continue
        ops.append(op)

    stat = {"CHANGED": 0, "INERT": 0, "ERROR": 0, "SKIP": 0}
    inert, errors = [], []
    for op in ops:
        scen = scenario(op)
        if scen is None:
            stat["SKIP"] += 1
            continue
        build, strat = scen
        env = scenario_env(op)
        try:
            with_op = evaluate(build, strat, env)
            bare_build = json.loads(json.dumps(build))
            bare_build["feats"] = []
            bare_strat = {"rounds": [{"round": 1, "buffs": ["rage"], "attacks": 1,
                                      "per_attack": {"ops": []}}]}
            if op.get("kind") == "spell":
                bare = 0.0
            else:
                bare = evaluate(bare_build, bare_strat, env)
        except Exception as exc:                       # noqa: BLE001
            stat["ERROR"] += 1
            errors.append((op["id"], str(exc)[:100]))
            continue
        if abs(with_op - bare) > 1e-9:
            stat["CHANGED"] += 1
            if args.verbose:
                print(f"  CHANGED {op['id']}: {bare:.3f} → {with_op:.3f}")
        else:
            stat["INERT"] += 1
            inert.append(op["id"])

    print("=" * 60)
    print(f"对拍 {len(ops)} 个算子")
    for k, v in stat.items():
        print(f"  {k:8s} {v:4d}")
    if inert:
        print("\nINERT（装上后数值无变化，需人工确认是否属于防御/控制/资源类）：")
        for i in inert[:25]:
            print("  " + i)
        if len(inert) > 25:
            print(f"  … 另有 {len(inert)-25} 条")
    if errors:
        print("\nERROR：")
        for i, e in errors[:15]:
            print(f"  {i}: {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
