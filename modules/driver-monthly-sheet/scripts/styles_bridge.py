#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""styles_bridge.py —— 把模板 sheet 用到的样式（cellXfs 及其依赖的
fonts/fills/borders/numFmts）合并进目标工作簿的 xl/styles.xml。

原因：新 sheet 要「格式和字段跟模板一模一样」，必须复用模板的 cellXfs 样式。
但样式索引是相对各自工作簿的 styles.xml 的，直接照搬索引会指向错误样式。
本模块把模板 cellXfs 深拷贝进目标 styles.xml，并返回
    {模板cellXf索引: 目标cellXf索引}
的映射，供 sheet_writer 生成单元格 s= 属性。

只改目标 styles.xml 一个部件（由调用方 zip 写回），其余部件不动。
"""
from __future__ import annotations

import copy
from xml.etree import ElementTree as ET

from xlsxzip import NS_MAIN, _prefix

ET.register_namespace("", NS_MAIN)


def bridge_styles(template_styles_xml: bytes, target_styles_xml: bytes,
                  used_xf_indexes: set) -> tuple:
    """返回 (new_target_styles_bytes, mapping)。

    used_xf_indexes: 模板中被新 sheet 使用到的 cellXfs 索引集合。
    mapping: {模板xf索引: 目标xf索引}。
    """
    mapping = {}  # 模板cellXf索引 -> 目标cellXf索引
    ts = ET.fromstring(template_styles_xml)
    tg = ET.fromstring(target_styles_xml)

    fonts_t = _container(ts, "fonts")
    fills_t = _container(ts, "fills")
    borders_t = _container(ts, "borders")
    fmts_t = _container(ts, "numFmts")
    xfs_t = _container(ts, "cellXfs")

    fonts_g = _container(tg, "fonts")
    fills_g = _container(tg, "fills")
    borders_g = _container(tg, "borders")
    fmts_g = _ensure(tg, "numFmts", before=fonts_g)
    xfs_g = _container(tg, "cellXfs")

    # 目标工作簿的 numFmtId 复用表
    tgt_fmt_ids = {int(n.get("numFmtId", 0)) for n in fmts_g}
    fmt_delta = {}  # 模板 numFmtId -> 目标 numFmtId（仅自定义 >=164）

    tpl_xfs = xfs_t.findall(_prefix("xf", NS_MAIN))
    builtin_max = 163

    for xf_idx in sorted(used_xf_indexes):
        if xf_idx < 0 or xf_idx >= len(tpl_xfs):
            continue
        xf = tpl_xfs[xf_idx]
        new_xf = copy.deepcopy(xf)

        # numFmtId 映射（自定义格式重编号以避免与目标冲突）
        nid = int(xf.get("numFmtId", 0))
        if nid > builtin_max:
            src_fmt = None
            for n in fmts_t.findall(_prefix("numFmt", NS_MAIN)):
                if int(n.get("numFmtId", -1)) == nid:
                    src_fmt = n
                    break
            if src_fmt is not None and nid not in fmt_delta:
                code = src_fmt.get("formatCode", "")
                # 目标里若已有同 code，直接复用其 id
                reuse = None
                for n in fmts_g.findall(_prefix("numFmt", NS_MAIN)):
                    if n.get("formatCode", "") == code:
                        reuse = int(n.get("numFmtId"))
                        break
                if reuse is not None:
                    fmt_delta[nid] = reuse
                else:
                    new_id = max(tgt_fmt_ids | {builtin_max}) + 1
                    nf = ET.SubElement(fmts_g, _prefix("numFmt", NS_MAIN))
                    nf.set("numFmtId", str(new_id))
                    nf.set("formatCode", code)
                    tgt_fmt_ids.add(new_id)
                    fmt_delta[nid] = new_id
            if nid in fmt_delta:
                new_xf.set("numFmtId", str(fmt_delta[nid]))

        # font / fill / border：深拷贝进目标，重映射 id
        for tag, src_cont, tgt_cont in (("fontId", fonts_t, fonts_g),
                                        ("fillId", fills_t, fills_g),
                                        ("borderId", borders_t, borders_g)):
            sid = int(xf.get(tag, 0))
            tag_map = {"fontId": "font", "fillId": "fill", "borderId": "border"}[tag]
            srcs = src_cont.findall(_prefix(tag_map, NS_MAIN))
            tgts = tgt_cont.findall(_prefix(tag_map, NS_MAIN))
            if sid >= len(srcs):
                new_xf.set(tag, "0")
                continue
            # 用 XML 序列化串去重，避免重复追加
            src_sig = ET.tostring(srcs[sid], encoding="unicode")
            found = None
            for j, tnode in enumerate(tgts):
                if ET.tostring(tnode, encoding="unicode") == src_sig:
                    found = j
                    break
            if found is None:
                tgt_cont.append(copy.deepcopy(srcs[sid]))
                found = len(tgt_cont.findall(_prefix(tag_map, NS_MAIN))) - 1
            new_xf.set(tag, str(found))

        xfs_g.append(new_xf)
        mapping[xf_idx] = len(xfs_g.findall(_prefix("xf", NS_MAIN))) - 1

    # 更新各容器 count 属性
    for cont in (fonts_g, fills_g, borders_g, fmts_g, xfs_g):
        tag = cont.tag.split("}")[-1]
        child = {"fonts": "font", "fills": "fill", "borders": "border",
                 "numFmts": "numFmt", "cellXfs": "xf"}[tag]
        cont.set("count", str(len(cont.findall(_prefix(child, NS_MAIN)))))

    return ET.tostring(tg, encoding="utf-8", xml_declaration=True), mapping


def _container(root: ET.Element, name: str) -> ET.Element:
    el = root.find(_prefix(name, NS_MAIN))
    if el is None:
        el = ET.SubElement(root, _prefix(name, NS_MAIN))
        el.set("count", "0")
    return el


def _ensure(root: ET.Element, name: str, before: ET.Element) -> ET.Element:
    el = root.find(_prefix(name, NS_MAIN))
    if el is None:
        el = ET.Element(_prefix(name, NS_MAIN))
        el.set("count", "0")
        root.insert(list(root).index(before), el)
    return el


def collect_used_xfs(info, cfg) -> set:
    """从 TemplateInfo 收集需要复制的模板 cellXf 索引。"""
    used = set()
    for _, d in info.header_cells.items():
        used.add(int(d.get("s", 0)))
    for v in info.row_style.values():
        used.add(int(v))
    used.add(0)
    return used
