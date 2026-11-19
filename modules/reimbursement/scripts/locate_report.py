#!/usr/bin/env python3
"""只读定位：司机 -> 年份文件 -> 月份 sheet -> 报销金额单元格。

用法：
  locate_report.py --driver 周小毛 --year 2026 --month 9
输出 JSON：ok / file / sheet / cell / value / candidates / errors。
绝不修改任何文件。
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "..", "config.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from runtime_config import get as rc_get  # 配置优先、原值兜底


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def match_sheets(names, year, month):
    """按 YYYY.M -> M月 -> M月<后缀> 顺序匹配；排除参数表。"""
    exact = "%d.%d" % (year, month)
    plain = "%d月" % month
    hits = [n for n in names if n == exact]
    stage = "YYYY.M"
    if not hits:
        hits = [n for n in names if n == plain]
        stage = "M月"
    if not hits:
        hits = [n for n in names if n.startswith(plain) and "路线" not in n]
        stage = "M月*"
    return hits, stage


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--driver", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.add_argument("--config", default=CONFIG_PATH)
    args = p.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    subdir = cfg["report_subdir"].replace("{year}", str(args.year))
    fname = cfg["report_filename"].replace("{driver}", args.driver).replace("{year}", str(args.year))
    path = os.path.join(cfg["report_root"], subdir, fname)
    out = {"ok": False, "file": path, "driver": args.driver,
           "year": args.year, "month": args.month, "candidates": [], "errors": []}
    if not os.path.isfile(path):
        out["errors"].append("找不到司机报表文件：%s" % path)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    try:
        import openpyxl
    except ImportError:
        out["errors"].append("缺少 openpyxl，无法读取报表")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    wb_formula = openpyxl.load_workbook(path, data_only=False, read_only=True)
    wb_value = openpyxl.load_workbook(path, data_only=True, read_only=True)
    hits, stage = match_sheets(wb_formula.sheetnames, args.year, args.month)
    out["sheet_match_stage"] = stage
    out["sheets_matched"] = hits
    if len(hits) == 0:
        out["errors"].append("找不到 %d 年 %d 月的 sheet（现有：%s）" %
                             (args.year, args.month, ", ".join(wb_formula.sheetnames)))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if len(hits) > 1:
        out["errors"].append("月份 sheet 歧义：%s" % ", ".join(hits))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    sheet = hits[0]
    out["sheet"] = sheet
    ws_f = wb_formula[sheet]
    ws_v = wb_value[sheet]
    max_row = min(ws_f.max_row or 0, int(rc_get(cfg, "locator_max_rows", 2000)))
    max_col = min(ws_f.max_column or 0, int(rc_get(cfg, "locator_max_cols", 40)))
    label_contains = rc_get(cfg, "locator_label_contains", ["报销"])
    label_excludes = rc_get(cfg, "locator_label_excludes", ["备注", "说明", "口径"])
    label_max = int(rc_get(cfg, "locator_label_max_chars", 20))
    right_offset = int(rc_get(cfg, "locator_right_offset", 3))

    for row in ws_f.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
        for cell in row:
            v = cell.value
            if not isinstance(v, str):
                continue
            label = v.strip()
            if not any(k in label for k in label_contains) or any(k in label for k in label_excludes):
                continue
            if len(label) > label_max:
                continue
            r, c = cell.row, cell.column
            found = None
            for offset in range(1, right_offset + 1):
                if c + offset > max_col:
                    break
                fv = ws_f.cell(row=r, column=c + offset).value
                vv = ws_v.cell(row=r, column=c + offset).value
                if isinstance(fv, str) and fv.startswith("="):
                    found = {"sheet": sheet, "label_cell": cell.coordinate, "label": label,
                             "cell": ws_f.cell(row=r, column=c + offset).coordinate,
                             "formula": True, "raw": fv, "value": vv}
                    break
                if is_number(vv):
                    found = {"sheet": sheet, "label_cell": cell.coordinate, "label": label,
                             "cell": ws_f.cell(row=r, column=c + offset).coordinate,
                             "formula": False, "value": float(vv)}
                    break
            if found:
                out["candidates"].append(found)
            else:
                out["candidates"].append({"sheet": sheet, "label_cell": cell.coordinate,
                                          "label": label, "value": None,
                                          "note": "右侧 %d 格内无数值" % right_offset})
    wb_formula.close()
    wb_value.close()

    valid = [c for c in out["candidates"] if c.get("value") is not None and not c.get("formula")]
    if len(out["candidates"]) == 0:
        out["errors"].append("该月 sheet 中找不到任何报销项")
    elif len(out["candidates"]) > 1:
        out["errors"].append("报销项歧义（%d 处），需人工指定累加目标：%s" %
                             (len(out["candidates"]), ", ".join(c["label_cell"] for c in out["candidates"])))
    elif len(valid) != 1:
        c = out["candidates"][0]
        if c.get("formula"):
            out["errors"].append("报销金额是公式（%s），不覆盖公式" % c["cell"])
        else:
            out["errors"].append("报销项无有效数值（%s），不自动填 0" % c.get("label_cell"))
    else:
        out["ok"] = True
        out["cell"] = valid[0]["cell"]
        out["value"] = valid[0]["value"]
        out["label_cell"] = valid[0]["label_cell"]
        out["label"] = valid[0]["label"]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
