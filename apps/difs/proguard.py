"""Recognising ProGuard/R8 mapping files at upload."""

import codecs
import re


def extract_proguard_id(name: str):
    match = re.search("proguard/([-a-fA-F0-9]+).txt", name)
    if match is None:
        return
    return match.group(1)


# A ProGuard/R8 mapping file's class records always look like
# `originalClassName -> obfuscatedClassName:` at column 0; member records
# (fields/methods) are indented under a class record and never appear before
# one. So the first non-comment, non-blank line of a real mapping is always
# a class record — checking just that line is enough to tell a mapping file
# apart from arbitrary text, without parsing the rest of the file.
_PROGUARD_CLASS_RECORD_RE = re.compile(r"^\S+ -> \S+:$")

# Real mapping files put the first class record within the first few header
# comment lines, so a small prefix is always enough to find it. Bounding the
# sniff avoids decoding the whole file to `str`, which for a mapping in the
# tens of MBs would itself allocate a comparable amount of RAM just to look
# at the first line.
_PROGUARD_SNIFF_BYTES = 64 * 1024


def extract_proguard_metadata(content: bytes):
    """Cheaply sniff-check that this looks like a ProGuard mapping file.

    This intentionally avoids ProguardMapper.open(), which eagerly builds a
    full in-memory class/method index over the entire file just to answer
    yes/no here — for a mapping file in the tens of MBs that index build can
    itself use several hundred MB of RAM, which is wasted work since nothing
    below actually uses the parsed mapper. Deobfuscation uses a ProguardCache
    built from the mapping the first time an event needs it (apps.symbolication).
    """
    head = content[:_PROGUARD_SNIFF_BYTES].removeprefix(codecs.BOM_UTF8)

    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        try:
            decoded = stripped.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if _PROGUARD_CLASS_RECORD_RE.match(decoded) is None:
            return None
        break

    return {"arch": "any", "feature": "mapping"}
