#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按模式编译职业特性算子，并为每条给出明确结论。

用法：
    python compile_class_features.py [--write]

职业特性的结构介于法术与专长之间：多数是条件化的，但"攻击次数"这一类
句式极其固定且影响最大（乘数级），优先编译。
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

# 「执行攻击动作时可以发动 N 次武器攻击」
ATTACK_COUNT = re.compile(r"执行攻击动作时可以发动\s*([一二两三四五六七八九十\d]+)\s*次武器攻击")


def base_op(c):
    return {
        "id": c["id"],
        "name": c["name"],
        "kind": "class_feature",
        "acquisition": {
            "type": "class_level",
            "class": c["class"],
            "class_id": c.get("class_id") or f"class.{_class_slug(c['class'])}",
            "level": c.get("level"),
            "subclass": c.get("subclass"),
        },
        "cost": {"passive": True},
        "decision_point": "self_turn_start",
        "gate": {},
        "projection": {},
        "source": (c.get("sources") or [c.get("source") or {}])[0],
        "status": "modeled",
        "ruling": None,
    }


CLASS_SLUG = {
    "圣武士": "paladin", "奇械师": "artificer", "契术师": "warlock", "武僧": "monk",
    "法师": "wizard", "游侠": "ranger", "灵能武士": "psychic_warrior", "狂战士": "barbarian",
    "盗贼": "rogue", "牧师": "cleric", "德鲁伊": "druid", "吟游诗人": "bard",
    "灵能师": "psion", "召唤师": "summoner",
}


def _class_slug(name):
    return CLASS_SLUG.get(name, name)


def compile_one(c):
    e = c.get("effect") or ""

    if re.search(r"召唤|灵魄|部署物|构筑体", e):
        return ("out_of_scope", None, "召唤物/部署物相关，按协作约定排除")

    m = ATTACK_COUNT.search(e)
    if m:
        n = CN.get(m.group(1)) or int(m.group(1))
        op = base_op(c)
        op["projection"]["attack_count"] = n
        return ("modeled", op, f"攻击次数 = {n}")

    reasons = []
    if re.search(r"如果|若|当[^。]{0,10}时", e):
        reasons.append("条件分支")
    if re.search(r"选择|之一", e):
        reasons.append("多选一")
    if re.search(r"长休|短休|次数", e):
        reasons.append("限次使用")
    if re.search(r"优势", e) and not m:
        reasons.append("涉及优势层数")
    if re.search(r"详见下表|如下表", e):
        reasons.append("伤害查表")
    if not reasons:
        reasons.append("结构未匹配到已知模式")
    return ("needs_ruling", None, "；".join(reasons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rel = {r["id"]: r for r in map(json.loads,
                                   open(os.path.join(DATA, "relevance.jsonl"), encoding="utf-8"))}
    feats = [json.loads(l) for l in open(os.path.join(DATA, "class_features.jsonl"),
                                         encoding="utf-8")]
    existing = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(OPS, "*.json"))}

    rows, written, stat = [], 0, {}
    for c in feats:
        if rel.get(c["id"], {}).get("tier") != "A":
            continue
        if c["id"] in existing:
            rows.append({"id": c["id"], "name": c["name"], "status": "modeled",
                         "reason": "已手工编译", "source": "manual"})
            stat["modeled"] = stat.get("modeled", 0) + 1
            continue
        status, op, reason = compile_one(c)
        rows.append({"id": c["id"], "name": c["name"], "status": status,
                     "reason": reason, "source": "compiler"})
        stat[status] = stat.get(status, 0) + 1
        if status == "modeled" and args.write:
            with open(os.path.join(OPS, f"{op['id']}.json"), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump(op, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            written += 1

    out = os.path.join(DATA, "class_feature_compile_status.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"A 档职业特性 {len(rows)} 条")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v:4d}")
    if args.write:
        print(f"新写入算子 {written} 个")
    print(f"结论表 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
