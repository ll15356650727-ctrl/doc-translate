#!/usr/bin/env python3
"""Portable document translation preparation, incremental reuse and verification.

The host translates JSONL requests; this program never calls a translation service.
Python 3.10+, standard library only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import os
import zipfile
import xml.etree.ElementTree as ET

VERSION = "1.0.0"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = "{" + W + "}"
MARKER = re.compile(r"⟦P\d{4,}⟧")
SUPPORTED = {".md", ".txt", ".docx"}
PROTECT = re.compile(
    r"`+[^`\n]*`+|!?\[|\]\([^\n)]*\)|\]\[[^\]\n]*\]|\]|<[^>\n]+>|"
    r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\$\{[^}\n]+\}|\{\{[^}\n]+\}\}|\{[A-Za-z_][\w.:-]*\}|%\([^)]+\)[a-z]|%[sdif]|"
    r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b|"
    r"[+-]?\d+(?:[.,:/-]\d+)*(?:%|％)?|\\[^\s]|\*{1,3}|_{1,3}|~{2}"
)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        name = stream.name
        stream.write(data)
    os.replace(name, path)


def under(path, parent):
    return path == parent or parent in path.parents


def glossary_load(path=None, previous=None):
    value = read_json(path) if path else (previous or {"schema_version": "1.0", "entries": []})
    if value.get("schema_version") != "1.0" or not isinstance(value.get("entries"), list):
        raise ValueError("术语表需包含 schema_version=1.0 与 entries 数组")
    seen = {}
    for entry in value["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str) or not entry["source"]:
            raise ValueError("术语 source 必须是非空字符串")
        if entry.get("status") not in {"candidate", "confirmed"}:
            raise ValueError("术语 status 必须为 candidate 或 confirmed")
        if not isinstance(entry.get("target"), str) or not entry["target"]:
            raise ValueError("术语 target 必须是非空字符串")
        key = entry["source"] if entry.get("case_sensitive") else entry["source"].casefold()
        if key in seen:
            raise ValueError("重复或冲突术语: " + entry["source"])
        seen[key] = entry["target"]
        if entry.get("keep") and entry["target"] != entry["source"]:
            raise ValueError("keep 术语的 target 必须与 source 一致")
    return value


def term_pattern(entry):
    term = entry["source"]
    escaped = re.escape(term)
    if re.fullmatch(r"[A-Za-z0-9 _-]+", term):
        escaped = r"(?<![\w])" + escaped + r"(?![\w])"
    return re.compile(escaped, 0 if entry.get("case_sensitive") else re.I)


def mask(source, glossary):
    if MARKER.search(source):
        raise ValueError("原文包含保留的 ⟦P…⟧ 标记，请先另行处理该片段")
    terms = [e for e in glossary["entries"] if term_pattern(e).search(source)]
    candidates = []
    inline_end = -1
    for opening in re.finditer(r"`+", source):
        if opening.start() < inline_end:
            continue
        closing = re.search(r"(?<!`)" + re.escape(opening.group()) + r"(?!`)", source[opening.end():])
        if closing:
            end = opening.end() + closing.end()
            candidates.append((opening.start(), end, source[opening.start():end]))
            inline_end = end
        else:
            candidates.append((opening.start(), opening.end(), opening.group()))
    # Link labels remain translatable; delimiters and destinations stay protected.
    for m in PROTECT.finditer(source):
        candidates.append((m.start(), m.end(), m.group()))
    for entry in terms:
        if entry["status"] == "confirmed":
            candidates += [(m.start(), m.end(), entry["target"]) for m in term_pattern(entry).finditer(source)]
    # Longest candidate wins at each start; never overlap a protected range.
    candidates.sort(key=lambda x: (x[0], -(x[1]-x[0])))
    spans, end = [], -1
    for a, b, replacement in candidates:
        if a >= end:
            spans.append((a, b, replacement))
            end = b
    pieces, protected, offset = [], [], 0
    for index, (a, b, replacement) in enumerate(spans):
        token = f"⟦P{index:04d}⟧"
        pieces.extend([source[offset:a], token])
        protected.append({"token": token, "source": source[a:b], "replacement": replacement})
        offset = b
    pieces.append(source[offset:])
    return "".join(pieces), protected, terms


def decode_text(data, encoding=None):
    if encoding:
        codec = encoding
    elif data.startswith(b"\xef\xbb\xbf"):
        codec = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    else:
        codec = "utf-8"
    try:
        return data.decode(codec), codec
    except (UnicodeError, LookupError) as error:
        raise ValueError("无法按指定编码读取；请用 --encoding 明确编码: " + str(error))


def table_ranges(line):
    boundaries, tick, escaped = [], 0, False
    for i, c in enumerate(line):
        if escaped:
            escaped = False
            continue
        if c == "\\":
            escaped = True
        elif c == "`":
            tick ^= 1
        elif c == "|" and not tick:
            boundaries.append(i)
    if not boundaries:
        return [(0, len(line))]
    positions = [-1] + boundaries + [len(line)]
    return [(a + 1, b) for a, b in zip(positions, positions[1:]) if b > a + 1]


def text_segments(text, markdown):
    segments, warnings = [], []
    cursor, fence, front, html_block, heading = 0, None, False, False, ""
    lines = text.splitlines(keepends=True)
    for number, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        stripped = line.strip()
        start = cursor
        cursor += len(raw)
        if markdown:
            if number == 0 and stripped == "---":
                front = True
                warnings.append("Frontmatter 保留原样")
                continue
            if front:
                if stripped in {"---", "..."}:
                    front = False
                continue
            fm = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if fm:
                if fence is None:
                    fence = (fm[1][0], len(fm[1]))
                    warnings.append("代码围栏保留原样")
                elif fm[1][0] == fence[0] and len(fm[1]) >= fence[1] and not fm[2].strip():
                    fence = None
                continue
            if fence:
                continue
            if stripped.startswith("<") or html_block:
                if stripped:
                    html_block = True
                    warnings.append("HTML 区块保留原样，请检查其可见文字")
                    continue
                html_block = False
            if re.match(r"^( {4}|\t)", line):
                warnings.append("缩进代码/深层列表保留原样，请检查")
                continue
            if re.match(r"^\s*\[[^\]]+\]:", line) or re.fullmatch(r"[\s|:\-]+", line):
                continue
            if re.fullmatch(r"\s*(?:\*\s*){3,}", line):
                continue
        if not stripped:
            continue
        prefix = re.match(r"^\s*(?:(?:>\s*)*)(?:(?:#{1,6}\s+)|(?:[-+*]\s+(?:\[[ xX]\]\s*)?)|(?:\d+[.)]\s+))?", line) if markdown else re.match(r"^\s*", line)
        lower = prefix.end()
        if markdown and re.match(r"^\s*#", line):
            heading = line[lower:].strip()
        ranges = table_ranges(line) if markdown and "|" in line else [(lower, len(line))]
        for a, b in ranges:
            a = max(a, lower)
            a += len(line[a:b]) - len(line[a:b].lstrip())
            b = a + len(line[a:b].rstrip())
            if b <= a or not re.search(r"[^\W\d_]", line[a:b], re.UNICODE):
                continue
            source = line[a:b]
            context = heading or (lines[number-1].strip() if number else "")
            segments.append({"locator": f"line:{number+1}:{a}", "source": source,
                             "context": context[:600], "range": [start+a, start+b], "kind": "text"})
    if front or fence:
        raise ValueError("Markdown Frontmatter 或代码围栏未闭合，未提取此文件")
    return segments, sorted(set(warnings))


def xml_root(raw):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML 含不支持的实体定义")
    return ET.fromstring(raw)


def word_segments(data):
    segments, warnings = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        infos = package.infolist()
        if len(infos) != len({i.filename for i in infos}):
            raise ValueError("DOCX 含重复 ZIP 成员")
        if sum(i.file_size for i in infos) > 100_000_000:
            raise ValueError("DOCX 解包体积超过 100MB，本版需拆分处理")
        if any(i.flag_bits & 1 for i in infos) or any("_xmlsignatures/" in i.filename.lower() or "vbaproject" in i.filename.lower() for i in infos):
            raise ValueError("加密、签名或宏文档不支持")
        if "word/document.xml" not in package.namelist():
            raise ValueError("不是有效 DOCX：缺少 word/document.xml")
        for info in infos:
            if info.filename.endswith(".xml"):
                xml_root(package.read(info))
        for name in sorted(package.namelist()):
            if re.match(r"word/(?:charts/|embeddings/|media/)", name):
                warnings.append("图表、嵌入对象、图片内容未翻译")
            if not re.fullmatch(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name):
                continue
            raw = package.read(name)
            root = xml_root(raw)
            text = raw.decode("utf-8")
            # Patching bytes preserves namespaces, attributes, relationships and other members.
            match = re.search(r'xmlns:([\w]+)=["\']' + re.escape(W) + r'["\']', text)
            if not match:
                raise ValueError("Word 命名空间前缀不受支持: " + name)
            prefix = match[1]
            matches = list(re.finditer(r"<" + prefix + r":t\b[^>]*>([^<]*)</" + prefix + r":t\s*>", text))
            nodes = list(root.iter(NS + "t"))
            if len(matches) != len(nodes):
                raise ValueError("Word 文本节点结构需单独适配: " + name)
            contexts, skipped = {}, set()
            field_nodes, depth = set(), 0
            for child in root.iter():
                if child.tag == NS+"fldChar":
                    flag = child.get(NS+"fldCharType")
                    if flag == "begin":
                        depth += 1
                    elif flag == "end":
                        depth = max(0, depth-1)
                elif child.tag == NS+"t" and depth:
                    field_nodes.add(child)
            complex_tags = {NS+t for t in ("fldChar", "fldSimple", "instrText", "ins", "del", "txbxContent", "sdt", "oMath")}
            parents = {c: p for p in root.iter() for c in p}
            for i, node in enumerate(nodes):
                parent, ancestors = node, []
                while parent in parents:
                    parent = parents[parent]
                    ancestors.append(parent)
                paragraph = next((p for p in ancestors if p.tag == NS+"p"), None)
                context = "".join(paragraph.itertext()) if paragraph is not None else (node.text or "")
                contexts[i] = context[:1000]
                complex_ancestor = any(p.tag in complex_tags for p in ancestors)
                complex_paragraph = paragraph is not None and any(p.tag in complex_tags or p.tag.endswith("}oMath") for p in paragraph.iter())
                if complex_ancestor or complex_paragraph or node in field_nodes:
                    skipped.add(i)
                    warnings.append("含字段、修订、文本框、内容控件或公式的段落保留原样")
                if paragraph is not None and len(list(paragraph.iter(NS+"t"))) > 1:
                    warnings.append("存在拆分样式的文本节点，请结合段落审校译文及样式")
            for i, (node, m) in enumerate(zip(nodes, matches)):
                source = node.text or ""
                if i in skipped or not source.strip() or not re.search(r"[^\W\d_]", source, re.UNICODE):
                    continue
                segments.append({"locator": f"{name}:text:{i}", "source": source,
                                 "context": contexts[i], "range": [m.start(1), m.end(1)],
                                 "part": name, "kind": "word"})
    return segments, sorted(set(warnings))


def extract(path, encoding=None):
    data = path.read_bytes()
    if path.suffix.lower() == ".docx":
        segments, warnings = word_segments(data)
        return {"encoding": None, "sha256": sha(data), "segments": segments, "warnings": warnings}
    text, codec = decode_text(data, encoding)
    segments, warnings = text_segments(text, path.suffix.lower() == ".md")
    return {"encoding": codec, "sha256": sha(data), "segments": segments, "warnings": warnings}


def restore(segment, translation):
    result = translation
    for item in segment["protected"]:
        result = result.replace(item["token"], item["replacement"])
    return result


def check_translation(segment, translation):
    errors = []
    if not isinstance(translation, str) or not translation.strip():
        return ["译文为空或不是字符串"]
    expected = Counter(x["token"] for x in segment["protected"])
    if Counter(MARKER.findall(translation)) != expected:
        errors.append("保护标记缺失、重复或出现未知标记")
    if "\n" in translation or "\r" in translation or any(ord(c) < 32 for c in translation):
        errors.append("片段包含新增换行或控制字符")
    if segment["kind"] == "word":
        for pattern in (r"^\s*", r"\s*$"):
            if re.search(pattern, translation)[0] != re.search(pattern, segment["masked"])[0]:
                errors.append("Word 节点首尾空白发生变化，会影响相邻样式文字的连接")
                break
    # Prevent introduction of document syntax; existing syntax is represented by protected markers.
    bare = MARKER.sub("", translation)
    if segment["kind"] == "text" and re.search(r"[|<>`]|\*{1,3}|_{1,3}|\]\(", bare):
        errors.append("译文引入新的 Markdown/HTML 结构符号，需调整或单独适配")
    if Counter(re.findall(r"\d+(?:[.,:/-]\d+)*(?:%|％)?", MARKER.sub("", translation))) != Counter(re.findall(r"\d+(?:[.,:/-]\d+)*(?:%|％)?", MARKER.sub("", segment["masked"]))):
        errors.append("译文新增或改变未保护数值")
    return errors


def workspace(path):
    root = Path(path).resolve()
    manifest = read_json(root / "manifest.json")
    if manifest.get("schema_version") != "1.0":
        raise ValueError("不兼容的工作目录版本")
    return root, manifest


def write_requests(root, manifest, budget=6000):
    folder = root / "requests"
    folder.mkdir(parents=True, exist_ok=True)
    # Only our known generated batch names are refreshed.
    for old in folder.glob("batch-[0-9][0-9][0-9][0-9].jsonl"):
        old.unlink()
    groups = {}
    for segment in manifest["segments"]:
        if segment["status"] == "pending":
            groups.setdefault(segment["cache_key"], []).append(segment)
    batch, size, index = [], 0, 1
    for group in groups.values():
        item = group[0]
        request = {"id": item["id"], "text": item["masked"], "context": item["context"],
                   "terms": item["terms"], "protected": item["protected"], "occurrences": len(group)}
        length = len(json.dumps(request, ensure_ascii=False))
        if batch and size + length > budget:
            atomic(folder / f"batch-{index:04d}.jsonl", ("\n".join(batch)+"\n").encode("utf-8"))
            batch, size, index = [], 0, index+1
        batch.append(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        size += length
    if batch:
        atomic(folder / f"batch-{index:04d}.jsonl", ("\n".join(batch)+"\n").encode("utf-8"))


def prepare(args):
    source, root = Path(args.input).resolve(), Path(args.work).resolve()
    if not source.exists() or source.is_symlink():
        raise ValueError("输入不存在或是软链接")
    if (source.is_dir() and under(root, source)) or root == source:
        raise ValueError("工作目录必须在输入目录之外")
    prior = read_json(root / "manifest.json") if (root / "manifest.json").exists() else None
    if prior and prior["source_root"] != str(source):
        raise ValueError("工作目录属于另一输入路径，请新建工作目录")
    if args.batch_chars < 500:
        raise ValueError("批次预算至少为 500 字符")
    glossary = glossary_load(args.glossary, prior.get("glossary") if prior else None)
    policy = {"target": args.target or (prior or {}).get("policy", {}).get("target"),
              "context": args.context if args.context is not None else (prior or {}).get("policy", {}).get("context", "忠实原意，沿用原文语气"),
              "glossary": glossary}
    if not policy["target"]:
        raise ValueError("首次翻译必须指定 --target")
    policy_hash = sha(policy)
    chosen_encoding = args.encoding or (prior or {}).get("input_encoding")
    previous = (prior or {}).get("segments", [])
    old_by_key = {s["cache_key"]: s for s in previous if s["status"] in {"translated", "confirmed"}}
    confirmed = {(s["file"], s["source"]): s for s in previous if s["status"] in {"confirmed", "conflict"}}
    paths = [source] if source.is_file() else sorted(source.rglob("*"))
    files, segments, blocked, conflicts = [], [], [], []
    for path in paths:
        if path.is_dir() or path.is_symlink() or path.name in {".DS_Store", "Thumbs.db", "desktop.ini"} or any(p.startswith(".") for p in path.relative_to(source if source.is_dir() else source.parent).parts):
            continue
        relative = path.relative_to(source).as_posix() if source.is_dir() else path.name
        if path.suffix.lower() not in SUPPORTED:
            blocked.append({"file": relative, "reason": "1.0 不支持此格式"})
            continue
        try:
            extracted = extract(path, chosen_encoding)
            current = []
            for s in extracted.pop("segments"):
                masked, protected, terms = mask(s["source"], glossary)
                # Pure numbers/code/locked terms do not need a translation call.
                s.update({"id": sha([policy_hash, relative, s["locator"], s["source"], s["context"]])[:24], "file": relative,
                          "masked": masked, "protected": protected, "terms": terms,
                          "cache_key": sha([policy_hash, s["source"], s["context"]]), "status": "pending"})
                if not re.search(r"[^\W\d_]", MARKER.sub("", masked), re.UNICODE):
                    s.update({"translation": masked, "status": "translated", "review": {"status": "PASS", "method": "protected-only", "notes": "仅包含原样保护内容"}, "reuse": "protected"})
                elif s["cache_key"] in old_by_key:
                    old = old_by_key[s["cache_key"]]
                    s.update({k: old[k] for k in ("translation", "status", "review", "keep_original", "reason") if k in old})
                    s["reuse"] = "cached"
                elif (relative, s["source"]) in confirmed:
                    old = confirmed[(relative, s["source"])]
                    s.update({"status": "conflict", "previous_translation": old.get("previous_translation", old.get("translation")), "previous_protected": old.get("previous_protected", old["protected"])})
                    conflicts.append({"id": s["id"], "file": relative, "reason": "已确认译文的目标语言、术语或场景配置改变"})
                current.append(s)
            files.append({"file": relative, "path": str(path), **extracted})
            segments.extend(current)
        except (ValueError, OSError, zipfile.BadZipFile, ET.ParseError, UnicodeError) as error:
            blocked.append({"file": relative, "reason": str(error)})
    removed = sorted(set(f["file"] for f in (prior or {}).get("files", [])) - set(f["file"] for f in files))
    manifest = {"schema_version": "1.0", "version": VERSION, "source_root": str(source), "policy": policy,
                "policy_hash": policy_hash, "glossary": glossary, "files": files, "segments": segments,
                "blocked": blocked, "conflicts": conflicts, "removed_files": removed, "input_encoding": chosen_encoding,
                "file_reviews": (prior or {}).get("file_reviews", {}), "prepared_at": stamp(), "batch_chars": args.batch_chars}
    pending = [s for s in segments if s["status"] == "pending"]
    unique = {s["cache_key"]: s for s in pending}
    summary = {"files_read": len(files), "blocked": blocked, "conflicts": conflicts,
               "warnings": [{"file": f["file"], "items": f["warnings"]} for f in files if f["warnings"]],
               "segments": len(segments), "pending_unique": len(unique), "reused_segments": sum(s.get("reuse") == "cached" for s in segments),
               "source_characters": sum(len(s["source"]) for s in segments),
               "pending_unique_characters": sum(len(s["masked"]) for s in unique.values()),
               "reused_source_characters": sum(len(s["source"]) for s in segments if s.get("reuse") == "cached"),
               "deduplicated_source_characters": sum(len(s["source"]) for s in pending)-sum(len(s["source"]) for s in unique.values()),
               "measurement": "字符统计，非 Token 实测"}
    atomic(root / "manifest.json", manifest)
    atomic(root / "summary.json", summary)
    write_requests(root, manifest, args.batch_chars)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def ingest(args):
    root, manifest = workspace(args.work)
    by_id = {s["id"]: s for s in manifest["segments"]}
    failures, applied = [], 0
    for number, line in enumerate(Path(args.responses).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            response = json.loads(line)
            segment = by_id.get(response.get("id"))
            if not segment:
                raise ValueError("未知或已过期片段 ID")
            translation = response.get("translation")
            errors = check_translation(segment, translation)
            if errors:
                raise ValueError("；".join(errors))
            if segment["status"] == "conflict":
                raise ValueError("先 resolve 已确认译文冲突")
            if segment["status"] == "confirmed" and translation != segment["translation"]:
                raise ValueError("不得覆盖已确认译文，先显式 resolve")
            review = response.get("review", {"status": "UNVERIFIED", "method": "none", "notes": "未做语义审校"})
            if not isinstance(review, dict) or review.get("status") not in {"PASS", "NEEDS_CONFIRMATION", "UNVERIFIED"}:
                raise ValueError("review.status 无效")
            if review["status"] == "PASS" and not (review.get("method") and review.get("notes")):
                raise ValueError("通过审校需记录 method 和 notes")
            keep = bool(response.get("keep_original"))
            if keep and not response.get("reason"):
                raise ValueError("保留原文需 reason")
            if restore(segment, translation) == segment["source"] and not keep:
                review = {"status": "NEEDS_CONFIRMATION", "method": "unchanged", "notes": "译文与原文相同，需确认是否应保留"}
            for item in manifest["segments"]:
                if item["cache_key"] != segment["cache_key"] or item["status"] in {"conflict", "confirmed"}:
                    continue
                item.update({"translation": translation, "status": "translated", "review": review,
                             "keep_original": keep, "reason": response.get("reason", "")})
            applied += 1
        except (ValueError, TypeError, AttributeError) as error:
            failures.append({"line": number, "reason": str(error)})
    atomic(root / "manifest.json", manifest)
    atomic(root / "ingest-report.json", {"applied": applied, "failures": failures})
    write_requests(root, manifest, manifest["batch_chars"])
    print(json.dumps({"applied": applied, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def xml_shape(raw):
    root = xml_root(raw)
    def walk(node):
        return [node.tag, sorted(node.attrib.items()), None if node.tag == NS+"t" else node.text,
                node.tail, [walk(child) for child in node]]
    return walk(root)


def render_file(file, segments):
    path = Path(file["path"])
    data = path.read_bytes()
    if sha(data) != file["sha256"]:
        raise ValueError("源文件在 prepare 后已变化，请重新 prepare")
    for s in segments:
        if s["status"] not in {"translated", "confirmed"}:
            raise ValueError("缺少译文或有未解决的确认冲突: " + s["id"])
        errors = check_translation(s, s["translation"])
        if errors:
            raise ValueError("；".join(errors))
    if path.suffix.lower() != ".docx":
        source = data.decode(file["encoding"])
        result = source
        for s in sorted(segments, key=lambda x: x["range"][0], reverse=True):
            a, b = s["range"]
            if source[a:b] != s["source"]:
                raise ValueError("片段定位不符，请重建工作目录")
            result = result[:a] + restore(s, s["translation"]) + result[b:]
        return result.encode(file["encoding"])
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, "w") as target:
        for info in original.infolist():
            raw = original.read(info.filename)
            items = [s for s in segments if s.get("part") == info.filename]
            if items:
                text, before = raw.decode("utf-8"), raw
                for s in sorted(items, key=lambda x: x["range"][0], reverse=True):
                    a, b = s["range"]
                    text = text[:a] + html.escape(restore(s, s["translation"]), quote=False) + text[b:]
                raw = text.encode("utf-8")
                if xml_shape(before) != xml_shape(raw):
                    raise ValueError("Word 非文本结构发生变化")
            target.writestr(info, raw)
    result = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(result)) as package:
        if package.testzip():
            raise ValueError("Word 压缩包校验失败")
        for name in package.namelist():
            if name.endswith(".xml"):
                xml_root(package.read(name))
    return result


def report_write(folder, report):
    folder = Path(folder)
    atomic(folder / "translation-report.json", report)
    # No document HTML is executable in the offline report.
    esc = lambda x: html.escape(str(x))
    rows = "".join("<tr>" + "".join(f"<td>{esc(item.get(k, ''))}</td>" for k in ("file", "status", "detail")) + "</tr>" for item in report.get("files", []))
    pairs = "".join(f"<tr><td>{esc(s.get('id', ''))}</td><td>{esc(s.get('source', ''))}</td><td>{esc(s.get('translation', ''))}</td></tr>" for s in report.get("pairs", []))
    body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>文档翻译检查报告</title><style>body{{font:16px/1.7 system-ui,sans-serif;max-width:1080px;margin:40px auto;padding:0 20px;color:#183330;background:#fafcfb}}table{{border-collapse:collapse;width:100%;margin:20px 0}}th,td{{border:1px solid #ceded8;padding:12px;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}}th{{background:#e5f1ed}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf4f1;padding:16px;border-radius:8px}}</style>
<h1>文档翻译检查报告</h1><p>机械检查：{esc(report['status'])} · 语义：{esc(report.get('semantic', 'UNVERIFIED'))} · 版面：{esc(report.get('layout', 'UNVERIFIED'))}</p>
<table><thead><tr><th>文件</th><th>结论</th><th>说明</th></tr></thead><tbody>{rows}</tbody></table>
<h2>原文与译文</h2><table><thead><tr><th>片段</th><th>原文</th><th>译文</th></tr></thead><tbody>{pairs}</tbody></table>
<h2>完整记录</h2><pre>{esc(json.dumps(report, ensure_ascii=False, indent=2))}</pre></html>"""
    atomic(folder / "translation-report.html", body.encode("utf-8"))


def build(args):
    root, manifest = workspace(args.work)
    files, pairs = [], []
    current_names = {f["file"] for f in manifest["files"]}
    output_root = root / "translated"
    if output_root.exists():
        for stale in output_root.rglob("*"):
            if stale.is_file() and stale.relative_to(output_root).as_posix() not in current_names:
                archived = root / "previous-outputs" / stamp().replace(":", "-") / stale.relative_to(output_root)
                archived.parent.mkdir(parents=True, exist_ok=True)
                stale.replace(archived)
    for file in manifest["files"]:
        items = [s for s in manifest["segments"] if s["file"] == file["file"]]
        target = root / "translated" / file["file"]
        try:
            result = render_file(file, items)
            atomic(target, result)
            pending_review = any(s.get("review", {}).get("status") == "NEEDS_CONFIRMATION" for s in items)
            reviews = manifest.get("file_reviews", {}).get(file["file"], {})
            valid_reviews = {scope: entry for scope, entry in reviews.items() if entry.get("output_sha256") == sha(result) and entry.get("source_sha256") == file["sha256"]}
            format_checked = valid_reviews.get("format", {}).get("status") == "PASS"
            failed_review = any(r.get("status") == "NEEDS_CONFIRMATION" for r in valid_reviews.values())
            status = "NEEDS_CONFIRMATION" if pending_review or failed_review or (file["warnings"] and not format_checked) else "PASS_AUTOMATED"
            files.append({"file": file["file"], "status": status, "detail": file["warnings"] or "完整回填，保护内容检查通过", "output": str(target), "sha256": sha(result), "reviews": valid_reviews})
            pairs.extend({"id": s["id"], "source": s["source"], "translation": restore(s, s["translation"]), "review": s.get("review")} for s in items)
        except (ValueError, OSError, UnicodeError, ET.ParseError, zipfile.BadZipFile) as error:
            # An earlier successful output must not masquerade as this build's result.
            if target.exists():
                archived = root / "previous-outputs" / stamp().replace(":", "-") / file["file"]
                archived.parent.mkdir(parents=True, exist_ok=True)
                target.replace(archived)
            files.append({"file": file["file"], "status": "FAIL", "detail": str(error)})
    files.extend({"file": f["file"], "status": "FAIL", "detail": f["reason"]} for f in manifest["blocked"])
    status = "FAIL" if not files or any(f["status"] == "FAIL" for f in files) else ("NEEDS_CONFIRMATION" if any(f["status"] == "NEEDS_CONFIRMATION" for f in files) else "PASS_AUTOMATED")
    reviews = [s.get("review", {}).get("status", "UNVERIFIED") for s in manifest["segments"]]
    semantic = "NEEDS_CONFIRMATION" if "NEEDS_CONFIRMATION" in reviews else ("PASS_CONTEXTUAL" if reviews and all(s == "PASS" for s in reviews) else "UNVERIFIED")
    word_outputs = [f for f in files if f["file"].lower().endswith(".docx")]
    layout = "NOT_APPLICABLE" if not word_outputs else ("PASS_RECORDED" if all(f.get("reviews", {}).get("layout", {}).get("status") == "PASS" for f in word_outputs) else "UNVERIFIED")
    report = {"status": status, "semantic": semantic, "layout": layout, "generated_at": stamp(), "files": files, "pairs": pairs,
              "note": "机械检查不证明语义准确或版面一致；PASS_CONTEXTUAL 仅表示响应记录了上下文审校"}
    report_write(root / "reports", report)
    print(json.dumps({"status": status, "semantic": semantic, "report": str(root / "reports/translation-report.html"), "files": files}, ensure_ascii=False, indent=2))
    return 1 if status == "FAIL" else 0


def record_review(args):
    root, manifest = workspace(args.work)
    file = next((f for f in manifest["files"] if f["file"] == args.file), None)
    if not file or not args.notes.strip():
        raise ValueError("需要有效文件与实际检查说明")
    output = root / "translated" / file["file"]
    if not output.is_file() or sha(Path(file["path"]).read_bytes()) != file["sha256"]:
        raise ValueError("需先 build 成功且源文件没有变化")
    manifest.setdefault("file_reviews", {}).setdefault(args.file, {})[args.scope] = {
        "status": args.status, "notes": args.notes, "recorded_at": stamp(),
        "output_sha256": sha(output.read_bytes()), "source_sha256": file["sha256"]}
    atomic(root / "manifest.json", manifest)
    print("PASS: 已记录检查结果；再次 build 更新报告")


def resolve(args):
    root, manifest = workspace(args.work)
    segment = next((s for s in manifest["segments"] if s["id"] == args.id), None)
    if not segment:
        raise ValueError("未知片段 ID")
    if args.choice == "keep":
        if segment["status"] != "conflict":
            raise ValueError("keep 仅用于确认冲突")
        old = {**segment, "protected": segment["previous_protected"]}
        unmasked = restore(old, segment["previous_translation"])
        # Re-mask only if the preserved text still matches the new locked values.
        translated = unmasked
        for item in segment["protected"]:
            if translated.count(item["replacement"]) != 1:
                raise ValueError("旧译文无法按新保护规则保留，请人工调整后重译")
            translated = translated.replace(item["replacement"], item["token"], 1)
        errors = check_translation(segment, translated)
        if errors:
            raise ValueError("；".join(errors))
        segment.update({"translation": translated, "status": "confirmed", "review": {"status": "PASS", "method": "user-decision", "notes": "用户确认继续使用旧译文"}})
    else:
        segment["status"] = "pending"
        for key in ("translation", "review", "previous_translation", "previous_protected"):
            segment.pop(key, None)
    atomic(root / "manifest.json", manifest)
    write_requests(root, manifest, manifest["batch_chars"])
    print("PASS: 已更新 " + segment["id"])


def confirm(args):
    root, manifest = workspace(args.work)
    segment = next((s for s in manifest["segments"] if s["id"] == args.id), None)
    if not segment or segment["status"] != "translated":
        raise ValueError("只能确认已有完整译文的片段")
    segment["status"] = "confirmed"
    segment["review"] = {"status": "PASS", "method": "user-confirmed", "notes": "用户明确确认此译文"}
    atomic(root / "manifest.json", manifest)
    print("PASS: 已记录用户确认")


def structure_signature(path, extracted):
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as package:
            result = {}
            for name in package.namelist():
                if not re.fullmatch(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml", name):
                    continue
                raw = package.read(name)
                editable = {s["locator"] for s in extracted["segments"] if s.get("part") == name}
                preserved = [(i, n.text) for i, n in enumerate(xml_root(raw).iter(NS+"t")) if f"{name}:text:{i}" not in editable]
                result[name] = {"shape": xml_shape(raw), "preserved": preserved}
            return result
    source = path.read_bytes().decode(extracted["encoding"])
    for item in sorted(extracted["segments"], key=lambda s: s["range"][0], reverse=True):
        a, b = item["range"]
        source = source[:a] + "TRANSLATABLE" + source[b:]
    return source


def audit(args):
    source, translation, output = Path(args.source).resolve(), Path(args.translation).resolve(), Path(args.output).resolve()
    if source.suffix.lower() not in SUPPORTED or source.suffix.lower() != translation.suffix.lower():
        raise ValueError("审校要求同种支持格式的原文与译文")
    if under(source, output) or under(translation, output):
        raise ValueError("审校输出目录不能包含输入文件")
    a, b = extract(source, args.encoding), extract(translation, args.encoding)
    errors, pairs = [], []
    if structure_signature(source, a) != structure_signature(translation, b):
        errors.append("原文与译文的标题/表格/代码等非翻译结构发生变化，需人工对齐")
    if len(a["segments"]) != len(b["segments"]):
        errors.append("原文与译文片段数不一致，不能自动对齐")
    else:
        glossary = glossary_load(args.glossary)
        for i, (original, translated) in enumerate(zip(a["segments"], b["segments"])):
            src, dst = original["source"], translated["source"]
            sm, sp, terms = mask(src, glossary)
            tm, tp, _ = mask(dst, {"schema_version": "1.0", "entries": []})
            important = lambda items: Counter(x["source"] for x in items if not re.fullmatch(r"[\s*_~]+", x["source"]))
            expected = Counter(x["replacement"] for x in sp if not re.fullmatch(r"[\s*_~]+", x["source"]))
            actual = important(tp)
            # Locked terms may be plain prose; compare their occurrence separately.
            for term in terms:
                if term["status"] == "confirmed":
                    expected.pop(term["target"], None)
                    if term["target"] not in dst:
                        errors.append(f"片段 {i+1} 缺少已确认术语: {term['target']}")
            if expected != actual:
                errors.append(f"片段 {i+1} 数值/链接/代码/占位符需复核")
            if not dst.strip():
                errors.append(f"片段 {i+1} 空译")
            pairs.append({"id": str(i+1), "source": src, "translation": dst})
    files = [{"file": translation.name, "status": "FAIL" if errors else "PASS_AUTOMATED", "detail": errors or "保护内容初检通过；仍需语义审校"}]
    report_write(output, {"status": files[0]["status"], "semantic": "UNVERIFIED", "layout": "UNVERIFIED", "files": files, "pairs": pairs, "warnings": a["warnings"] + b["warnings"]})
    print(json.dumps({"status": files[0]["status"], "errors": errors, "report": str(output / "translation-report.html")}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description="文档翻译、增量复用与校对；不调用外部服务")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="提取内容并生成待翻译批次")
    p.add_argument("--input", required=True)
    p.add_argument("--target")
    p.add_argument("--work", required=True)
    p.add_argument("--glossary")
    p.add_argument("--context")
    p.add_argument("--encoding")
    p.add_argument("--batch-chars", type=int, default=6000)
    p.set_defaults(func=prepare)
    p = sub.add_parser("ingest", help="校验并合并翻译响应")
    p.add_argument("--work", required=True)
    p.add_argument("--responses", required=True)
    p.set_defaults(func=ingest)
    p = sub.add_parser("build", help="回填完整文件并生成检查报告")
    p.add_argument("--work", required=True)
    p.set_defaults(func=build)
    p = sub.add_parser("resolve", help="按用户决策处理确认冲突")
    p.add_argument("--work", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--choice", choices=["keep", "retranslate"], required=True)
    p.set_defaults(func=resolve)
    p = sub.add_parser("confirm", help="记录用户明确确认过的译文")
    p.add_argument("--work", required=True)
    p.add_argument("--id", required=True)
    p.set_defaults(func=confirm)
    p = sub.add_parser("audit", help="只检查现有原文和译文")
    p.add_argument("--source", required=True)
    p.add_argument("--translation", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--glossary")
    p.add_argument("--encoding")
    p.set_defaults(func=audit)
    p = sub.add_parser("record-review", help="记录实际执行过的格式或版面复核")
    p.add_argument("--work", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--scope", choices=["format", "layout"], required=True)
    p.add_argument("--status", choices=["PASS", "NEEDS_CONFIRMATION"], required=True)
    p.add_argument("--notes", required=True)
    p.set_defaults(func=record_review)
    p = sub.add_parser("doctor", help="检查核心依赖")
    p.set_defaults(func=lambda args: print(("PASS" if sys.version_info >= (3, 10) else "UNSUPPORTED") + ": Python " + sys.version.split()[0] + "；要求 3.10+，核心仅需标准库。Word 版面渲染工具需由运行环境另行提供。"))
    args = parser.parse_args()
    try:
        return args.func(args) or 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile, ET.ParseError) as error:
        print("无法完成：" + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
