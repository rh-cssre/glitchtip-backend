"""Print the offset of each function in the fixture binaries as JSON.

Offsets are relative to the image's load address, which is what a native
frame's instruction_addr minus its image_addr gives. The tests build their
events from these, so a rebuild with a different compiler moves the
addresses without breaking the events.
"""

import json
import re
import subprocess
import sys

FUNCTIONS = ("parse_value", "crash_here", "run_job", "main")


def nm_symbols(path, base):
    output = subprocess.run(
        ["llvm-nm", "--defined-only", "--demangle", path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    symbols = {}
    for line in output.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[1].lower() != "t":
            continue
        address, _, name = parts
        short = name.split("(")[0].split("::")[-1].lstrip("_")
        if short in FUNCTIONS:
            symbols[short] = int(address, 16) - base
    return symbols


def map_symbols(path):
    # lld-link /MAP "Publics by Value" lines: "<sec>:<off> <symbol> <Rva+Base> <obj>"
    symbols = {}
    with open(path) as f:
        text = f.read()
    base = int(re.search(r"Preferred load address is ([0-9a-fA-F]+)", text)[1], 16)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4 or not re.fullmatch(
            r"[0-9a-fA-F]{4}:[0-9a-fA-F]{8}", parts[0]
        ):
            continue
        name, address = parts[1], parts[2]
        for function in FUNCTIONS:
            if name == function or name.startswith(f"?{function}@"):
                symbols.setdefault(function, int(address, 16) - base)
    return symbols


elf, macho, pe_map = sys.argv[1:4]
print(
    json.dumps(
        {
            "elf": nm_symbols(elf, 0),
            # A Mach-O executable's __TEXT segment starts at 0x100000000.
            "macho": nm_symbols(macho, 0x100000000),
            "pe": map_symbols(pe_map),
        },
        indent=2,
        sort_keys=True,
    )
)
