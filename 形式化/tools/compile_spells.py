#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按模式编译法术算子，并为每条给出明确结论。

用法：
    python compile_spells.py [--write]

与 `compile_maneuvers.py` 同一思路：**不臆测**。能按模式确定的才生成算子，
其余标 `needs_ruling` 或 `out_of_scope`，每条都有 status。

法术的结构比战技规整：A 档 455 条中约 61% 是"攻击/豁免 + 骰式伤害"。
"""

import argparse
import glob
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(REPO, "形式化", "data")
OPS = os.path.join(REPO, "形式化", "ops")

DICE = re.compile(r"(\d+)\s*d\s*(\d+)")
ABILITY = re.compile(r"(力量|敏捷|体质|智力|感知|魅力)\s*豁免")
ABILITY_EN = {"力量": "str", "敏捷": "dex", "体质": "con",
              "智力": "int", "感知": "wis", "魅力": "cha"}
HALF = re.compile(r"豁免成功[^。]{0,12}(伤害减半|半伤)|伤害减半")
ATTACK = re.compile(r"(远程|近战)?法术攻击")
# 伤害类型关键词 → 英文标识
TYPES = [("火焰", "fire"), ("冷冻", "cold"), ("寒冷", "cold"), ("闪电", "lightning"),
         ("强酸", "acid"), ("毒素", "poison"), ("雷鸣", "thunder"), ("光耀", "radiant"),
         ("暗蚀", "necrotic"), ("黯蚀", "necrotic"), ("力场", "force"), ("心灵", "psychic"),
         ("钝击", "bludgeoning"), ("穿刺", "piercing"), ("挥砍", "slashing")]
# 戏法等级成长：5/11/17 级
SCALING_CN = re.compile(r"(\d+)\s*级[^。]{0,10}?(?:变(?:为|成)?|提升至)\s*(\d+)\s*d\s*(\d+)")


def detect_type(text):
    best, pos = None, None
    for cn, en in TYPES:
        i = text.find(cn + "伤害")
        if i < 0:
            i = text.find(cn)
        if i >= 0 and (pos is None or i < pos):
            best, pos = en, i
    return best


def build_op(sp):
    op = {
        "id": sp["id"],
        "name": sp["name"],
        "kind": "spell",
        "level": sp["level"],
        "school": sp["school"],
        "casting_time": sp.get("casting_time"),
        "range": sp.get("range"),
        "concentration": bool(sp.get("concentration")),
        "acquisition": {"type": "spell_access", "level": sp["level"]},
        "cost": {"action": 1} if "动作" in (sp.get("casting_time") or "") else {},
        "decision_point": "before_roll",
        "projection": {},
        "source": sp.get("source") or {"book": "5z", "version": "1.63",
                                       "file": "法术", "anchor": sp["name"]},
        "status": "modeled",
        "ruling": None,
    }
    return op


def compile_one(sp):
    e = sp.get("effect") or ""
    m = DICE.search(e)
    has_save = ABILITY.search(e)
    has_attack = ATTACK.search(e)

    # 无骰式 → 不进入伤害链路
    if not m:
        return ("out_of_scope", None, "原文无骰式伤害")

    # 排除：治疗、召唤、护盾等把骰子用在非伤害处
    if re.search(r"恢复(了)?\s*\d+\s*d\d+\s*点生命值|治疗", e) and not has_save and not has_attack:
        return ("needs_ruling", None, "骰子用于治疗而非伤害")

    dice = f"{m.group(1)}d{m.group(2)}"
    dtype = detect_type(e[m.start():m.start() + 80]) or detect_type(e) or "force"

    # 施法者可选伤害类型（繁彩球、能量射线、龙息术…）：应取目标不抗的那一种，
    # 而不是文本里第一个出现的。交给引擎按目标抗性择优。
    all_types = [en for cn, en in TYPES if cn in e]
    type_choice = None
    if len(set(all_types)) > 1 and re.search(r"选择|任选|由你决定", e):
        type_choice = sorted(set(all_types))
    elif len(set(re.findall(r"\d+\s*d\s*\d+", e))) > 1 and len(set(all_types)) > 1:
        # 多骰式且多类型但无"选择"语义 —— 多为附带/二段伤害，不能自动取首个
        return ("needs_ruling", None, "含多段伤害或附带伤害，需人工区分主伤害")

    if has_save:
        ability = ABILITY_EN[has_save.group(1)]
        op = build_op(sp)
        op["save"] = {"ability": ability, "dc_from": "spell_dc"}
        op["damage"] = {"dice": dice, "damage_type": dtype,
                        "half_on_save": bool(HALF.search(e))}
        if type_choice:
            op["damage"]["type_choice"] = type_choice
        scaling = SCALING_CN.findall(e)
        if scaling:
            op["scaling"] = [{"level": int(lv), "dice": f"{n}d{f}"} for lv, n, f in scaling]
        return ("modeled", op, f"{ability} 豁免，{dice} {dtype}"
                + ("（半伤）" if op["damage"]["half_on_save"] else ""))

    if has_attack:
        op = build_op(sp)
        op["attack"] = {"type": "ranged_spell_attack",
                        "damage": {"dice": dice, "damage_type": dtype}}
        if type_choice:
            op["attack"]["damage"]["type_choice"] = type_choice
        scaling = SCALING_CN.findall(e)
        if scaling:
            op["scaling"] = [{"level": int(lv), "dice": f"{n}d{f}"} for lv, n, f in scaling]
        return ("modeled", op, f"法术攻击，{dice} {dtype}")

    # 有骰式但既非攻击也非豁免：多为持续伤害、自伤、或附带效果
    reasons = []
    if re.search(r"持续|直到", e):
        reasons.append("持续伤害，需按轮结算")
        if re.search(r"开始(其|你的)?回合|回合开始", e):
            reasons.append("按回合复发")
    if re.search(r"你受到|自身受到", e):
        reasons.append("对自身造成伤害")
    if not reasons:
        reasons.append("有骰式但未匹配攻击/豁免结构")
    return ("needs_ruling", None, "；".join(reasons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rel = {r["id"]: r for r in map(json.loads,
                                   open(os.path.join(DATA, "relevance.jsonl"), encoding="utf-8"))}
    spells = [json.loads(l) for l in open(os.path.join(DATA, "spells.jsonl"), encoding="utf-8")]
    existing = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(OPS, "*.json"))}

    rows, written, stat = [], 0, {}
    for sp in spells:
        if rel.get(sp["id"], {}).get("tier") != "A":
            continue
        if sp["id"] in existing:
            # 已有算子：状态以算子声明为准（与职业特性/专长编译器一致）
            op_doc = json.load(open(os.path.join(OPS, sp["id"] + ".json"),
                                    encoding="utf-8"))
            st = op_doc.get("scope") or "modeled"
            rows.append({"id": sp["id"], "name": sp["name"], "status": st,
                         "reason": op_doc.get("scope_reason") or "已手工编译",
                         "source": "manual"})
            stat[st] = stat.get(st, 0) + 1
            continue
        status, op, reason = compile_one(sp)
        rows.append({"id": sp["id"], "name": sp["name"], "status": status,
                     "reason": reason, "source": "compiler"})
        stat[status] = stat.get(status, 0) + 1
        if status == "modeled" and args.write:
            with open(os.path.join(OPS, f"{op['id']}.json"), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump(op, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            written += 1

    out = os.path.join(DATA, "spell_compile_status.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"A 档法术 {len(rows)} 条")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v:4d}")
    if args.write:
        print(f"新写入算子 {written} 个")
    print(f"结论表 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
