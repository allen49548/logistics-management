#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xlsxzip.py —— 以 zip 层「精确插入」方式向既有 xlsx 新增 worksheet。

背景：真实司机表是 WPS 保存的文件，含 customXml / metadata.xml / vmlDrawing /
sharedStrings 等 openpyxl 无法完整保留的部件。整体 load+save 会丢失这些部件。
本模块只改动 4 个索引部件 + 新增 1 个 worksheet 部件，其余部件字节原样复制：

  - xl/workbook.xml                       追加 <sheet>
  - xl/_rels/workbook.xml.rels            追加 sheet relationship
  - [Content_Types].xml                   追加 worksheet override
  - docProps/app.xml                      追加 TitlesOfParts / HeadingPairs（存在时）
  - xl/worksheets/sheetN.xml              新部件（由本模块生成）

对外主函数：insert_sheet(path, sheet_name, sheet_xml, backup_dir=None) -> dict
sheet_xml 为完整 worksheet XML 字符串（由 sheet_writer 生成，样式索引指向目标工作簿）。
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
import tempfile
from datetime import datetime
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"

ET.register_namespace("", NS_MAIN)
ET.register_namespace("r", NS_REL_DOC)


def _prefix(tag: str, ns: str) -> str:
    return "{%s}%s" % (ns, tag)


def _read_parts(path: str) -> dict:
    parts = {}
    with zipfile.ZipFile(path, "r") as z:
        for info in z.infolist():
            parts[info.filename] = z.read(info.filename)
    return parts


def _write_parts(path: str, parts: dict, order_hint: list = None):
    """按最小改动写回：保持每个部件字节不变，仅重写被修改的部件。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".midsheet-", suffix=".xlsx", dir=d)
    os.close(fd)
    try:
        names = list(parts.keys())
        if order_hint:
            rank = {n: i for i, n in enumerate(order_hint)}
            names.sort(key=lambda n: rank.get(n, 10_000))
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                z.writestr(n, parts[n])
        shutil.move(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _backup(path: str, backup_dir: str) -> str | None:
    if not backup_dir:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(backup_dir, ts)
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, os.path.basename(path))
    shutil.copy2(path, dst)
    return dst


def next_sheet_part(parts: dict) -> tuple:
    """返回 (new_sheet_path, new_sheet_id, new_rid)。"""
    used_nums = []
    for n in parts:
        m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", n)
        if m:
            used_nums.append(int(m.group(1)))
    num = max(used_nums, default=0) + 1
    while ("xl/worksheets/sheet%d.xml" % num) in parts:
        num += 1

    # workbook.xml 里 sheetId 取最大值+1
    wb = ET.fromstring(parts["xl/workbook.xml"])
    sheets_el = wb.find(_prefix("sheets", NS_MAIN))
    ids = []
    if sheets_el is not None:
        for sh in sheets_el.findall(_prefix("sheet", NS_MAIN)):
            try:
                ids.append(int(sh.get("sheetId", "0")))
            except ValueError:
                pass
    sid = max(ids, default=0) + 1

    # rels 里取最大 rId
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    rnums = []
    for rel in rels.findall(_prefix("Relationship", NS_REL_PKG)):
        m = re.fullmatch(r"rId(\d+)", rel.get("Id", ""))
        if m:
            rnums.append(int(m.group(1)))
    rid = "rId%d" % (max(rnums, default=0) + 1)

    return "xl/worksheets/sheet%d.xml" % num, sid, rid


def insert_sheet(path: str, sheet_name: str, sheet_xml: str,
                 backup_dir: str | None = None,
                 verify: bool = True,
                 extra_parts: dict | None = None) -> dict:
    """把 sheet_xml 作为新 sheet 精确插入 path。返回 JSON 摘要。

    sheet_xml 必须：
      - 根元素为 <worksheet>（命名空间 NS_MAIN）
      - 单元格样式索引指向目标工作簿 styles.xml 已有索引
    extra_parts: 需一并原子写入的额外部件（如已桥接好的 xl/styles.xml），
                 与插入操作共用同一份备份，失败时一起回滚。
    """
    out = {"ok": False, "file": path, "sheet": sheet_name, "added": [], "errors": []}
    if not os.path.isfile(path):
        out["errors"].append("目标文件不存在: %s" % path)
        return out

    parts = _read_parts(path)
    order = list(parts.keys())
    orig = dict(parts)  # 用于回读校验
    if extra_parts:
        parts.update(extra_parts)

    if any(s == sheet_name for s in _sheet_names(parts)):
        out["errors"].append("sheet 已存在: %s" % sheet_name)
        return out

    try:
        sheet_part, sid, rid = next_sheet_part(parts)
        parts[sheet_part] = sheet_xml.encode("utf-8")
        out["added"].append(sheet_part)

        # 1) workbook.xml
        wb = ET.fromstring(parts["xl/workbook.xml"])
        sheets_el = wb.find(_prefix("sheets", NS_MAIN))
        if sheets_el is None:
            sheets_el = ET.SubElement(wb, _prefix("sheets", NS_MAIN))
        sh = ET.SubElement(sheets_el, _prefix("sheet", NS_MAIN))
        sh.set("name", sheet_name)
        sh.set("sheetId", str(sid))
        sh.set("{%s}id" % NS_REL_DOC, rid)
        parts["xl/workbook.xml"] = ET.tostring(wb, encoding="utf-8", xml_declaration=True)

        # 2) workbook.xml.rels
        rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
        rel = ET.SubElement(rels, _prefix("Relationship", NS_REL_PKG))
        rel.set("Id", rid)
        rel.set("Type", NS_REL_DOC + "/worksheet")
        rel.set("Target", "worksheets/" + os.path.basename(sheet_part))
        parts["xl/_rels/workbook.xml.rels"] = ET.tostring(rels, encoding="utf-8", xml_declaration=True)

        # 3) [Content_Types].xml
        ct = ET.fromstring(parts["[Content_Types].xml"])
        ct_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
        exists = any(o.get("PartName") == "/" + sheet_part for o in ct.findall(_prefix("Override", NS_CT)))
        if not exists:
            ov = ET.SubElement(ct, _prefix("Override", NS_CT))
            ov.set("PartName", "/" + sheet_part)
            ov.set("ContentType", ct_type)
        parts["[Content_Types].xml"] = ET.tostring(ct, encoding="utf-8", xml_declaration=True)

        # 4) docProps/app.xml（存在则追加 TitlesOfParts）
        app_key = "docProps/app.xml"
        if app_key in parts:
            try:
                app = ET.fromstring(parts[app_key])
                titles = app.find(_prefix("TitlesOfParts", NS_EP))
                if titles is not None:
                    vec = titles.find("{http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes}vector")
                    if vec is not None:
                        vt_lpstr = "{http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes}lpstr"
                        item = ET.SubElement(vec, vt_lpstr)
                        item.text = sheet_name
                        vec.set("size", str(len(vec.findall(vt_lpstr))))
                        parts[app_key] = ET.tostring(app, encoding="utf-8", xml_declaration=True)
            except Exception:
                pass  # app.xml 非关键，失败不影响工作簿有效性

        bak = _backup(path, backup_dir) if backup_dir else None
        _write_parts(path, parts, order_hint=order)

        # 回读校验：原有部件字节零差异 + 新 sheet 可解析
        if verify:
            check = _read_parts(path)
            changed = [n for n in orig if n in check and check[n] != orig[n]]
            allowed = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                       "[Content_Types].xml", "docProps/app.xml"}
            if extra_parts:
                allowed |= set(extra_parts.keys())
            unexpected = [n for n in changed if n not in allowed]
            if unexpected:
                out["errors"].append("回读校验失败，意外改动部件: %s" % ", ".join(unexpected))
                if bak:
                    shutil.copy2(bak, path)
                return out
            if sheet_part not in check:
                out["errors"].append("回读校验失败，新 sheet 部件缺失")
                if bak:
                    shutil.copy2(bak, path)
                return out
            ET.fromstring(check[sheet_part])  # 可解析
            try:
                import openpyxl
                wbk = openpyxl.load_workbook(path, read_only=True)
                if sheet_name not in wbk.sheetnames:
                    out["errors"].append("openpyxl 回读找不到新 sheet")
                    wbk.close()
                    if bak:
                        shutil.copy2(bak, path)
                    return out
                wbk.close()
            except ImportError:
                pass
            out["verified_parts_unchanged"] = len(orig) - len(changed)
        out["ok"] = True
        out["backup"] = bak
        return out
    except Exception as e:
        out["errors"].append("插入异常: %s: %s" % (type(e).__name__, e))
        return out


def _sheet_names(parts: dict) -> list:
    try:
        wb = ET.fromstring(parts["xl/workbook.xml"])
        sheets_el = wb.find(_prefix("sheets", NS_MAIN))
        if sheets_el is None:
            return []
        return [s.get("name") for s in sheets_el.findall(_prefix("sheet", NS_MAIN))]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 增量追加（2026-11-19 用户定案）：只新增、保留已有 sheet 与原有内容，
# 绝不覆盖 / 清空 / 替换任何已存在 sheet。sheet 不存在则新建后写入。
# ---------------------------------------------------------------------------

def _sheet_part_of(parts: dict, sheet_name: str) -> str | None:
    """按 sheet 名解析其真实 worksheet 部件路径（如 xl/worksheets/sheet2.xml）。"""
    try:
        wb = ET.fromstring(parts["xl/workbook.xml"])
        sheets_el = wb.find(_prefix("sheets", NS_MAIN))
        if sheets_el is None:
            return None
        rid = None
        for sh in sheets_el.findall(_prefix("sheet", NS_MAIN)):
            if sh.get("name") == sheet_name:
                rid = sh.get("{%s}id" % NS_REL_DOC)
                break
        if not rid:
            return None
        rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
        for rel in rels.findall(_prefix("Relationship", NS_REL_PKG)):
            if rel.get("Id") == rid:
                target = rel.get("Target", "")
                if target.startswith("/xl/"):
                    return target.lstrip("/")
                # 相对路径，相对 xl/ 目录
                return os.path.normpath(os.path.join("xl", target)).replace("\\", "/")
    except Exception:
        return None
    return None


def read_sheet_data(path: str, sheet_name: str,
                    fingerprint_cols: list | None = None) -> dict:
    """只读提取已有 sheet 的数据行与行号，用于增量去重与追加定位。

    返回 {ok, exists, sheet_part, max_row, data_start_row, headers,
          rows: [{row, values: [...], fp: str}], errors}
    - values 为每行按列序号（1-based）的文本值列表（inlineStr / 数值 / 共享串）。
    - fp 为去重指纹：默认用整行非空单元格值拼接；fingerprint_cols 可指定只看
      语义列序号（1-based），例如运输单按 [日期,车牌,司机,路线] 去重。
    - 不写任何文件、不改动任何部件。
    """
    out = {"ok": False, "exists": False, "sheet_part": None, "max_row": 0,
           "data_start_row": 2, "headers": [], "rows": [], "errors": []}
    if not os.path.isfile(path):
        out["errors"].append("目标文件不存在: %s" % path)
        return out
    parts = _read_parts(path)
    part = _sheet_part_of(parts, sheet_name)
    if not part or part not in parts:
        out["exists"] = False
        out["ok"] = True  # sheet 不存在是正常情况（将新建），不算错误
        return out
    out["exists"] = True
    out["sheet_part"] = part

    # 共享字符串表（inlineStr 之外的 t="s" 单元格）
    sst = []
    sst_key = "xl/sharedStrings.xml"
    if sst_key in parts:
        try:
            sst_root = ET.fromstring(parts[sst_key])
            for si in sst_root.findall(_prefix("si", NS_MAIN)):
                sst.append("".join(t.text or "" for t in si.iter(_prefix("t", NS_MAIN))))
        except Exception:
            sst = []

    def cell_value(c) -> str:
        t = c.get("t")
        if t == "inlineStr":
            is_el = c.find(_prefix("is", NS_MAIN))
            return "".join(x.text or "" for x in is_el.iter(_prefix("t", NS_MAIN))) if is_el is not None else ""
        v = c.find(_prefix("v", NS_MAIN))
        raw = v.text if (v is not None and v.text is not None) else ""
        if t == "s":
            try:
                return sst[int(raw)]
            except (ValueError, IndexError):
                return raw
        return raw

    def cell_style(c) -> int:
        try:
            return int(c.get("s", "0") or 0)
        except ValueError:
            return 0

    def col_num(ref: str) -> int:
        m = re.match(r"([A-Z]+)", ref or "")
        if not m:
            return 0
        n = 0
        for ch in m.group(1):
            n = n * 26 + (ord(ch) - 64)
        return n

    try:
        root = ET.fromstring(parts[part])
        sheet_data = root.find(_prefix("sheetData", NS_MAIN))
        if sheet_data is None:
            out["ok"] = True
            return out
        rows_out = []
        for row_el in sheet_data.findall(_prefix("row", NS_MAIN)):
            try:
                rnum = int(row_el.get("r", "0"))
            except ValueError:
                rnum = 0
            out["max_row"] = max(out["max_row"], rnum)
            vals = {}
            styles = {}
            for c in row_el.findall(_prefix("c", NS_MAIN)):
                ci = col_num(c.get("r", ""))
                if ci:
                    vals[ci] = cell_value(c)
                    styles[ci] = cell_style(c)
            if not vals:
                continue
            values = [vals.get(i, "") for i in range(1, max(vals) + 1)]
            if fingerprint_cols:
                key_vals = [values[i - 1] if i - 1 < len(values) else "" for i in fingerprint_cols]
            else:
                key_vals = values
            fp = "|".join((str(x) or "").strip() for x in key_vals)
            rows_out.append({"row": rnum, "values": values, "fp": fp,
                             "styles": styles})
        # 表头取第一行文本
        if rows_out:
            out["headers"] = rows_out[0]["values"]
            data_rows = [r for r in rows_out if r["row"] >= out["data_start_row"]]
            if data_rows:
                out["data_start_row"] = min(r["row"] for r in data_rows)
        out["rows"] = rows_out
        out["ok"] = True
        return out
    except Exception as e:
        out["errors"].append("读取 sheet 数据失败: %s: %s" % (type(e).__name__, e))
        return out


def append_rows(path: str, sheet_name: str, row_xmls: list,
                backup_dir: str | None = None, verify: bool = True,
                extra_parts: dict | None = None) -> dict:
    """把新行 XML 追加到已有 sheet 的 sheetData 末尾（绝不改动已有行）。

    与 fill_sheet 的本质区别：fill_sheet 整段替换 worksheet 字节，
    append_rows 只在 </sheetData> 前插入新 <row>，原有行字节保持不变。
    因此已有内容、公式、样式索引一律不动。备用 styles.xml（extra_parts）仍可
    随本次原子写入，用于让新增行的样式索引生效（桥接采用追加式，不改老索引）。

    row_xmls: list[str]，每个元素形如 '<row r="12">...</row>'，行号由调用方分配。
    """
    out = {"ok": False, "file": path, "sheet": sheet_name,
           "appended": 0, "next_row": None, "errors": []}
    if not os.path.isfile(path):
        out["errors"].append("目标文件不存在: %s" % path)
        return out
    parts = _read_parts(path)
    order = list(parts.keys())
    orig = dict(parts)
    part = _sheet_part_of(parts, sheet_name)
    if not part or part not in parts:
        out["errors"].append("找不到 sheet: %s" % sheet_name)
        return out
    if extra_parts:
        parts.update(extra_parts)
    try:
        xml = parts[part].decode("utf-8")
        marker = "</sheetData>"
        pos = xml.find(marker)
        if pos < 0:
            out["errors"].append("sheet 缺少 sheetData 节点，无法追加")
            return out
        if not row_xmls:
            out["ok"] = True  # 无新增行，零改动
            return out
        new_xml = xml[:pos] + "".join(row_xmls) + xml[pos:]
        ET.fromstring(new_xml)  # 合法性校验
        parts[part] = new_xml.encode("utf-8")
        bak = _backup(path, backup_dir) if backup_dir else None
        _write_parts(path, parts, order_hint=order)
        if verify:
            check = _read_parts(path)
            changed = [n for n in orig if n in check and check[n] != orig[n]]
            allowed = {part}
            if extra_parts:
                allowed |= set(extra_parts.keys())
            unexpected = [n for n in changed if n not in allowed]
            if unexpected:
                out["errors"].append("回读校验失败，意外改动部件: %s" % ", ".join(unexpected))
                if bak:
                    shutil.copy2(bak, path)
                return out
            ET.fromstring(check[part])
        out["ok"] = True
        out["appended"] = len(row_xmls)
        out["backup"] = bak
        return out
    except Exception as e:
        out["errors"].append("追加异常: %s: %s" % (type(e).__name__, e))
        return out


def create_workbook_from_template(template: str, out_path: str,
                                   sheet_name: str, sheet_xml: str,
                                   backup_dir: str | None = None,
                                   extra_parts: dict | None = None) -> dict:
    """目标司机文件不存在时，以模板为骨架新建文件并写入首个目标 sheet。

    基于模板 zip 复制全部部件，仅替换唯一 worksheet 内容并改其 sheet 名，
    最大程度保留 WPS 特有部件（customXml/metadata/vmlDrawing 等）。
    extra_parts: 需一并写入的部件（如桥接后的 xl/styles.xml），保证 sheet_xml 中
      的样式索引有对应定义。
    """
    out = {"ok": False, "file": out_path, "errors": []}
    if not os.path.isfile(template):
        out["errors"].append("模板文件不存在: %s" % template)
        return out
    try:
        parts = _read_parts(template)
        sheet_parts = sorted(n for n in parts
                             if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheet_parts:
            out["errors"].append("模板无 worksheet 部件")
            return out
        first = sheet_parts[0]
        parts[first] = sheet_xml.encode("utf-8")
        if extra_parts:
            parts.update(extra_parts)
        # workbook.xml：把首个 sheet 改名为目标名
        wb = ET.fromstring(parts["xl/workbook.xml"])
        sheets_el = wb.find(_prefix("sheets", NS_MAIN))
        sheets = sheets_el.findall(_prefix("sheet", NS_MAIN)) if sheets_el is not None else []
        if sheets:
            sheets[0].set("name", sheet_name)
        else:
            if sheets_el is None:
                sheets_el = ET.SubElement(wb, _prefix("sheets", NS_MAIN))
            sh = ET.SubElement(sheets_el, _prefix("sheet", NS_MAIN))
            sh.set("name", sheet_name)
            sh.set("sheetId", "1")
        parts["xl/workbook.xml"] = ET.tostring(wb, encoding="utf-8", xml_declaration=True)
        _write_parts(out_path, parts)
        if os.path.isfile(out_path):
            ET.fromstring(_read_parts(out_path)[first])
        out["ok"] = True
        return out
    except Exception as e:
        out["errors"].append("新建工作簿异常: %s: %s" % (type(e).__name__, e))
        return out


def list_sheets(path: str) -> list:
    try:
        return _sheet_names(_read_parts(path))
    except Exception:
        return []  # 非 xlsx / 临时锁文件 / 损坏文件：视为无 sheet，不抛异常


def fill_sheet(path: str, sheet_name: str, sheet_xml: str,
               backup_dir: str | None = None, verify: bool = True,
               extra_parts: dict | None = None) -> dict:
    """替换既有 sheet 的 XML（用于 exist_policy=overwrite）。其余同 insert_sheet。"""
    out = {"ok": False, "file": path, "sheet": sheet_name, "errors": []}
    if not os.path.isfile(path):
        out["errors"].append("目标文件不存在: %s" % path)
        return out
    parts = _read_parts(path)
    order = list(parts.keys())
    orig = dict(parts)
    if extra_parts:
        parts.update(extra_parts)
    wb = ET.fromstring(parts["xl/workbook.xml"])
    sheets_el = wb.find(_prefix("sheets", NS_MAIN))
    target_rel_id = None
    for sh in (sheets_el.findall(_prefix("sheet", NS_MAIN)) if sheets_el is not None else []):
        if sh.get("name") == sheet_name:
            target_rel_id = sh.get("{%s}id" % NS_REL_DOC)
            break
    if not target_rel_id:
        out["errors"].append("找不到 sheet: %s" % sheet_name)
        return out
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    target_rel = None
    for rel in rels.findall(_prefix("Relationship", NS_REL_PKG)):
        if rel.get("Id") == target_rel_id:
            target_rel = rel.get("Target")
            break
    if not target_rel:
        out["errors"].append("找不到 sheet relationship: %s" % target_rel_id)
        return out
    sheet_part = ("xl/" + target_rel) if not target_rel.startswith("/xl/") else target_rel.lstrip("/")
    try:
        parts[sheet_part] = sheet_xml.encode("utf-8")
        bak = _backup(path, backup_dir) if backup_dir else None
        _write_parts(path, parts, order_hint=order)
        if verify:
            check = _read_parts(path)
            changed = [n for n in orig if n in check and check[n] != orig[n]]
            allowed = {sheet_part}
            if extra_parts:
                allowed |= set(extra_parts.keys())
            unexpected = [n for n in changed if n not in allowed]
            if unexpected:
                out["errors"].append("回读校验失败，意外改动: %s" % ", ".join(unexpected))
                if bak:
                    shutil.copy2(bak, path)
                return out
        out["ok"] = True
        out["backup"] = bak
        out["replaced_part"] = sheet_part
        return out
    except Exception as e:
        out["errors"].append("替换异常: %s: %s" % (type(e).__name__, e))
        return out
