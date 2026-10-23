#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按模式编译战技算子，并为每条给出明确结论。

用法：
    python compile_maneuvers.py [--write] [--out-dir DIR]

设计原则：**不臆测**。能按模式确定编译的才生成算子；
无法确定的标 `needs_ruling` 并写明缺什么；不影响伤害的标 `out_of_scope`。
每条 A 档战技都会得到一个 status，不存在"忘了处理"的条目。

产出的结论表写到 `data/maneuver_compile_status.jsonl`，供覆盖率统计与人工复核。
"""

import argparse
import glob
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(REPO, "形式化", "data")
OPS = os.path.join(REPO, "形式化", "ops")

CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

DICE_VALUE = re.compile(r"[“\"]?([一二两三四五六七八九十\d]+)\s*枚卓越骰的骰值")
NO_DAMAGE = re.compile(r"不会造成伤害|不造成伤害|不造成任何伤害")
SCHOOL_FILE = {
    "开山流": "专长/战技专长/开山流专长.htm",
    "游龙流": "专长/战技专长/游龙流专长.htm",
    "摘星流": "专长/战技专长/摘星流专长.htm",
    "混元流": "专长/战技专长/混元流专长.htm",
    "神机流": "专长/战技专长/神机流专长.htm",
}


def cn2int(s):
    return int(s) if s.isdigit() else CN.get(s, 1)


def op_id(m):
    return m["id"]


def base_op(m):
    sup = m.get("superiority") or {}
    cost = {k: v for k, v in (m.get("cost") or {}).items() if k != "raw"}
    if sup and sup.get("first"):
        cost["superiority"] = sup["first"]
    return {
        "id": op_id(m),
        "name": m["name"],
        "kind": "maneuver",
        "acquisition": {"type": "maneuver_slot", "known": True,
                        "dice_cost_first": sup.get("first"),
                        "dice_cost_repeat": sup.get("repeat")},
        "cost": cost,
        "decision_point": "before_roll",
        "gate": {"weapon_medium": m.get("weapon_medium")},
        "projection": {},
        "source": {"book": "5z", "version": "1.63",
                   "file": SCHOOL_FILE.get(m["school"], "专长/战技专长"),
                   "anchor": m["name"]},
        "status": "modeled",
        "ruling": None,
    }


def compile_one(m):
    """返回 (status, op_or_None, reason)。"""
    e = m.get("effect") or ""
    kind = m.get("kind")
    name = m["name"]

    # 1) 明确不造成伤害 → 不影响伤害链路
    if NO_DAMAGE.search(e):
        return ("out_of_scope", None,
                "原文明确不造成伤害，不进入伤害链路")

    # 2a) 伤害对象不是"被攻击的目标"，而是线/区域上的其它生物——不能当目标加伤
    if re.search(r"(线段|区域|周围)[^。]{0,20}生物[^。]{0,12}受到", e):
        return ("needs_ruling", None,
                "伤害作用于线/区域上的其它生物，不是被攻击目标")

    # 2b) 多选一（颜色、材质、形态）——需按具体选项分别编译
    if re.search(r"[红蓝绿黄紫白黑]：", e) or re.search(r"选择以下|以下选项之一", e):
        return ("needs_ruling", None, "多选一结构（颜色/形态/选项），需按选项分别编译")

    # 2) 「命中后紧邻 N 枚卓越骰的骰值」——只用这条高置信句式。
    #    宽松匹配会把查表、随距离累加、甚至与伤害无关的骰值统统吞进来
    #    （长城=查表、彗星袭月=按飞行距离累加、子弹时间=用于代替敏捷调整值），
    #    因此宁少不错：只认"命中"与骰值相距很近的写法。
    HIT_DICE = re.compile(r"(?:命中[^。]{0,25}|攻击[^。]{0,20})"
                          r"([一二两三四五六七八九十\d]+)\s*枚卓越骰的骰值")
    m_dice = HIT_DICE.search(e)
    if m_dice:
        n = cn2int(m_dice.group(1))
        # 排除：查表、按距离/次数累加、条件变形
        if re.search(r"详见下表|如下表|参见下表", e):
            return ("needs_ruling", None, "伤害查表，表结构未入库")
        if re.search(r"每(飞行|移动|前进|突进|消耗)[^。]{0,20}额外造成", e):
            return ("needs_ruling", None, "伤害随位移/次数累加，需状态化表达式")
        head = e[:m_dice.start()]
        if re.search(r"如果[^。]{0,40}改为|若[^。]{0,40}改为", head):
            return ("needs_ruling", None, "卓越骰枚数随条件变化")

        # 以下四类在打桩契约下不能算作"本人对目标的伤害"
        if re.search(r"友方生物|同伴|你的盟友", e):
            return ("needs_ruling", None, "伤害来自友方的攻击，不属于本人输出")
        if re.search(r"夹击", e):
            return ("needs_ruling", None, "以夹击为前提，打桩契约默认不成立")
        if re.search(r"倍数", e):
            return ("needs_ruling", None, "「等同于骰值倍数的伤害」倍率不明")
        if re.search(r"物件[^。]{0,20}自动命中", e):
            return ("needs_ruling", None, "该加骰作用于物件，不是生物目标")
        if re.search(r"如果[^。]{0,30}放弃了", e):
            return ("needs_ruling", None, "加骰数取决于放弃了哪项特性，需按选项编译")
        if re.search(r"(可以使用一个反应|可以用一个反应)", e):
            return ("needs_ruling", None, "涉及友方用反应参与，不属于本人单角色输出")
        if re.search(r"为每一(把|道|枚|个)", e):
            return ("needs_ruling", None, "多实例分别攻击，需按实例数建模")

        op = base_op(m)
        dtype = None
        if "闪电伤害" in e:
            dtype = "lightning"
        elif "火焰伤害" in e:
            dtype = "fire"
        elif "挥砍伤害" in e:
            dtype = "slashing"
        extra = {"slot": "战技", "dice": f"{n}dS", "crit_doubles": True,
                 "note": f"命中后额外造成 {n} 枚卓越骰的骰值伤害"}
        if dtype:
            extra["damage_type"] = dtype
        op["projection"]["extra_dice"] = [extra]
        return ("modeled", op, f"命中后 +{n} 枚卓越骰")

    # 3) 强化技：持续期间每次攻击加骰
    if kind == "强化技" and m_dice and re.search(r"持续期间|直到你的下个回合", e):
        n = cn2int(m_dice.group(1))
        op = base_op(m)
        op["decision_point"] = "self_turn_start"
        op["projection"]["extra_dice"] = [{
            "slot": "战技", "dice": f"{n}dS", "crit_doubles": True,
            "note": f"持续期间每次攻击命中后额外造成 {n} 枚卓越骰的骰值伤害"}]
        return ("modeled", op, f"持续增益：每次攻击 +{n} 枚卓越骰")

    # 3b) 公式型伤害：速度/上限这类结构已由引擎支持
    if re.search(r"等同于.?你的移动速度|移动速度\s*/", e):
        op = base_op(m)
        op["projection"]["damage_replaces_weapon"] = True
        op["projection"]["formula_damage"] = {
            "expr": "speed / 2.5", "cap": 50,
            "note": "伤害总值等于移动速度/2.5，最大 50"}
        return ("modeled", op, "公式：移动速度/2.5（上限 50）")

    # 4) 其余：需要裁定或特殊表达式
    reasons = []
    if re.search(r"检定.*对抗|对抗.*检定", e):
        reasons.append("依赖对抗检定，结果分布未建模")
    if re.search(r"参见下表|详见下表|如下表", e):
        reasons.append("伤害查表——需先把该表结构化进 damage_tables.jsonl")
    if re.search(r"等同于.*移动速度|生命值差额|倍数", e):
        reasons.append("伤害为公式而非骰式，需专用表达式")
    if re.search(r"立即死亡|直接死亡", e):
        reasons.append("即死效果，打桩下无意义")
    if not reasons:
        reasons.append("结构未匹配到已知模式")
    return ("needs_ruling", None, "；".join(reasons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="实际写入算子文件")
    ap.add_argument("--out-dir", default=OPS)
    args = ap.parse_args()

    rel = {r["id"]: r for r in map(json.loads,
                                   open(os.path.join(DATA, "relevance.jsonl"), encoding="utf-8"))}
    mvs = [json.loads(l) for l in open(os.path.join(DATA, "maneuvers.jsonl"), encoding="utf-8")]
    existing = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(OPS, "*.json"))}

    rows, written = [], 0
    stat = {}
    for m in mvs:
        if rel.get(m["id"], {}).get("tier") != "A":
            continue
        if m["id"] in existing:
            rows.append({"id": m["id"], "name": m["name"], "status": "modeled",
                         "reason": "已手工编译", "source": "manual"})
            stat["modeled"] = stat.get("modeled", 0) + 1
            continue
        status, op, reason = compile_one(m)
        rows.append({"id": m["id"], "name": m["name"], "status": status,
                     "reason": reason, "source": "compiler"})
        stat[status] = stat.get(status, 0) + 1
        if status == "modeled" and args.write:
            path = os.path.join(args.out_dir, f"{op['id']}.json")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(op, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            written += 1

    out = os.path.join(DATA, "maneuver_compile_status.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"A 档战技 {len(rows)} 条")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v:3d}")
    if args.write:
        print(f"新写入算子 {written} 个")
    print(f"结论表 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
