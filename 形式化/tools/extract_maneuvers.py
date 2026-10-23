#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 5z 规则书 CHM 抽取战技（Maneuver）条目，生成 A 档结构化数据。

用法：
    python extract_maneuvers.py [--chm PATH] [--out PATH]

默认从 docs/5z规则1.63版.chm 解包（需 Windows 的 hh.exe），缓存到 .cache/chm/，
结果写入 data/maneuvers.jsonl。

A 档只抽取战技头部模板字段与正文，不做语义编译。语义编译（B 档）产出 ops/ 下的算子。
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1.63"
SCHOOL_FILES = [
    "开山流专长.htm",
    "游龙流专长.htm",
    "摘星流专长.htm",
    "混元流专长.htm",
    "神机流专长.htm",
]
REL_DIR = os.path.join("专长", "战技专长")
HEADER_FIELDS = ("武器媒介", "卓越骰", "启动消耗", "目标", "持续时间")
# 规则书自身存在字段名不一致：个别条目写作"启动时间"而非"启动消耗"（v1.63 摘星流·高级·燃命 等）
FIELD_ALIASES = {"启动时间": "启动消耗"}
# 「超极」是「超级」的错别字变体，规则书里真实存在 6 处。
# 不认它会导致这些条目被漏抽，且正文串进前一条战技。
HEADER_RE = re.compile(r"^(开山流|游龙流|摘星流|混元流|神机流)·(低级|中级|高级|超级|超极)·(\S+)")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def decompile(chm_path, cache_dir):
    """用 hh.exe 解包 CHM；已解包则直接复用。"""
    marker = os.path.join(cache_dir, REL_DIR)
    if os.path.isdir(marker):
        return cache_dir
    os.makedirs(cache_dir, exist_ok=True)
    if not os.path.isfile(chm_path):
        sys.exit(f"CHM 不存在: {chm_path}")
    try:
        subprocess.run(
            ["hh.exe", "-decompile", os.path.abspath(cache_dir), os.path.abspath(chm_path)],
            check=True,
        )
    except FileNotFoundError:
        sys.exit("未找到 hh.exe：请在 Windows 环境运行，或手动解包后指定 --cache")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"hh.exe 解包失败: {exc}")
    if not os.path.isdir(marker):
        sys.exit(f"解包后未找到 {REL_DIR}，请检查 CHM")
    return cache_dir


def read_html(path):
    """规则书 HTML 为 GBK，按 utf-8 → gbk → gb18030 顺序尝试解码。"""
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", "ignore")


def to_lines(text):
    """HTML → 行列表。规则书由 Word 导出，表格会被压成单元格逐行。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table|br)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def parse_superiority(raw):
    """'2枚（3枚）' → {first:2, repeat:3}；'1枚' → {first:1, repeat:1}。"""
    if not raw:
        return None
    nums = re.findall(r"(\d+|X)枚", raw)
    if not nums:
        return {"raw": raw}
    def cast(v):
        return v if v == "X" else int(v)
    if len(nums) == 1:
        return {"first": cast(nums[0]), "repeat": cast(nums[0]), "raw": raw}
    return {"first": cast(nums[0]), "repeat": cast(nums[1]), "raw": raw}


def parse_cost(raw):
    """启动消耗 → 结构化。词汇有限，未识别的保留原文供 B 档处理。"""
    if not raw:
        return {"raw": ""}
    out = {"raw": raw}
    m = re.search(r"替换(?:([一二两三四五六七八九十\d])次)?攻击", raw)
    if m:
        cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        v = m.group(1) or "一"
        out["replace_attack"] = cn.get(v, int(v) if str(v).isdigit() else 1)
    if "附赠动作" in raw:
        out["bonus_action"] = 1
    if "反应" in raw:
        out["reaction"] = 1
    if re.search(r"(?<!附赠)(?<!反应)动作", raw) and "附赠动作" not in raw and "反应" not in raw:
        out["action"] = 1
    m = re.search(r"(\d+)尺移动力", raw)
    if m:
        out["movement_ft"] = int(m.group(1))
    elif "移动力" in raw:
        out["movement_ft"] = "variable"
    if "无需任何动作" in raw or raw == "无":
        out["free"] = True
    return out


def slugify(name):
    return re.sub(r"\s+", "", name)


def extract(maneuver_dir):
    records = []
    for fname in SCHOOL_FILES:
        path = os.path.join(maneuver_dir, fname)
        if not os.path.isfile(path):
            print(f"跳过（不存在）: {fname}", file=sys.stderr)
            continue
        lines = to_lines(read_html(path))
        starts = [i for i, ln in enumerate(lines) if HEADER_RE.match(ln)]
        for n, idx in enumerate(starts):
            m = HEADER_RE.match(lines[idx])
            school, tier, kind = m.group(1), m.group(2), m.group(3)
            if tier == "超极":        # 归一化错别字
                tier = "超级"
            name = lines[idx - 1] if idx > 0 else "?"

            fields = {}
            notes = []
            j = idx + 1
            while j < len(lines):
                hit = None
                for fld in HEADER_FIELDS + tuple(FIELD_ALIASES):
                    if lines[j].startswith(fld + "："):
                        canon = FIELD_ALIASES.get(fld, fld)
                        fields[canon] = lines[j][len(fld) + 1:].strip()
                        if canon != fld:
                            notes.append(f"原文字段名为“{fld}”，已归一为“{canon}”")
                        hit = fld
                        break
                if hit is None:
                    break
                j += 1

            end = starts[n + 1] - 1 if n + 1 < len(starts) else len(lines)
            body = lines[j:end]

            records.append({
                "id": f"mv.{school}.{tier}.{slugify(name)}",
                "name": name,
                "school": school,
                "tier": tier,
                "kind": kind,
                "weapon_medium": fields.get("武器媒介"),
                "superiority": parse_superiority(fields.get("卓越骰")),
                "cost": parse_cost(fields.get("启动消耗")),
                "target": fields.get("目标"),
                "duration": fields.get("持续时间"),
                "flavor": body[0] if body else None,
                "effect": "\n".join(body[1:]) if len(body) > 1 else "",
                "raw_fields": fields,
                "notes": notes,
                "source": {
                    "book": "5z",
                    "version": RULES_VERSION,
                    "file": f"{REL_DIR.replace(os.sep, '/')}/{fname}",
                    "anchor": name,
                },
                "status": "extracted",
            })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "maneuvers.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    records = extract(os.path.join(root, REL_DIR))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    ids = [r["id"] for r in records]
    dupes = {i for i in ids if ids.count(i) > 1}
    print(f"写入 {len(records)} 条 → {args.out}")
    if dupes:
        print(f"警告：{len(dupes)} 个重复 id: {sorted(dupes)[:10]}", file=sys.stderr)
    missing = [r["id"] for r in records if r["superiority"] is None]
    if missing:
        print(f"提示：{len(missing)} 条无卓越骰字段（多为超级战技）", file=sys.stderr)


if __name__ == "__main__":
    main()
