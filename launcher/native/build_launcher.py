#!/usr/bin/env python3
"""Rebuild the shipped native Windows launcher; maintainer tool only.

Normal users double-click the existing ``HoYo Project Studio.exe``. This script
requires a preinstalled MinGW-w64 x64 GCC and windres toolchain. It downloads
nothing, installs nothing, and never builds or bundles the Python application.

Example on Linux with an extracted toolchain::

    python launcher/native/build_launcher.py --toolchain-prefix \
        /path/to/toolchain/usr/bin/x86_64-w64-mingw32-

The same source, resources, and toolchain produce identical EXE bytes. PE build
timestamps are zero and compiler debug paths are not included. Build evidence
goes to qa/native_launcher_build_0415.json by default, outside release ZIPs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

VERSION = "0.4.15"
SOURCE = Path(__file__).resolve().parent
PROJECT = SOURCE.parent.parent


def default_output_path(project: Path) -> Path:
    """Keep one launcher beside app in releases; use the root in flat sources.

    PROJECT is the content root derived from this script's location. Releases
    deliberately name it app, so rebuilding from app/launcher/native also works
    if the previous outer EXE is missing. An explicit --output overrides this.
    """
    launcher_root = project.parent if project.name.casefold() == "app" else project
    return launcher_root / "HoYo Project Studio.exe"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_tool(prefix: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        name = prefix + suffix
        located = shutil.which(name)
        if located is not None:
            return str(Path(located).absolute())
    raise SystemExit(
        f"The MinGW-w64 maintainer tool {prefix + suffixes[0]} was not found. "
        "Use a preinstalled x64 MinGW-w64 toolchain or --toolchain-prefix. "
        "To open Studio normally, double-click the prebuilt HoYo Project Studio.exe."
    )


def run(command: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(
        command, cwd=SOURCE, env=environment, text=True,
        capture_output=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Native launcher build failed ({Path(command[0]).name}, "
            f"exit {result.returncode}):\n{result.stdout}{result.stderr}"
        )
    return result.stdout


def inspect_pe(path: Path) -> dict[str, object]:
    """Check the emitted binary directly without running Windows code."""
    data = path.read_bytes()
    if len(data) < 1024 or data[:2] != b"MZ":
        raise SystemExit("Compiler output is not a valid Windows executable.")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise SystemExit("Missing Windows PE signature.")
    machine, count, timestamp = struct.unpack_from("<HHI", data, pe + 4)
    optional_length = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    subsystem, dll_characteristics = struct.unpack_from("<HH", data, optional + 68)
    if (machine, magic, subsystem, timestamp) != (0x8664, 0x20B, 2, 0):
        raise SystemExit("Expected timestamp-free AMD64 PE32+ Windows GUI output.")
    if dll_characteristics & 0x160 != 0x160:
        raise SystemExit("ASLR, high-entropy ASLR, and NX must all be enabled.")
    sections: list[dict[str, object]] = []
    for index in range(count):
        entry = optional + optional_length + 40 * index
        raw_name = data[entry:entry + 8].split(b"\0", 1)[0]
        virtual_size, rva, raw_size, raw_offset = struct.unpack_from("<IIII", data, entry + 8)
        flags = struct.unpack_from("<I", data, entry + 36)[0]
        sections.append({
            "name": raw_name.decode("ascii"), "rva": rva,
            "virtual_size": virtual_size, "raw_offset": raw_offset,
            "raw_size": raw_size, "flags": flags,
        })

    def offset(rva: int) -> int:
        for section in sections:
            start = int(section["rva"])
            if start <= rva < start + max(int(section["virtual_size"]), int(section["raw_size"])):
                return int(section["raw_offset"]) + rva - start
        raise SystemExit(f"Invalid PE address: {rva}.")

    def cstring(address: int) -> str:
        start = offset(address)
        return data[start:data.index(0, start)].decode("ascii")

    imports_rva = struct.unpack_from("<I", data, optional + 112 + 8)[0]
    imports: dict[str, list[str]] = {}
    cursor = offset(imports_rva)
    while any(data[cursor:cursor + 20]):
        lookup, _, _, name, first_thunk = struct.unpack_from("<IIIII", data, cursor)
        names: list[str] = []
        thunk = offset(lookup or first_thunk)
        while True:
            address = struct.unpack_from("<Q", data, thunk)[0]
            if address == 0:
                break
            if address & (1 << 63):
                raise SystemExit("Unexpected ordinal import in the native launcher.")
            names.append(cstring(address + 2))
            thunk += 8
        imports[cstring(name)] = names
        cursor += 20
    if {name.lower() for name in imports} != {"kernel32.dll", "user32.dll"}:
        raise SystemExit(f"Unexpected external runtime dependency: {list(imports)}.")
    resources = next(section for section in sections if section["name"] == ".rsrc")
    resource_start = int(resources["raw_offset"])
    resource_data = data[resource_start:resource_start + int(resources["raw_size"])]
    if (SOURCE / "launcher.manifest").read_bytes() not in resource_data:
        raise SystemExit("The Windows manifest was not embedded exactly.")
    if f"{VERSION}.0".encode("utf-16-le") not in resource_data:
        raise SystemExit("The Windows file version was not embedded.")
    for section in sections:
        flags = int(section["flags"])
        if flags & 0x20000000 and flags & 0x80000000:
            raise SystemExit("A native launcher code section is unexpectedly writable.")
    return {
        "machine": "AMD64 (0x8664)", "format": "PE32+", "subsystem": "Windows GUI (2)",
        "coff_timestamp": timestamp, "dll_characteristics": hex(dll_characteristics),
        "imports": imports, "resource_section_sha256": hashlib.sha256(resource_data).hexdigest(),
        "embedded_manifest_matches_source": True, "embedded_version": f"{VERSION}.0",
        "sections": sections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain-prefix", default="x86_64-w64-mingw32-")
    parser.add_argument("--output", type=Path, default=default_output_path(PROJECT))
    parser.add_argument("--report", type=Path, default=PROJECT / "qa/native_launcher_build_0415.json")
    parser.add_argument("--toolchain-provenance", type=Path,
                        help="Optional locally saved, verified compiler provenance JSON.")
    args = parser.parse_args()
    compiler = resolve_tool(args.toolchain_prefix, ("gcc", "gcc-posix", "gcc-win32"))
    windres = resolve_tool(args.toolchain_prefix, ("windres",))
    environment = dict(os.environ)
    environment["PATH"] = str(Path(compiler).parent) + os.pathsep + environment.get("PATH", "")
    environment["SOURCE_DATE_EPOCH"] = "0"
    environment["LC_ALL"] = "C"
    target = run([compiler, "-dumpmachine"], environment).strip()
    if target != "x86_64-w64-mingw32":
        raise SystemExit(f"This launcher needs the x64 MinGW-w64 target, found {target}.")
    compiler_version = run([compiler, "--version"], environment).splitlines()[0]
    linker_version = run([compiler, "-Wl,--version"], environment).splitlines()
    linker_version = next((line for line in linker_version if "GNU ld" in line), linker_version[0])
    windres_version = run([windres, "--version"], environment).splitlines()[0]
    commands: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="hoyo-native-build-") as build_directory:
        temporary = Path(build_directory)
        resource = temporary / "launcher.res.o"
        binary = temporary / "HoYo Project Studio.exe"
        commands.append([
            windres, "--input", "launcher.rc", "--input-format", "rc",
            "--output-format", "coff", "--target", "pe-x86-64",
            "--codepage", "65001", "--language", "0x409",
            "--preprocessor", Path(compiler).name,
            "--preprocessor-arg=-E", "--preprocessor-arg=-xc",
            "--preprocessor-arg=-DRC_INVOKED", "--output", str(resource),
        ])
        commands.append([
            compiler, "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror", "-pedantic",
            "-fno-builtin", "-fno-ident", "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
            "-mwindows", "-nostdlib", "-nodefaultlibs", "-Wl,--entry,StudioEntryPoint",
            "-Wl,--subsystem,windows:6.0", "-Wl,--dynamicbase", "-Wl,--high-entropy-va",
            "-Wl,--nxcompat", "-Wl,--no-insert-timestamp", "-Wl,--disable-auto-import",
            "-Wl,--disable-runtime-pseudo-reloc", "-Wl,--strip-all",
            "launcher.c", str(resource), "-lkernel32", "-luser32", "-o", str(binary),
        ])
        for command in commands:
            run(command, environment)
        inspection = inspect_pe(binary)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        staged = output.with_name(output.name + ".new")
        shutil.copyfile(binary, staged)
        os.replace(staged, output)
    inputs = ["launcher.c", "launcher_args.h", "launcher.rc", "launcher.manifest", "build_launcher.py"]
    report: dict[str, object] = {
        "version": VERSION, "output": str(output), "bytes": output.stat().st_size,
        "sha256": sha256(output), "native_windows_runtime_tested": False,
        "compiler": compiler_version, "target": target, "linker": linker_version,
        "resource_compiler": windres_version, "commands": commands,
        "source_sha256": {name: sha256(SOURCE / name) for name in inputs},
        "source_icon_sha256": sha256(PROJECT / "resources/hoyo_project_studio.ico"),
        "pe": inspection,
    }
    if args.toolchain_provenance:
        report["toolchain_provenance"] = json.loads(args.toolchain_provenance.read_text())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "bytes": report["bytes"],
                      "sha256": report["sha256"], "report": str(args.report.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
