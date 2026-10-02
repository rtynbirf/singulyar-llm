#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПРОВЕРКА_ЦЕЛОСТНОСТИ.py — сверка частей модели с MANIFEST.txt.

Запуск без автора, из любого места:
    python3 СБОРКА/ПРОВЕРКА_ЦЕЛОСТНОСТИ.py            # части
    python3 СБОРКА/ПРОВЕРКА_ЦЕЛОСТНОСТИ.py --merge    # части + склейка + целое

Пути считаются от этого файла: репа самодостаточна, машина автора не нужна.
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "MANIFEST.txt"


def parse_manifest():
    text = MANIFEST.read_text(encoding="utf-8")
    total_m = re.search(r"Размер:\s*([\d\s\u00a0]+)\s*байт", text)
    whole_m = re.search(r"SHA-256:\s*([0-9a-f]{64})", text)
    total = int(re.sub(r"[^\d]", "", total_m.group(1))) if total_m else None
    whole = whole_m.group(1) if whole_m else None
    parts = re.findall(r"(MODELS/\S+\.bin)\s+(\d+)\s+([0-9a-f]{64})", text)
    return total, whole, parts


def sha256_file(path, limit=None):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            read += len(b)
            if limit is not None and read >= limit:
                break
    return h.hexdigest(), read


def main():
    merge = "--merge" in sys.argv
    total, whole, parts = parse_manifest()
    if not parts:
        print("RED: манифест не содержит частей"); return 1
    ok = True
    merged = hashlib.sha256() if merge else None
    for rel, size_s, digest in parts:
        p = ROOT / rel
        if not p.exists():
            print(f"RED: нет файла {rel}"); ok = False; continue
        digest_real, size_real = sha256_file(p)
        good = (digest_real == digest and size_real == int(size_s))
        print(("GREEN" if good else "RED") + f": {rel}  {size_real} байт")
        if not good:
            print(f"     манифест: {size_s} {digest}")
            print(f"     факт:     {size_real} {digest_real}")
            ok = False
        if merge and good:
            with open(p, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    merged.update(b)
    if merge:
        merged_hex = merged.hexdigest()
        good = (merged_hex == whole and merged_hex is not None)
        print(("GREEN" if good else "RED") + f": склейка SHA-256 {merged_hex}")
        if whole and not good:
            print(f"     манифест: {whole}")
            ok = False
    print("ИТОГ:", "GREEN — целостность подтверждена" if ok else "RED — расхождение найдено")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
