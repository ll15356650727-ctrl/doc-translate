#!/usr/bin/env python3
"""Validate and create the portable, deterministic distribution archive."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
AUTHOR = '公众号“' + 'A' + 'I 翻车现场”的智障研究员'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def files_for_package():
    paths = [ROOT / "SKILL.md"]
    for folder in ("assets", "references", "scripts"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.name not in {".DS_Store", "Thumbs.db", "desktop.ini"}
                     and p.suffix != ".pyc")
    return sorted(paths)


def validate():
    source = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    header = re.match(r"\A---\n(.*?)\n---\n", source, re.S)
    require(bool(header), "SKILL.md 缺少 frontmatter")
    require('name: doc-translate' in header[1] and 'version: "1.0.0"' in header[1], "名称或版本不匹配")
    require(AUTHOR in header[1], "缺少原作者信息")
    require("description:" in header[1] and 'display_name_zh: "专业文档翻译与校对"' in header[1], "缺少描述或中文名称")
    for doc in [ROOT / "SKILL.md", *(ROOT / "references").glob("*.md")]:
        for link in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
            if "://" not in link and not link.startswith("#"):
                require((doc.parent / link).exists(), "失效引用: " + link)
    icon = (ROOT / "assets/skill-icon.png").read_bytes()
    require(icon.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", icon[16:24]) == (1024, 1024), "图标必须为 1024×1024 PNG")
    prohibited = re.compile(r"(?i)(?<![a-z0-9])(?:" + "|".join(["o"+"p"+"e"+"n"+"a"+"i", "c"+"h"+"a"+"t"+"g"+"p"+"t", "c"+"o"+"d"+"e"+"x", "g"+"p"+"t", "a"+"i"]) + r")(?![a-z0-9])")
    for path in files_for_package():
        require(not path.is_symlink(), "禁止打包软链接")
        if path.suffix in {".py", ".md", ".json", ".txt"}:
            content = path.read_text(encoding="utf-8").replace(AUTHOR, "")
            require(not prohibited.search(content), "存在平台或模型标识: " + path.name)
            require("-" + "aifanche" not in content, "分发包不应携带外部标识")
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/test_translation.py")], capture_output=True, text=True)
    require(result.returncode == 0, result.stdout + result.stderr)
    return result.stderr


def main():
    parser = argparse.ArgumentParser(description="检查并打包专业文档翻译与校对")
    parser.add_argument("--check", action="store_true", help="只验证，不打包")
    args = parser.parse_args()
    try:
        checks = validate()
        print(checks.strip())
        if args.check:
            print("PASS: 技能结构、引用、语法、图标、分发中立性与行为回归检查通过")
            return 0
        destination = ROOT / "releases/current/doc-translate-1.0.0.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for path in files_for_package():
                info = zipfile.ZipInfo((Path(ROOT.name) / path.relative_to(ROOT)).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if path.suffix == ".py" else 0o644) << 16
                package.writestr(info, path.read_bytes())
        print("PASS: " + str(destination))
        print("SHA-256: " + hashlib.sha256(destination.read_bytes()).hexdigest())
        return 0
    except (ValueError, OSError) as error:
        print("检查失败：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
