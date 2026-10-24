#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按判据 F1–F12 对专长做「是否影响战斗构筑数值」的分诊。

用法：
    python screen_feats.py [--data DIR] [--out PATH]

判据见 `形式化/方案-职业专长法术.md` §4。

本脚本是**机械分诊**，产出理由码与命中原文片段，供人工逐条复核。
它不做最终判定——最终是否建模由 B 档编译与 DM 裁定决定。

输出 verdict 取值：
  in         命中强判据（F2/F3/F4/F6/F9）→ 进 B 档队列
  manual     只命中弱判据（F1/F5/F7/F8/F10/F11/F12）→ 按判据**批量复核**：
             例如「所有仅命中 F11 的专长」可以一次性确认，无需逐条看
  defensive  只命中防御类信号 → 不进主队列，但可能被某个伤害算子反向依赖
  out        只命中社交/探索/剧情信号 → 不进队列
  unclear    无任何信号 → 多半与伤害无关，抽样复核即可
"""

import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 纳入判据 F1–F12
CRITERIA = {
    "F1": ("属性", r"属性值|属性上限|(力量|敏捷|体质|智力|感知|魅力)\s*[+＋]\s*\d"),
    # 优势/劣势必须带攻击语境：单独出现时多为豁免优势（如「健壮」对抗毒素的豁免），
    # 与伤害无关，直接匹配会制造假阳性
    "F2": ("命中", r"攻击检定|攻击加值|命中时|命中后|若命中|未命中|失手|攻击[^。]{0,15}(优势|劣势)"),
    "F3": ("伤害加值", r"额外造成|伤害掷骰[^。]{0,12}加值|\d+\s*点伤害|伤害[^。]{0,6}[+＋]"),
    "F4": ("额外伤害骰", r"\d+\s*d\s*\d+"),
    "F5": ("重骰", r"重骰|重掷|重新掷"),
    "F6": ("重击", r"重击"),
    "F7": ("限次效果", r"每场战斗|每次长休|每长休|每短休|每日|每回合一次|冷却|使用次数"),
    "F8": ("限次次数", r"(增加|额外获得)[^。]{0,10}次数|次数[^。]{0,6}(增加|\+1)"),
    "F9": ("动作经济", r"额外攻击|附赠动作|反应|替换|多重攻击"),
    "F10": ("资源池", r"卓越骰|术法点|法术位|狂暴次数"),
    "F11": ("解锁与获取", r"额外专长|额外[^。]{0,4}战技|习得|解锁|法术列表|已知法术|专长位"),
    "F12": ("位移", r"速度|触及|射程"),
}
# 排除信号
OUT_SIGNALS = {
    "D1": ("防御", r"护甲等级|AC\b|生命(值)?上限|抗性|免疫|豁免加值|护甲熟练|盾牌"),
    "D2": ("社交探索", r"技能熟练|熟练项|交涉|说服|威吓|隐匿|察觉|调查|求生|旅行|探索"),
    "D3": ("剧情画风", r"剧情|画风|角色扮演|叙事|故事"),
}
# F12（速度/触及/射程）计入强判据：打桩追踪相对距离（D-016），
# 触及与射程直接决定哪些攻击在给定距离下可用，速度决定每轮能否进入/脱离。
STRONG = {"F2", "F3", "F4", "F6", "F9", "F12"}
MAX_EVIDENCE = 6


def text_of(rec):
    """只搜效果正文。

    风味文本与先决条件里的「命中」「动作」等词会制造大量假阳性
    （例如纯加血量的「健壮」曾被判为命中相关），因此不纳入检索。
    """
    return rec.get("effect") or rec.get("body") or ""


def collect(text, table):
    hits, evidence = [], []
    for code, (_, pat) in table.items():
        m = re.search(pat, text)
        if m:
            hits.append(code)
            start = max(0, m.start() - 12)
            evidence.append(f"{code}:{m.group(0)}")
    return hits, evidence[:MAX_EVIDENCE]


def decide(in_hits, out_hits):
    """只有命中强判据才进队列；弱判据不足以独立定案。

    强判据 = 命中相关 / 伤害加值 / 额外伤害骰 / 重击 / 动作经济——
    这几项无论是否伴随防御或社交内容，都会改变伤害数值。
    """
    strong = [c for c in in_hits if c in STRONG]
    if strong:
        return "in"
    if in_hits:
        # 只命中属性/重骰/限次/资源/解锁/位移等弱判据，需人判断
        return "manual"
    if set(out_hits) == {"D1"}:
        return "defensive"
    if out_hits:
        return "out"
    # 完全无信号：多半与伤害无关，抽样复核即可
    return "unclear"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(REPO, "形式化", "data"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "feat_triage.jsonl"))
    args = ap.parse_args()

    src = os.path.join(args.data, "feats.jsonl")
    if not os.path.isfile(src):
        sys.exit(f"缺少 {src}，先运行 extract_feats.py")

    rows = []
    for line in open(src, encoding="utf-8"):
        rec = json.loads(line)
        text = text_of(rec)
        in_hits, in_ev = collect(text, CRITERIA)
        out_hits, out_ev = collect(text, OUT_SIGNALS)
        rows.append({
            "id": rec["id"],
            "name": rec["name"],
            "categories": rec.get("categories"),
            "prerequisite": rec.get("prerequisite"),
            "verdict": decide(in_hits, out_hits),
            "criteria": [f"{c}:{CRITERIA[c][0]}" for c in in_hits],
            "exclusions": [f"{c}:{OUT_SIGNALS[c][0]}" for c in out_hits],
            "evidence": in_ev + out_ev,
            "status": "screened",
        })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    stat = {}
    for r in rows:
        stat[r["verdict"]] = stat.get(r["verdict"], 0) + 1
    print(f"写入 {len(rows)} 条 → {args.out}")
    for k in ("in", "defensive", "out", "manual"):
        print(f"  {k:10s} {stat.get(k, 0):4d}")

    crit = {}
    for r in rows:
        if r["verdict"] == "in":
            for c in r["criteria"]:
                crit[c] = crit.get(c, 0) + 1
    print("  in 档命中的判据分布:", dict(sorted(crit.items())))


if __name__ == "__main__":
    main()
