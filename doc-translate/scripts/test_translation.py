#!/usr/bin/env python3
"""Behavioral regressions with disposable multilingual document fixtures."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import sys

sys.dont_write_bytecode = True
import translate as t


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doc-translate-test-")
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.work = self.root / "work"

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        options = dict(input=str(self.input), target="en", work=str(self.work), glossary=None,
                       context=None, encoding=None, batch_chars=6000)
        options.update(kwargs)
        with redirect_stdout(io.StringIO()):
            t.prepare(argparse.Namespace(**options))
        return t.read_json(self.work / "manifest.json")

    def ingest(self, rows):
        path = self.root / "responses.jsonl"
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            return t.ingest(argparse.Namespace(work=str(self.work), responses=str(path)))

    def build(self):
        with redirect_stdout(io.StringIO()):
            return t.build(argparse.Namespace(work=str(self.work)))

    def answers(self, manifest, mapping):
        rows = []
        seen = set()
        for item in manifest["segments"]:
            if item["status"] != "pending" or item["cache_key"] in seen:
                continue
            seen.add(item["cache_key"])
            rows.append({"id": item["id"], "translation": mapping[item["masked"]],
                         "review": {"status": "PASS", "method": "fixture", "notes": "测试夹具的预期译文"}})
        return rows

    def test_markdown_roundtrip_preserves_structure(self):
        source = '# 说明\r\n\r\n| 名称 | 费用 |\r\n| --- | --- |\r\n| 会员 | 费用为 20 元 |\r\n\r\n```py\r\nprint("原样")\r\n```\r\n'
        path = self.input / "help.md"
        path.write_bytes(source.encode("utf-8"))
        manifest = self.prepare()
        mapping = {"说明": "Guide", "名称": "Name", "费用": "Fee", "会员": "Member", "费用为 ⟦P0000⟧ 元": "The fee is ⟦P0000⟧ yuan"}
        self.assertEqual(self.ingest(self.answers(manifest, mapping)), 0)
        self.assertEqual(self.build(), 0)
        result = (self.work / "translated/help.md").read_bytes().decode()
        self.assertIn("| Name | Fee |\r\n| --- | --- |", result)
        self.assertIn('print("原样")', result)
        self.assertEqual(path.read_bytes(), source.encode())
        self.assertEqual(t.read_json(self.work / "reports/translation-report.json")["status"], "NEEDS_CONFIRMATION")

    def test_bad_marker_rejected_and_good_rows_survive(self):
        (self.input / "a.txt").write_text("费用为 20 元\n\n请确认", encoding="utf-8")
        m = self.prepare()
        a, b = m["segments"]
        result = self.ingest([{"id": a["id"], "translation": "Fee 30"}, {"id": b["id"], "translation": "Please confirm"}])
        self.assertEqual(result, 1)
        self.assertEqual([s["status"] for s in t.read_json(self.work / "manifest.json")["segments"]], ["pending", "translated"])
        self.assertEqual(self.build(), 1)
        self.assertFalse((self.work / "translated/a.txt").exists())

    def test_incremental_only_new_content(self):
        path = self.input / "a.txt"
        path.write_text("欢迎\n\n保存成功", encoding="utf-8")
        m = self.prepare()
        self.ingest(self.answers(m, {"欢迎": "Welcome", "保存成功": "Saved successfully"}))
        path.write_text("欢迎\n\n保存成功\n\n请重试", encoding="utf-8")
        updated = self.prepare(target=None)
        self.assertEqual([s["source"] for s in updated["segments"] if s["status"] == "pending"], ["请重试"])
        self.assertEqual(t.read_json(self.work / "summary.json")["reused_segments"], 2)

    def test_duplicate_context_deduplicated(self):
        (self.input / "a.txt").write_text("确认\n\n确认\n", encoding="utf-8")
        m = self.prepare()
        self.assertEqual(t.read_json(self.work / "summary.json")["pending_unique"], 1)
        self.ingest(self.answers(m, {"确认": "Confirm"}))
        self.assertTrue(all(s["status"] == "translated" for s in t.read_json(self.work / "manifest.json")["segments"]))

    def test_context_prevents_blind_reuse(self):
        (self.input / "a.md").write_text("# 操作\n确认\n# 状态\n确认", encoding="utf-8")
        m = self.prepare()
        items = [s for s in m["segments"] if s["source"] == "确认"]
        self.assertNotEqual(items[0]["cache_key"], items[1]["cache_key"])

    def test_confirmed_protected_across_policy_change_and_prepare(self):
        (self.input / "a.txt").write_text("确认", encoding="utf-8")
        m = self.prepare()
        sid = m["segments"][0]["id"]
        self.ingest(self.answers(m, {"确认": "Confirm"}))
        with redirect_stdout(io.StringIO()):
            t.confirm(argparse.Namespace(work=str(self.work), id=sid))
        self.assertEqual(self.ingest([{"id": sid, "translation": "Approve"}]), 1)
        changed = self.prepare(context="另一场景")
        self.assertEqual(changed["segments"][0]["status"], "conflict")
        changed = self.prepare(target=None)
        self.assertEqual(changed["segments"][0]["status"], "conflict")
        self.assertEqual(self.build(), 1)

    def test_glossary_locked_and_latin_word_boundaries(self):
        g = {"schema_version": "1.0", "entries": [{"source": "关键结果", "target": "key result", "status": "confirmed"}, {"source": "API", "target": "API", "status": "confirmed", "keep": True}]}
        masked, protected, _ = t.mask("关键结果通过 API 更新", g)
        self.assertIn("key result", [p["replacement"] for p in protected])
        self.assertEqual(t.mask("CAPITAL", g)[0], "CAPITAL")
        self.assertIn("⟦P", masked)

    def test_links_variables_and_numbers_protected(self):
        source = "查看 [第 2 页](https://example.org/a) 的 `${count}` 与 {name}"
        masked, protected, _ = t.mask(source, {"schema_version": "1.0", "entries": []})
        self.assertTrue(any(p["source"] == "2" for p in protected))
        self.assertTrue(any("example.org" in p["source"] for p in protected))
        self.assertTrue(any("${count}" in p["source"] for p in protected))
        self.assertEqual(t.restore({"protected": protected}, masked), source)

    def test_bom_and_encoding_preserved(self):
        path = self.input / "a.txt"
        path.write_bytes("请确认\r\n".encode("utf-16"))
        m = self.prepare()
        self.ingest(self.answers(m, {"请确认": "Please confirm"}))
        self.assertEqual(self.build(), 0)
        data = (self.work / "translated/a.txt").read_bytes()
        self.assertTrue(data.startswith((b"\xff\xfe", b"\xfe\xff")))
        self.assertEqual(data.decode("utf-16"), "Please confirm\r\n")

    def test_source_change_blocks_write_and_archives_old_output(self):
        path = self.input / "a.txt"
        path.write_text("确认", encoding="utf-8")
        m = self.prepare()
        self.ingest(self.answers(m, {"确认": "Confirm"}))
        self.build()
        path.write_text("已修改", encoding="utf-8")
        self.assertEqual(self.build(), 1)
        self.assertFalse((self.work / "translated/a.txt").exists())
        self.assertTrue(list((self.work / "previous-outputs").rglob("a.txt")))

    def test_removed_source_not_in_current_delivery(self):
        path = self.input / "a.txt"
        path.write_text("确认", encoding="utf-8")
        m = self.prepare()
        self.ingest(self.answers(m, {"确认": "Confirm"}))
        self.build()
        path.unlink()
        self.prepare()
        self.build()
        self.assertFalse((self.work / "translated/a.txt").exists())

    def test_unsupported_and_corrupt_files_do_not_block_other_preflight(self):
        (self.input / "bad.pdf").write_bytes(b"x")
        (self.input / "bad.docx").write_bytes(b"x")
        (self.input / "good.txt").write_text("确认", encoding="utf-8")
        m = self.prepare()
        self.assertEqual(len(m["blocked"]), 2)
        self.assertEqual(len(m["files"]), 1)

    def test_unclosed_markdown_block_is_blocked(self):
        (self.input / "bad.md").write_text("```\n内容", encoding="utf-8")
        m = self.prepare()
        self.assertEqual(len(m["blocked"]), 1)

    def test_unknown_id_rejected(self):
        (self.input / "a.txt").write_text("确认", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.ingest([{"id": "unknown", "translation": "Confirm"}]), 1)

    def test_word_preserves_zip_members_and_structure(self):
        document = f'<w:document xmlns:w="{t.W}"><w:body><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>请确认</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>费用 20 元</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
        header = f'<w:hdr xmlns:w="{t.W}"><w:p><w:r><w:t>内部资料</w:t></w:r></w:p></w:hdr>'
        path = self.input / "memo.docx"
        with zipfile.ZipFile(path, "w") as p:
            p.writestr("word/document.xml", document)
            p.writestr("word/header1.xml", header)
            p.writestr("word/media/image1.png", b"unchanged-binary")
            p.writestr("word/styles.xml", '<styles xmlns="urn:test"/>')
        original = path.read_bytes()
        m = self.prepare()
        self.ingest(self.answers(m, {"请确认": "Please confirm", "费用 ⟦P0000⟧ 元": "Fee: ⟦P0000⟧ yuan", "内部资料": "Internal material"}))
        self.assertEqual(self.build(), 0)
        with zipfile.ZipFile(self.work / "translated/memo.docx") as p:
            self.assertIn(b"Please confirm", p.read("word/document.xml"))
            self.assertEqual(p.read("word/styles.xml"), b'<styles xmlns="urn:test"/>')
            self.assertEqual(p.read("word/media/image1.png"), b"unchanged-binary")
            self.assertEqual(t.xml_shape(p.read("word/document.xml")), t.xml_shape(document.encode()))
        self.assertEqual(original, path.read_bytes())

    def test_word_fields_are_skipped_and_reported(self):
        document = f'<w:document xmlns:w="{t.W}"><w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/><w:t>页码</w:t><w:fldChar w:fldCharType="end"/></w:r></w:p><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as p:
            p.writestr("word/document.xml", document)
        items, warnings = t.word_segments(buf.getvalue())
        self.assertEqual([s["source"] for s in items], ["正文"])
        self.assertTrue(warnings)

    def test_new_structure_or_numbers_rejected(self):
        s = {"source": "确认", "masked": "确认", "protected": [], "kind": "text"}
        self.assertTrue(t.check_translation(s, "Confirm | injected"))
        self.assertTrue(t.check_translation(s, "Confirm 200"))

    def test_report_escapes_untrusted_text(self):
        t.report_write(self.root / "report", {"status": "PASS_AUTOMATED", "files": [], "pairs": [{"id": "1", "source": "<script>alert(1)</script>", "translation": "<img src=x>"}]})
        result = (self.root / "report/translation-report.html").read_text()
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_audit_detects_digit_corruption_and_structure_changes(self):
        a, b = self.root / "source.md", self.root / "target.md"
        a.write_text("# 费用\n费用为 20 元", encoding="utf-8")
        b.write_text("## Fee\nThe fee is 30 yuan", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            code = t.audit(argparse.Namespace(source=str(a), translation=str(b), target="en", output=str(self.root / "audit"), glossary=None, encoding=None))
        self.assertEqual(code, 1)
        report = t.read_json(self.root / "audit/translation-report.json")
        self.assertIn("结构", str(report))
        self.assertIn("数值", str(report))

    def test_work_directory_inside_input_rejected(self):
        with self.assertRaises(ValueError):
            self.prepare(work=str(self.input / "work"))

    def test_old_response_rejected_after_target_language_change(self):
        (self.input / "a.txt").write_text("确认", encoding="utf-8")
        m = self.prepare()
        stale = self.answers(m, {"确认": "Confirm"})
        n = self.prepare(target="ja")
        self.assertNotEqual(m["segments"][0]["id"], n["segments"][0]["id"])
        self.assertEqual(self.ingest(stale), 1)

    def test_confirmed_translation_context_change_is_conflict(self):
        path = self.input / "a.md"
        path.write_text("# 操作\n确认", encoding="utf-8")
        m = self.prepare()
        self.ingest(self.answers(m, {"操作": "Actions", "确认": "Confirm"}))
        sid = next(s["id"] for s in m["segments"] if s["source"] == "确认")
        with redirect_stdout(io.StringIO()):
            t.confirm(argparse.Namespace(work=str(self.work), id=sid))
        path.write_text("# 状态\n确认", encoding="utf-8")
        n = self.prepare()
        self.assertEqual(next(s["status"] for s in n["segments"] if s["source"] == "确认"), "conflict")

    def test_signed_and_chinese_adjacent_numbers_are_locked(self):
        _, protected, _ = t.mask("余额-20元，增长+5%，第3页", {"schema_version": "1.0", "entries": []})
        self.assertEqual([p["source"] for p in protected], ["-20", "+5%", "3"])

    def test_format_review_tied_to_exact_output(self):
        path = self.input / "a.md"
        path.write_text("# 说明\n\n```\nkeep\n```", encoding="utf-8")
        m = self.prepare()
        self.ingest(self.answers(m, {"说明": "Guide"}))
        self.build()
        with redirect_stdout(io.StringIO()):
            t.record_review(argparse.Namespace(work=str(self.work), file="a.md", scope="format", status="PASS", notes="代码块原样保留已核对"))
        self.build()
        self.assertEqual(t.read_json(self.work / "reports/translation-report.json")["status"], "PASS_AUTOMATED")
        self.ingest([{"id": m["segments"][0]["id"], "translation": "Instructions"}])
        self.build()
        self.assertEqual(t.read_json(self.work / "reports/translation-report.json")["status"], "NEEDS_CONFIRMATION")

    def test_word_numeric_only_nodes_checked_in_audit(self):
        source, target = self.root / "a.docx", self.root / "b.docx"
        for path, number in [(source, "20"), (target, "30")]:
            with zipfile.ZipFile(path, "w") as p:
                p.writestr("word/document.xml", f'<w:document xmlns:w="{t.W}"><w:body><w:p><w:r><w:t>{number}</w:t></w:r></w:p></w:body></w:document>')
        with redirect_stdout(io.StringIO()):
            result = t.audit(argparse.Namespace(source=str(source), translation=str(target), target="en", output=str(self.root / "audit"), glossary=None, encoding=None))
        self.assertEqual(result, 1)

    def test_multibacktick_inline_code_kept_whole(self):
        source = "请保留 ``code`with`tick`` 与尾部文字"
        masked, protected, _ = t.mask(source, {"schema_version": "1.0", "entries": []})
        self.assertIn("``code`with`tick``", [p["source"] for p in protected])
        self.assertEqual(t.restore({"protected": protected}, masked), source)

    def test_word_field_spanning_paragraphs_is_not_translated(self):
        document = f'<w:document xmlns:w="{t.W}"><w:body><w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p><w:p><w:r><w:t>缓存值</w:t></w:r></w:p><w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as p:
            p.writestr("word/document.xml", document)
        items, warnings = t.word_segments(buf.getvalue())
        self.assertEqual([s["source"] for s in items], ["正文"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
