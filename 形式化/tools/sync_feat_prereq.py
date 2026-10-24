#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把专长的先决条件从数据层同步进算子。

先决条件原本只写在 `data/feats.jsonl` 里，算子侧是空的——于是构筑校验
无从判断"这个专长你其实拿不到"。算子才是引擎与工具读取的那一层，
所以要有这条同步。

规则书写作「9级，战斗反射，敏捷13」这样一句，数据里有拆好的
`prerequisite_list`；优先用它，没有再退回按分隔符切。

用法：
    python sync_feat_prereq.py [--write]
"""

import argparse
import glob
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(REPO, "形式化", "data")
OPS = os.path.join(REPO, "形式化", "ops")
SEP = ("，", ",", "、")


def split_prereq(feat):
    lst = feat.get("prerequisite_list") or []
    if lst:
        return [str(x).strip() for x in lst if str(x).strip()]
    raw = feat.get("prerequisite")
    if not raw:
        return []
    parts = [raw]
    for s in SEP:
        parts = [p for chunk in parts for p in chunk.split(s)]
    return [p.strip() for p in parts if p.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    feats = {f["id"]: f for f in map(json.loads, open(
        os.path.join(DATA, "feats.jsonl"), encoding="utf-8"))}

    changed, missing = [], []
    for path in sorted(glob.glob(os.path.join(OPS, "feat.*.json"))):
        op = json.load(open(path, encoding="utf-8"))
        feat = feats.get(op["id"])
        if feat is None:
            missing.append(op["id"])
            continue
        want = split_prereq(feat)
        acq = op.setdefault("acquisition", {})
        if acq.get("prerequisite") == want:
            continue
        acq["prerequisite"] = want
        changed.append((op["id"], want))
        if args.write:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(op, fh, ensure_ascii=False, indent=2)
                fh.write("\n")

    print(f"同步先决条件：变更 {len(changed)} 条")
    for oid, want in changed[:10]:
        print(f"  {oid}: {want}")
    if len(changed) > 10:
        print(f"  … 另有 {len(changed) - 10} 条")
    if missing:
        print(f"数据里找不到的算子 {len(missing)} 条：{missing[:5]}")
    if not args.write:
        print("（未写入；加 --write 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
