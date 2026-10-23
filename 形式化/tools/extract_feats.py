#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CHM 抽取专长（Feat）条目，生成 A 档结构化数据。

用法：
    python extract_feats.py [--chm PATH] [--out PATH]

专长页没有战技那样的模板字段，唯一可靠的标记是**版式**：专长名是整段加粗、
字号 14pt 的段落（Word 导出后为 <p><b><span style="...font-weight:bold;
font-size:14.0000pt...">名称</span></b></p>）。

因此判定规则为：整个段落文本都落在 14pt 粗体 span 内、长度 ≤ 20 字、
且不含句读标点，才视为专长名。这条规则用于避免把长名拆成碎片
（如「黯星降临」被拆成「黯星」+「降临」）。

A 档只抽名称、先决条件与正文，不做语义编译。
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1.63"
FEAT_DIRS = ["通用专长", "娴熟专长", "职业专长", "种族专长", "秘法专长", "超魔专长"]
SKIP_FILES = {"专长说明.htm"}
MAX_NAME_LEN = 20
SENTENCE_PUNCT = re.compile(r"[，。；：、！？]")

# 14pt 粗体的 span（两种属性顺序都出现过）
BOLD14 = re.compile(
    r'<span[^>]*font-weight:bold[^>]*font-size:14\.0000pt[^>]*>(.*?)</span>'
    r'|<span[^>]*font-size:14\.0000pt[^>]*font-weight:bold[^>]*>(.*?)</span>',
    re.S | re.I,
)
PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def decompile(chm_path, cache_dir):
    marker = os.path.join(cache_dir, "专长")
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
    return cache_dir


def read_html(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", "ignore")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def paragraphs(text):
    """返回 [(is_feat_name, text)]。"""
    out = []
    for raw in PARA.findall(text):
        plain = strip_tags(raw)
        if not plain:
            continue
        bold = "".join(a or b for a, b in BOLD14.findall(raw))
        bold_plain = strip_tags(bold)
        is_name = (
            bool(bold_plain)
            and bold_plain == plain
            and len(plain) <= MAX_NAME_LEN
            and not SENTENCE_PUNCT.search(plain)
        )
        out.append((is_name, plain))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "feats.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    records = []

    for d in FEAT_DIRS:
        dirpath = os.path.join(root, "专长", d)
        if not os.path.isdir(dirpath):
            print(f"跳过（不存在）: {d}", file=sys.stderr)
            continue
        for fname in sorted(os.listdir(dirpath)):
            if not fname.endswith(".htm") or fname in SKIP_FILES:
                continue
            paras = paragraphs(read_html(os.path.join(dirpath, fname)))
            starts = [i for i, (is_name, _) in enumerate(paras) if is_name]
            for n, idx in enumerate(starts):
                name = paras[idx][1]
                end = starts[n + 1] if n + 1 < len(starts) else len(paras)
                body = [t for _, t in paras[idx + 1:end]]

                prereq = None
                if body and body[0].startswith("先决条件"):
                    prereq = re.sub(r"^先决条件[：:]\s*", "", body[0]).strip()
                    body = body[1:]

                is_variant = name.startswith("玩家可选变体")
                records.append({
                    "id": f"feat.{name}",
                    "name": name,
                    "category": d,
                    "is_variant": is_variant,
                    "prerequisite": prereq,
                    "prerequisite_list": [p.strip() for p in re.split(r"[，、,]", prereq)] if prereq else [],
                    "flavor": body[0] if body else None,
                    "effect": "\n".join(body[1:]) if len(body) > 1 else "",
                    "body": "\n".join(body),
                    "source": {
                        "book": "5z",
                        "version": RULES_VERSION,
                        "file": f"专长/{d}/{fname}",
                        "anchor": name,
                    },
                    "status": "extracted",
                })

    # 同一专长可能出现在多个分类页（如「额外引导神力」同时属德鲁伊与牧师），
    # 这是同一个专长，应合并为一条并保留全部 source，而不是当作重复。
    merged = {}
    for rec in records:
        key = rec["id"]
        if key in merged:
            first = merged[key]
            first["sources"].append(rec["source"])
            if rec["category"] not in first["categories"]:
                first["categories"].append(rec["category"])
            if not first.get("prerequisite") and rec.get("prerequisite"):
                first["prerequisite"] = rec["prerequisite"]
                first["prerequisite_list"] = rec["prerequisite_list"]
        else:
            rec["categories"] = [rec.pop("category")]
            rec["sources"] = [rec.pop("source")]
            rec["source"] = rec["sources"][0]
            merged[key] = rec

    final = list(merged.values())
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in final:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    multi = [r["name"] for r in final if len(r["sources"]) > 1]
    print(f"写入 {len(final)} 条（原始 {len(records)} 条，合并 {len(records) - len(final)} 条重复）→ {args.out}")
    if multi:
        print(f"跨分类专长 {len(multi)} 个: {multi[:8]}")
    no_prereq = sum(1 for r in final if not r["prerequisite"])
    print(f"无先决条件: {no_prereq} / {len(final)}")


if __name__ == "__main__":
    main()
