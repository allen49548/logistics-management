#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""driver_sheet.py —— 司机月度表技能 CLI 入口。

子命令：
  init        在 config/local 生成一份真实配置骨架
  inspect     检查模板/数据源/目标表的结构，辅助配置
  plan        只做解析与映射，输出将生成的 sheet 与值（JSON，绝不写文件）
  run         执行生成（默认 dry-run，--no-dry-run 才写入）
  selftest    用内置脱敏样例端到端自测（临时目录，绝不动真实表）

所有命令输出 JSON 摘要到 stdout；退出码 0=成功，2=业务错误，3=配置错误。
"""
from __future__ import annotations

import os
import sys
import json
import argparse
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pipeline  # noqa: E402


def load_config(profile: str | None, config_path: str | None) -> dict:
    if config_path:
        p = config_path
    elif profile:
        if profile == "local":
            p = os.path.join(ROOT, "config", "local", "config.json")
        elif profile == "example":
            p = os.path.join(ROOT, "examples", "config", "config.json")
        else:
            p = os.path.abspath(profile) if os.path.isabs(profile) else os.path.join(ROOT, profile)
    else:
        p = os.path.join(ROOT, "config", "local", "config.json")
        if not os.path.isfile(p):
            p = os.path.join(ROOT, "config", "config.example.json")
    if not os.path.isfile(p):
        raise FileNotFoundError("找不到配置文件: %s" % p)
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_config_path"] = p
    return cfg


def cmd_init(args):
    src = os.path.join(ROOT, "config", "config.example.json")
    dst_dir = os.path.join(ROOT, "config", "local")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "config.json")
    if os.path.exists(dst) and not args.force:
        return {"ok": False, "error": "配置已存在，加 --force 覆盖", "path": dst}
    shutil.copy2(src, dst)
    return {"ok": True, "path": dst,
            "next": "编辑 config/local/config.json 的路径与字段，再跑 inspect / plan / run"}


def cmd_inspect(args):
    cfg = load_config(args.profile, args.config)
    base = os.path.abspath(args.base_dir or ROOT)
    r = pipeline.resolve_paths(cfg, args.year, args.month, base)
    out = {"ok": True, "resolved_paths": r, "template": None, "source": None, "targets": []}
    from sheet_writer import TemplateInfo
    if os.path.isfile(r["template"]):
        info = TemplateInfo.from_template(r["template"], cfg)
        out["template"] = {"file": r["template"], "headers": [n for _, n in info.headers],
                           "data_start_row": info.data_start_row,
                           "merged": info.merged, "freeze": info.freeze}
    if r["source"] and os.path.isfile(r["source"]):
        from source_reader import read_source
        s = read_source(r["source"], cfg)
        out["source"] = {"file": r["source"], "ok": s["ok"], "headers": s["headers"],
                         "drivers": list(s["drivers"].keys()), "warnings": s["warnings"],
                         "errors": s["errors"]}
    tdir = r["target_dir"]
    if os.path.isdir(tdir):
        import re
        pat = re.compile(r"^(?P<driver>.+)%d\.xlsx$" % args.year)
        for fn in sorted(os.listdir(tdir)):
            if fn.startswith("~$") or fn.startswith(".~") or fn.startswith("."):
                continue  # 跳过 WPS/Excel 临时锁文件
            m = pat.match(fn)
            if m:
                from xlsxzip import list_sheets
                fp = os.path.join(tdir, fn)
                try:
                    sheets = list_sheets(fp)
                except Exception as e:
                    sheets = ["<读取失败: %s>" % e]
                out["targets"].append({"driver": m.group("driver"), "file": fp, "sheets": sheets})
    return out


def cmd_plan(args):
    cfg = load_config(args.profile, args.config)
    if not args.no_dry_override:
        cfg.setdefault("run", {})["dry_run"] = True
    base = os.path.abspath(args.base_dir or ROOT)
    return pipeline.run(cfg, args.year, args.month, base, args.source)


def cmd_run(args):
    cfg = load_config(args.profile, args.config)
    cfg.setdefault("run", {})["dry_run"] = not args.no_dry_run
    base = os.path.abspath(args.base_dir or ROOT)
    return pipeline.run(cfg, args.year, args.month, base, args.source)


def cmd_selftest(args):
    from selftest import run_selftest
    return run_selftest()


def main():
    p = argparse.ArgumentParser(description="司机月度表 skill：按模板生成月份 sheet 并填入运输/报销数据")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("init", help="生成 config/local/config.json 骨架")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(fn=cmd_init)

    def common(sp):
        sp.add_argument("--profile", default=None, help="local|example|配置文件/目录路径")
        sp.add_argument("--config", default=None, help="直接指定配置文件路径")
        sp.add_argument("--base-dir", default=None, help="相对路径基准目录（默认 skill 根目录）")
        sp.add_argument("--year", type=int, required=True)
        sp.add_argument("--month", type=int, required=True)
        sp.add_argument("--source", default=None, help="覆盖数据源路径")

    pin = sub.add_parser("inspect", help="检查模板/数据源/目标表结构")
    common(pin); pin.set_defaults(fn=cmd_inspect)

    pp = sub.add_parser("plan", help="只解析不写入，输出将生成的 sheet 与值")
    common(pp); pp.add_argument("--no-dry-override", action="store_true")
    pp.set_defaults(fn=cmd_plan)

    pr = sub.add_parser("run", help="执行生成（默认 dry-run）")
    common(pr); pr.add_argument("--no-dry-run", action="store_true", help="真正写入文件")
    pr.set_defaults(fn=cmd_run)

    ps = sub.add_parser("selftest", help="内置脱敏样例端到端自测")
    ps.set_defaults(fn=cmd_selftest)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        p.print_help()
        sys.exit(1)
    try:
        result = args.fn(args)
    except Exception as e:
        result = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("ok"):
        sys.exit(0)
    code = 3 if "config" in str(result.get("error", "")).lower() or "配置" in str(result.get("error", "")) else 2
    sys.exit(code)


if __name__ == "__main__":
    main()
