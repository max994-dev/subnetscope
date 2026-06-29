#!/usr/bin/env python3
"""
Interactive loop: type 's' to read clipboard/input text and produce sn_list1.txt
with 2-char chunks woven into subnet names. Type 'q' to quit.
"""

import getpass
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_FILE = SCRIPT_DIR / "sn_list.txt"
OUTPUT_FILE = SCRIPT_DIR / "sn_list1.txt"


def get_clipboard() -> str:
    for cmd in [["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["pbpaste"]]:
        try:
            return subprocess.check_output(cmd, text=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return ""


def sanitize(chunk: str) -> str:
    return chunk.replace(" ", "_")


def encode(clip_text: str):
    lines = INPUT_FILE.read_text().splitlines()
    chunks = [clip_text[i:i+2] for i in range(0, len(clip_text), 2)]

    output_lines = []
    chunk_idx = 0

    for line in lines:
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 3:
            output_lines.append(line)
            continue

        sn_id = parts[0]
        name = parts[1]
        sn_type = ":".join(parts[2:])

        prefix = sanitize(chunks[chunk_idx]) if chunk_idx < len(chunks) else ""
        chunk_idx += 1
        suffix = sanitize(chunks[chunk_idx]) if chunk_idx < len(chunks) else ""
        chunk_idx += 1

        new_name = f"{prefix}{name}{suffix}"
        output_lines.append(f"{sn_id}:{new_name}:{sn_type}")

    OUTPUT_FILE.write_text("\n".join(output_lines) + "\n")
    print(f"Success. {len(output_lines)} lines written.")


def main():
    print("Subnet encoder ready.")
    print("  s = encode from clipboard (or type text after 's ')")
    print("  q = quit")
    print()

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue

        if user_input.lower() == "q":
            print("Bye.")
            break

        if user_input.lower() == "s":
            text = getpass.getpass("Paste text (hidden): ")
            if not text:
                print("No text provided.")
                continue
            encode(text)
        else:
            print("Unknown command. Use 's' or 'q'.")


if __name__ == "__main__":
    main()
