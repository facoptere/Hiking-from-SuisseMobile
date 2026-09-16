#!/usr/bin/env python3
"""Affiche le payload cache Scrapy pour une URL."""

from __future__ import annotations

import argparse
import gzip
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = ROOT / "httpcache"


def gzip_read(path: Path) -> bytes:
    with gzip.open(path, "rb") as fh:
        return fh.read()


def maybe_decompress(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    try:
        import brotli

        return brotli.decompress(data)
    except Exception:
        pass
    try:
        import zlib

        return zlib.decompress(data)
    except Exception:
        return data


def decode_bytes(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def load_meta(entry_dir: Path) -> dict:
    return pickle.loads(gzip_read(entry_dir / "pickled_meta"))


def find_entries(cache_dir: Path, url: str) -> list[tuple[Path, dict]]:
    matches = []
    if not cache_dir.exists():
        return matches
    for meta_path in cache_dir.rglob("pickled_meta"):
        entry_dir = meta_path.parent
        meta = load_meta(entry_dir)
        if url in {meta.get("url"), meta.get("response_url")}:
            matches.append((entry_dir, meta))
    return matches


def print_entry(entry_dir: Path, meta: dict) -> None:
    request_headers = gzip_read(entry_dir / "request_headers")
    request_body = gzip_read(entry_dir / "request_body")

    print(f"cache: {entry_dir}")
    print(f"method: {meta.get('method')}")
    print(f"url: {meta.get('url')}")
    print(f"response_url: {meta.get('response_url')}")
    print(f"status: {meta.get('status')}")
    print("--- request headers ---")
    print(decode_bytes(request_headers), end="" if request_headers.endswith(b"\n") else "\n")
    print("--- request payload ---")
    payload = decode_bytes(request_body)
    if payload:
        print(payload, end="" if payload.endswith("\n") else "\n")
    else:
        print("(vide)")
    response_body = maybe_decompress(gzip_read(entry_dir / "response_body"))
    print("--- response body ---")
    text = decode_bytes(response_body)
    print(text, end="" if text.endswith("\n") else "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print Scrapy httpcache request payload for a URL")
    parser.add_argument("url", help="URL exacte de la requete cachee")
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Dossier httpcache (defaut: ./httpcache)",
    )
    args = parser.parse_args()

    matches = find_entries(Path(args.cache_dir), args.url)
    if not matches:
        print(f"aucune entree cache pour {args.url}", file=sys.stderr)
        return 1

    for i, (entry_dir, meta) in enumerate(matches):
        if i:
            print()
        print_entry(entry_dir, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
