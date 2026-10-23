#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 A 档数据做「伤害链路相关性」初筛，产出分诊表。

用法：
    python screen_relevance.py [--data DIR] [--out PATH]

这是一次**机械初筛**，不是最终判定。它只回答"这条是否值得进入 B 档编译队列"，
最终是否建模由人工 / DM 裁定，结果写回算子记录的 status 字段。

分诊等级：
  A  明确影响伤害数值或命中/重击，必进队列
  B  影响动作经济、资源或位移，可能间接影响伤害，需人工判断
  C  无伤害链路信号，暂不建模（保留在库中，可随时复核）
"""

import argparse
import json
import os
import re
import sys

SIGNALS = {
    "damage":  (r"伤害", r"\d+d\d+", r"易伤", r"免疫", r"抗性", r"吸收", r"生命值"),
    "hit":     (r"命中", r"攻击检定", r"优势", r"劣势", r"重击", r"失手"),
    "economy": (r"附赠动作", r"反应", r"攻击次数", r"额外攻击", r"替换", r"多重攻击"),
    "resource": (r"卓越骰", r"术法点", r"法术位", r"每日", r"长休", r"短休", r"次数"),
    "move":    (r"移动", r"冲锋", r"骑乘", r"速度", r"触及", r"射程", r"距离"),
}
WEIGHT = {"damage": 3, "hit": 2, "economy": 2, "resource": 1, "move": 1}
DICE_RE = re.compile(r"\d+d\d+")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def text_of(rec):
    parts = [rec.get("name") or "", rec.get("effect") or "",
             rec.get("body") or "", rec.get("target") or ""]
    return "\n".join(p for p in parts if p)


def score(text):
    hits = {}
    total = 0
    for group, pats in SIGNALS.items():
        found = [p for p in pats if re.search(p, text)]
        if found:
            hits[group] = found
            total += WEIGHT[group]
    if DICE_RE.search(text):
        hits.setdefault("damage", []).append("NdM骰式")
        total += 3
    return total, hits


def tier(total, hits):
    if total >= 6 and "damage" in hits:
        return "A"
    if total >= 3:
        return "B"
    return "C"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(REPO, "形式化", "data"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "relevance.jsonl"))
    args = ap.parse_args()

    rows = []
    for fname, kind in (("maneuvers.jsonl", "maneuver"),
                        ("class_features.jsonl", "class_feature"),
                        ("feats.jsonl", "feat"),
                        ("spells.jsonl", "spell")):
        path = os.path.join(args.data, fname)
        if not os.path.isfile(path):
            print(f"跳过（不存在）: {fname}", file=sys.stderr)
            continue
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            total, hits = score(text_of(rec))
            rows.append({
                "id": rec["id"],
                "kind": kind,
                "name": rec.get("name"),
                "score": total,
                "tier": tier(total, hits),
                "signals": sorted(hits),
                "status": "screened",
            })

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_kind = {}
    for r in rows:
        d = by_kind.setdefault(r["kind"], {"A": 0, "B": 0, "C": 0})
        d[r["tier"]] += 1
    print(f"写入 {len(rows)} 条 → {args.out}")
    for kind, d in by_kind.items():
        print(f"  {kind:9s} A={d['A']:5d}  B={d['B']:5d}  C={d['C']:5d}")


if __name__ == "__main__":
    main()
