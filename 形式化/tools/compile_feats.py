#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按模式编译专长算子，并为每条给出明确结论。

用法：
    python compile_feats.py [--write]

专长的结构比法术更杂（A 档 139 条中约三分之一是"其他"），
因此这里只认**明确的数值句式**，宁可标 needs_ruling 也不臆测。
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

# 「命中后额外造成 N 点伤害 / NdM」
HIT_EXTRA = re.compile(r"命中[^。]{0,20}额外造成[“\"]?([一二两三四五六七八九十\d]+)\s*d\s*(\d+)")
# 「攻击检定/攻击加值 +N」
ATTACK_BONUS = re.compile(r"攻击检定[^。]{0,10}[+＋]\s*(\d+)|攻击加值[^。]{0,6}[+＋]\s*(\d+)")
# 「伤害掷骰 +N」或「额外造成 N 点伤害」（固定值）
FLAT_DAMAGE = re.compile(r"伤害[^。]{0,12}[+＋]\s*(\d+)\s*点|额外造成\s*(\d+)\s*点伤害")
# 「重击范围 +N」
CRIT_RANGE = re.compile(r"重击范围[^。]{0,6}[+＋]\s*(\d+)")
ONCE_PER_TURN = re.compile(r"每回合一次|每回合只能|一回合一次")


def base_op(f):
    return {
        "id": f["id"],
        "name": f["name"],
        "kind": "feat",
        "acquisition": {"type": "feat_slot",
                        "prerequisite": f.get("prerequisite_list") or []},
        "cost": {"passive": True},
        "decision_point": "self_turn_start",
        "gate": {},
        "projection": {},
        "source": (f.get("sources") or [f.get("source") or {}])[0],
        "status": "modeled",
        "ruling": None,
    }


def compile_one(f):
    e = f.get("effect") or ""

    # 排除：需要队友 / 夹击 / 消耗其他角色资源
    if re.search(r"友方生物[^。]{0,10}(可以|能够)", e):
        return ("needs_ruling", None, "涉及友方参与，不属于本人单角色输出")
    if re.search(r"夹击", e):
        return ("needs_ruling", None, "以夹击为前提，打桩契约默认不成立")
    if re.search(r"召唤|狩猎虫|灵魄|部署物|构筑体|同伴", e):
        return ("out_of_scope", None, "召唤物/随从相关，按协作约定排除")
    if re.search(r"治疗", e) and not re.search(r"伤害", e):
        return ("out_of_scope", None, "治疗类效果，不进入伤害链路")

    # 专长的条件结构远多于法术：以下任一出现即不自动编译。
    # 实测把"巨武器大师的 +3"（对穿甲目标才生效）当无条件加值，
    # 以及把"阵地狙击"的按优势层数加骰当固定 1d10，都会系统性高估。
    cond = []
    if re.search(r"选择|之一", e):
        cond.append("多选一")
    if re.search(r"如果|若|当[^。]{0,10}时", e):
        cond.append("条件分支")
    if re.search(r"长休|短休|每场战斗|次数", e):
        cond.append("限次使用")
    if re.search(r"优势", e):
        cond.append("随优势层数变化")
    if re.search(r"护甲|盾牌|着装", e):
        cond.append("对特定装备的目标才生效")
    if re.search(r"详见下表|如下表|连招", e):
        cond.append("伤害查表")
    if re.search(r"重击后|造成重击[^。]{0,10}(时|后)", e):
        cond.append("重击触发")
    if re.search(r"在[^。]{0,14}后[，,]", e):
        cond.append("需前置动作/位移")
    if re.search(r"第\s*\d+\s*级起", e):
        cond.append("数值随等级变化，需分段建模")
    if cond:
        return ("needs_ruling", None, "；".join(cond))

    m = HIT_EXTRA.search(e)
    if m:
        n = CN.get(m.group(1)) or int(m.group(1))
        op = base_op(f)
        extra = {"slot": "专长", "dice": f"{n}d{m.group(2)}", "crit_doubles": True,
                 "note": "命中后额外造成此骰式伤害"}
        if ONCE_PER_TURN.search(e):
            extra["limit"] = "每回合一次"
        op["projection"]["extra_dice"] = [extra]
        return ("modeled", op, f"命中后 +{n}d{m.group(2)}"
                + ("（每回合一次）" if extra.get("limit") else ""))

    m = CRIT_RANGE.search(e)
    if m:
        op = base_op(f)
        op["projection"]["crit_range"] = int(m.group(1))
        return ("modeled", op, f"重击范围 +{m.group(1)}")

    m = ATTACK_BONUS.search(e)
    if m:
        val = int(m.group(1) or m.group(2))
        op = base_op(f)
        op["projection"]["attack_bonus"] = val
        return ("modeled", op, f"攻击检定 +{val}")

    m = FLAT_DAMAGE.search(e)
    if m:
        val = int(m.group(1) or m.group(2))
        op = base_op(f)
        op["projection"]["damage_slots"] = [{
            "slot": "专长", "expr": str(val), "applies_on": "on_hit",
            "note": f"伤害 +{val}"}]
        return ("modeled", op, f"固定伤害 +{val}")

    reasons = []
    if re.search(r"卓越骰", e):
        reasons.append("卓越骰相关，需与战技系统联动")
    if re.search(r"选择|之一", e):
        reasons.append("多选一，需按选项分别编译")
    if re.search(r"如果|若", e):
        reasons.append("含条件分支")
    if not reasons:
        reasons.append("结构未匹配到已知模式")
    return ("needs_ruling", None, "；".join(reasons))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rel = {r["id"]: r for r in map(json.loads,
                                   open(os.path.join(DATA, "relevance.jsonl"), encoding="utf-8"))}
    feats = [json.loads(l) for l in open(os.path.join(DATA, "feats.jsonl"), encoding="utf-8")]
    existing = {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(OPS, "*.json"))}

    rows, written, stat = [], 0, {}
    for f in feats:
        if rel.get(f["id"], {}).get("tier") != "A":
            continue
        if f["id"] in existing:
            # 已有算子文件：状态以算子自己的声明为准。「编译完成」不等于
            # 「能算伤害」——防御类特性同样如实记成 out_of_scope，
            # 否则统计表会把它们算进覆盖率，掩盖真实缺口。
            op_doc = json.load(open(os.path.join(OPS, f["id"] + ".json"),
                                    encoding="utf-8"))
            st = op_doc.get("scope") or "modeled"
            rows.append({"id": f["id"], "name": f["name"], "status": st,
                         "reason": op_doc.get("scope_reason") or "已手工编译",
                         "source": "manual"})
            stat[st] = stat.get(st, 0) + 1
            continue
        status, op, reason = compile_one(f)
        rows.append({"id": f["id"], "name": f["name"], "status": status,
                     "reason": reason, "source": "compiler"})
        stat[status] = stat.get(status, 0) + 1
        if status == "modeled" and args.write:
            with open(os.path.join(OPS, f"{op['id']}.json"), "w",
                      encoding="utf-8", newline="\n") as fh:
                json.dump(op, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            written += 1

    out = os.path.join(DATA, "feat_compile_status.jsonl")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"A 档专长 {len(rows)} 条")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {k:14s} {v:4d}")
    if args.write:
        print(f"新写入算子 {written} 个")
    print(f"结论表 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
