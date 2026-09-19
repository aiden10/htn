"""Conservative Lua source minifier for badge build artifacts.

It preserves the initial badge manifest and string literals verbatim, removes
comments/whitespace elsewhere, and only inserts spaces where adjacent tokens
would change meaning if joined.  It deliberately does not rename identifiers.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path


LONG_OPEN = re.compile(r"\[(=*)\[")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER = re.compile(r"(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)")
MULTI = ("...", "..", "==", "~=", "<=", ">=", "::", "//", "<<", ">>")


def long_end(source: str, start: int) -> int | None:
    match = LONG_OPEN.match(source, start)
    if not match:
        return None
    end = source.find("]" + match.group(1) + "]", match.end())
    return len(source) if end < 0 else end + len(match.group(1)) + 2


def tokens(source: str):
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue

        if source.startswith("--", index):
            bracket_end = long_end(source, index + 2)
            if bracket_end is not None:
                index = bracket_end
            else:
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline + 1
            continue

        if char in "\"'":
            quote = char
            end = index + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                elif source[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
            yield source[index:end]
            index = end
            continue

        bracket_end = long_end(source, index)
        if bracket_end is not None:
            yield source[index:bracket_end]
            index = bracket_end
            continue

        match = IDENTIFIER.match(source, index) or NUMBER.match(source, index)
        if match:
            yield match.group(0)
            index = match.end()
            continue

        operator = next((item for item in MULTI if source.startswith(item, index)), None)
        if operator:
            yield operator
            index += len(operator)
        else:
            yield char
            index += 1


def need_space(previous: str, current: str) -> bool:
    if previous[-1:].isalnum() or previous[-1:] == "_":
        if current[:1].isalnum() or current[:1] == "_":
            return True
    # Avoid accidentally creating a comment or a multi-character operator.
    joined = previous[-1:] + current[:1]
    return joined in {"--", "..", "==", "~=", "<=", ">=", "::", "//", "<<", ">>"}


def minify(source: str) -> str:
    manifest_end = source.find("]==]")
    if source.startswith("--[==[badge-app") and manifest_end >= 0:
        manifest = source[: manifest_end + 4].rstrip() + "\n"
        source = source[manifest_end + 4 :]
    else:
        manifest = ""

    output: list[str] = []
    previous = ""
    for token in tokens(source):
        if previous and need_space(previous, token):
            output.append(" ")
        output.append(token)
        previous = token
    return manifest + "".join(output) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    original = args.source.read_text(encoding="utf-8")
    result = minify(original)
    if args.verify and list(tokens(original)) != list(tokens(result)):
        raise SystemExit("minifier verification failed: token stream changed")
    args.output.write_text(result, encoding="utf-8", newline="\n")
    print(f"Wrote {args.output} ({len(result.encode('utf-8'))} bytes)")


if __name__ == "__main__":
    main()
