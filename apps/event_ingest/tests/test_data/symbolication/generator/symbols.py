"""Print, as JSON, where each fixture image's functions and frames are.

Everything is an offset from the image's load address, which is what a
native frame's instruction_addr minus its image_addr gives. For each format:

* ``functions``: the start of each function.
* ``frames``: the addresses a real crash reports for the stack
  ``main -> app::run_job -> app::crash_here``, where crash_here faults
  writing through a null pointer. main and run_job report their return
  address (the instruction after the call); crash_here reports the faulting
  store, the first instruction of the ``*target = local;`` line.

The tests build their events from these, so a rebuild with a different
compiler moves the addresses without breaking the events.
"""

import json
import re
import subprocess
import sys

FUNCTIONS = ("parse_value", "crash_here", "run_job", "main")
# (frame, callee) pairs: the frame reports the return address of this call.
CALLS = {"main": "run_job", "run_job": "crash_here"}
FAULT_SOURCE_LINE = "*target = local;"


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def nm_functions(path):
    functions = {}
    for line in run("llvm-nm", "--defined-only", "--demangle", path).splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[1].lower() != "t":
            continue
        address, _, name = parts
        short = name.split("(")[0].split("::")[-1].lstrip("_")
        if short in FUNCTIONS:
            functions[short] = int(address, 16)
    return functions


def map_functions(path):
    # lld-link /MAP "Publics by Value" lines: "<sec>:<off> <symbol> <Rva+Base> <obj>"
    functions = {}
    with open(path) as f:
        text = f.read()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4 or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{8}", parts[0]):
            continue
        name, address = parts[1], parts[2]
        for function in FUNCTIONS:
            if name == function or name.startswith(f"?{function}@"):
                functions.setdefault(function, int(address, 16))
    return functions


def instructions(path):
    """(address, mnemonic, operands) for every instruction in the image."""
    result = []
    pattern = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)$")
    for line in run("llvm-objdump", "-d", "--no-show-raw-insn", path).splitlines():
        match = pattern.match(line)
        if match:
            result.append((int(match[1], 16), match[2], match[3]))
    return result


def source_line_number():
    with open("/src/native/crash.cpp") as f:
        for number, line in enumerate(f, 1):
            if line.strip() == FAULT_SOURCE_LINE:
                return number
    raise SystemExit("fault line not found in crash.cpp")


def symbolize_lines(binary, addresses, extra=()):
    output = run(
        "llvm-symbolizer",
        f"--obj={binary}",
        "--no-inlines",
        "--output-style=JSON",
        *extra,
        *(hex(address) for address in addresses),
    )
    lines = []
    for text in output.splitlines():
        records = json.loads(text)
        for record in records if isinstance(records, list) else [records]:
            symbol = record["Symbol"][0] if record.get("Symbol") else {}
            lines.append(symbol.get("Line"))
    return lines


def layout(binary, functions, base, extra=()):
    starts = sorted(functions.values())
    code = instructions(binary)

    def function_at(address):
        owner = None
        for name, start in functions.items():
            if start <= address and (owner is None or start > functions[owner]):
                owner = name
        if owner is not None and address >= max(starts) + 0x1000:
            return None
        return owner

    frames = {}
    for index, (address, mnemonic, operands) in enumerate(code):
        caller = function_at(address)
        if caller not in CALLS or not mnemonic.startswith(("call", "bl")):
            continue
        target = re.search(r"0x([0-9a-f]+)|^([0-9a-f]+)", operands)
        if not target:
            continue
        target = int(target[1] or target[2], 16)
        if target == functions[CALLS[caller]]:
            frames.setdefault(caller, code[index + 1][0])

    fault_line = source_line_number()
    in_crash_here = [a for a, _, _ in code if function_at(a) == "crash_here"]
    for address, line in zip(
        in_crash_here, symbolize_lines(binary, in_crash_here, extra)
    ):
        if line == fault_line:
            frames["crash_here"] = address
            break

    missing = {"main", "run_job", "crash_here"} - set(frames)
    if missing:
        raise SystemExit(f"{binary}: could not locate frames {missing}")
    return {
        "functions": {name: address - base for name, address in functions.items()},
        "frames": {name: address - base for name, address in frames.items()},
    }


elf, macho, macho_dsym, pe, pe_map = sys.argv[1:6]
with open(pe_map) as f:
    pe_base = int(
        re.search(r"Preferred load address is ([0-9a-fA-F]+)", f.read())[1], 16
    )
print(
    json.dumps(
        {
            "elf": layout(elf, nm_functions(elf), 0),
            # A Mach-O executable's __TEXT segment starts at 0x100000000.
            "macho": layout(
                macho,
                nm_functions(macho),
                0x100000000,
                extra=(f"--dsym-hint={macho_dsym}",),
            ),
            "pe": layout(pe, map_functions(pe_map), pe_base),
        },
        indent=2,
        sort_keys=True,
    )
)
