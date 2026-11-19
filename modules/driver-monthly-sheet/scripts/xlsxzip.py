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
