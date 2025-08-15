#!/usr/bin/env python3
"""
Linux-only WinOLS Damos (.a2l) Translator — correctness-focused with debug

- Translates only text inside double quotes to preserve A2L syntax.
- Lets Google auto-detect source (no .detect() flakiness).
- Persistent per-file, per-language cache to avoid re-translating identical phrases.
- Retries with exponential backoff on network hiccups.
- Streams output to a new file: <original>.translated_<lang>.a2l
- Debug mode prints each translated fragment with line numbers and cache status.

Usage examples:
  python3 main.py /path/to/file.a2l --dest es
  python3 main.py /mnt/c/Users/you/file.a2l -t spanish --debug

Requires:
  pip install googletrans==4.0.0-rc1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Tuple

from googletrans import Translator, LANGUAGES

BANNER = " WinOLS Damos Translator (Linux, robust) ".center(83, "#")
QUOTE_RE = re.compile(r'"(.*?)"')  # non-greedy — typical A2L quoted blocks
CACHE_SAVE_INTERVAL_SEC = 5.0
RETRY_LIMIT = 5
BASE_BACKOFF = 0.75  # seconds
PROGRESS_EVERY_LINES = 200


# --------------------------- helpers ---------------------------

def normalize_lang(inp: str) -> str:
    """Accept 'es', 'spanish', 'Español' -> return two-letter code."""
    s = inp.strip().lower()
    if s in LANGUAGES:  # already a code like 'es'
        return s
    for code, name in LANGUAGES.items():
        if s == name.lower():
            return code
    matches = [code for code, name in LANGUAGES.items() if s in name.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"Ambiguous language name. Did you mean one of: {', '.join(matches)} ?")
    raise ValueError("Unrecognized language. Use a 2-letter code (e.g., 'en','es') or full name (e.g., 'spanish').")


def choose_output_path(path: Path, dest_code: str) -> Path:
    base = path.with_suffix("")        # strip extension
    ext = path.suffix or ".a2l"
    return Path(f"{base}.translated_{dest_code}{ext}")


def cache_path_for(path: Path, dest_code: str) -> Path:
    return path.with_suffix(f".cache_{dest_code}.json")


def load_cache(cache_file: Path) -> Dict[str, str]:
    if cache_file.is_file():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # Corrupt cache? Start fresh (keep file so we don't overwrite it blindly).
            return {}
    return {}


def save_cache(cache_file: Path, cache: Dict[str, str]) -> None:
    tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp.replace(cache_file)  # atomic-ish on same fs


def translate_text_frag(text: str, dest_code: str, translator: Translator) -> str:
    """Translate a single fragment with retries & backoff. Source auto-detected."""
    last_err = None
    for attempt in range(RETRY_LIMIT):
        try:
            return translator.translate(text, dest=dest_code).text
        except Exception as e:
            last_err = e
            sleep_s = BASE_BACKOFF * (2 ** attempt)
            print(f"[warn] translate retry {attempt+1}/{RETRY_LIMIT} after error: {e} "
                  f"(sleep {sleep_s:.2f}s)", file=sys.stderr)
            time.sleep(sleep_s)
    print("[warn] giving up on a fragment after repeated errors; leaving original text", file=sys.stderr)
    return text  # fallback: keep original


def translate_line(line: str,
                   dest: str,
                   translator: Translator,
                   cache: Dict[str, str],
                   debug: bool = False,
                   lineno: int | None = None) -> Tuple[str, bool]:
    """Translate all quoted fragments in a line; return (new_line, changed?)."""
    changed = False

    def _repl(m):
        nonlocal changed
        inner = m.group(1)
        if not inner.strip():
            return m.group(0)  # keep empty quotes as-is

        if inner in cache:
            out = cache[inner]
            source = "cache"
        else:
            out = translate_text_frag(inner, dest, translator)
            cache[inner] = out
            source = "google"

        if debug:
            # Truncate very long strings in debug for readability
            def trunc(s, n=180): return (s if len(s) <= n else s[:n] + "…")
            ln = f"{lineno}" if lineno is not None else "?"
            print(f"[line {ln}] ({source}) \"{trunc(inner)}\" -> \"{trunc(out)}\"")

        if out != inner:
            changed = True
        return f"\"{out}\""

    if '"' not in line:
        return line, False

    new_line = QUOTE_RE.sub(_repl, line)
    return new_line, changed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Linux-only A2L translator (quote-only, robust).")
    p.add_argument("path", nargs="?", help="Path to .a2l file (Linux path, e.g., /home/user/file.a2l or /mnt/c/...)")
    p.add_argument("--dest", "-t", help="Destination language (code or name), e.g., 'es' or 'spanish'")
    p.add_argument("--debug", action="store_true", help="Enable verbose debugging output")
    return p.parse_args()


# --------------------------- main ---------------------------

def main():
    print(BANNER)
    args = parse_args()

    # Path (allow interactive if omitted)
    if not args.path:
        print("Path to file.a2l (Linux path, e.g., /home/user/file.a2l or /mnt/c/...):")
        raw = input("> ").strip().strip('"').strip("'")
    else:
        raw = args.path.strip().strip('"').strip("'")

    path = Path(os.path.expanduser(raw))
    if not path.is_file():
        sys.exit(f"File not found: {path}")

    # Destination language (allow interactive if omitted)
    if not args.dest:
        print("Translate into which language? (e.g., 'es' or 'Spanish')")
        while True:
            try:
                dest = normalize_lang(input("> ").strip())
                break
            except ValueError as e:
                print(e)
    else:
        dest = normalize_lang(args.dest)

    out_path = choose_output_path(path, dest)
    cache_file = cache_path_for(path, dest)

    translator = Translator()
    cache = load_cache(cache_file)

    total_lines = 0
    changed_lines = 0
    last_save = time.time()

    print(f"# Input:   {path}")
    print(f"# Output:  {out_path}")
    print(f"# Cache:   {cache_file} (loaded {len(cache)} entries)")
    print(f"# Target:  {dest} ({LANGUAGES.get(dest, 'unknown')})")
    print("#" * 83)

    try:
        with path.open("r", encoding="latin1", errors="ignore") as fin, \
             out_path.open("w", encoding="latin1", errors="ignore") as fout:

            for lineno, line in enumerate(fin, 1):
                total_lines += 1

                if '"' in line:
                    new_line, chg = translate_line(
                        line.rstrip("\n"),
                        dest,
                        translator,
                        cache,
                        debug=args.debug,
                        lineno=lineno
                    )
                    if chg:
                        changed_lines += 1
                    fout.write(new_line + "\n")
                else:
                    fout.write(line)

                # periodic cache save + progress
                now = time.time()
                if now - last_save >= CACHE_SAVE_INTERVAL_SEC:
                    save_cache(cache_file, cache)
                    last_save = now
                if total_lines % PROGRESS_EVERY_LINES == 0 and not args.debug:
                    print(f"...processed {total_lines} lines, changed {changed_lines} lines, cache {len(cache)} entries")

    except KeyboardInterrupt:
        print("\n[info] interrupted by user (Ctrl+C). Saving cache...")
        save_cache(cache_file, cache)
        print(f"[info] partial output is in: {out_path}")
        sys.exit(1)

    # final save
    save_cache(cache_file, cache)

    print("#" * 83)
    print(f"Done. Wrote: {out_path}")
    print(f"Lines processed: {total_lines}, lines with translated quotes: {changed_lines}")
    print(f"Cache entries: {len(cache)}")
    print("#" * 83)


if __name__ == "__main__":
    main()
