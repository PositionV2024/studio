"""Check the distributed Windows launcher, without claiming a Windows UI run.

The PE checks read the actual EXE shipped in the application folder, so a build
that accidentally drops its icon, requests elevation or adds an external DLL
fails before release. PE field offsets and resource IDs follow Microsoft's
specification: https://learn.microsoft.com/windows/win32/debug/pe-format
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET

import pytest

from studio import __version__


ROOT = Path(__file__).resolve().parents[1]


class PortableExecutable:
    """The small PE32+ reader needed to inspect this project's build output."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        assert data[:2] == b"MZ", "The included launcher must be a Windows executable."
        pe = self.unpack("<I", 0x3C)[0]
        assert data[pe:pe + 4] == b"PE\0\0"
        self.machine, count, _, _, _, optional_size, self.characteristics = self.unpack("<HHIIIHH", pe + 4)
        self.optional = pe + 24
        assert self.unpack("<H", self.optional)[0] == 0x20B, "Expected a PE32+ executable."
        assert optional_size >= 112
        directory_count = self.unpack("<I", self.optional + 108)[0]
        assert 112 + directory_count * 8 <= optional_size
        self.directories = [self.unpack("<II", self.optional + 112 + index * 8) for index in range(directory_count)]
        self.sections = []
        for index in range(count):
            offset = self.optional + optional_size + index * 40
            name, virtual_size, rva, size, position, _, _, _, _, flags = self.unpack("<8sIIIIIIHHI", offset)
            assert position + size <= len(data), "A PE section extends beyond the delivered file."
            self.sections.append((name.rstrip(b"\0"), virtual_size, rva, size, position, flags))

    def unpack(self, format: str, offset: int) -> tuple:
        assert 0 <= offset <= len(self.data) - struct.calcsize(format)
        return struct.unpack_from(format, self.data, offset)

    def offset(self, rva: int, size: int = 1) -> int:
        for _, _, section_rva, raw_size, position, _ in self.sections:
            if section_rva <= rva and rva + size <= section_rva + raw_size:
                return position + rva - section_rva
        raise AssertionError(f"The PE contains an unmapped RVA: {rva:#x}, size={size}.")

    def ascii_at(self, rva: int) -> str:
        start = self.offset(rva)
        end = self.data.index(b"\0", start)
        return self.data[start:end].decode("ascii")

    def imports(self) -> dict[str, set[str]]:
        rva, size = self.directories[1]
        assert rva and size
        result: dict[str, set[str]] = {}
        cursor = self.offset(rva, size)
        end = cursor + size
        while cursor + 20 <= end:
            original_thunk, timestamp, forwarders, name_rva, first_thunk = self.unpack("<IIIII", cursor)
            cursor += 20
            if not any((original_thunk, timestamp, forwarders, name_rva, first_thunk)):
                return result
            dll = self.ascii_at(name_rva).lower()
            symbols = result.setdefault(dll, set())
            thunk = self.offset(original_thunk or first_thunk, 8)
            while True:
                value = self.unpack("<Q", thunk)[0]
                thunk += 8
                if not value:
                    break
                assert not value & (1 << 63), "The launcher should use documented named OS imports."
                symbols.add(self.ascii_at(value + 2))
        raise AssertionError("The import descriptor table is missing its terminator.")

    def resources(self) -> dict[tuple[int, ...], bytes]:
        rva, size = self.directories[2]
        assert rva and size, "The launcher's icon and manifest must be embedded."
        base = self.offset(rva, size)
        result: dict[tuple[int, ...], bytes] = {}

        def directory(relative: int, path: tuple[int, ...]) -> None:
            assert len(path) < 4, "Unexpectedly deep resource tree."
            assert 0 <= relative < size
            _, _, _, _, named, numbered = self.unpack("<IIHHHH", base + relative)
            assert named == 0, "This launcher's resources use numeric Windows resource IDs."
            assert relative + 16 + numbered * 8 <= size
            for index in range(numbered):
                name, child = self.unpack("<II", base + relative + 16 + index * 8)
                assert not name & 0x80000000
                key = (*path, name)
                if child & 0x80000000:
                    directory(child & 0x7FFFFFFF, key)
                else:
                    assert child + 16 <= size
                    content_rva, length, _, _ = self.unpack("<IIII", base + child)
                    start = self.offset(content_rva, length)
                    result[key] = self.data[start:start + length]

        directory(0, ())
        return result


@pytest.fixture(scope="module")
def executable() -> PortableExecutable:
    path = ROOT / "HoYo Project Studio.exe"
    if ROOT.name.casefold() == "app":
        # The release has one visible entry point beside its support folder.
        # Keep the same checks runnable from the flat maintainer checkout.
        path = ROOT.parent / "HoYo Project Studio.exe"
    assert path.is_file(), "The download must include its ready-made launcher."
    return PortableExecutable(path.read_bytes())


def test_delivered_launcher_is_a_complete_64_bit_gui_executable(executable):
    assert executable.machine == 0x8664
    assert executable.characteristics & 0x0002  # IMAGE_FILE_EXECUTABLE_IMAGE
    assert not executable.characteristics & 0x2000  # IMAGE_FILE_DLL
    assert executable.unpack("<H", executable.optional + 68)[0] == 2  # WINDOWS_GUI
    entry = executable.unpack("<I", executable.optional + 16)[0]
    assert entry
    assert any(
        rva <= entry < rva + size and flags & 0x20000000
        for _, _, rva, size, _, flags in executable.sections
    ), "The entry point must refer to executable code in the delivered file."
    flags = executable.unpack("<H", executable.optional + 70)[0]
    assert flags & 0x0040 and flags & 0x0100  # DYNAMIC_BASE and NX_COMPAT
    assert not any(flags & 0x20000000 and flags & 0x80000000 for *_, flags in executable.sections)


def test_launcher_only_requires_builtin_windows_dlls(executable):
    imports = executable.imports()
    assert set(imports) == {"kernel32.dll", "user32.dll"}
    assert {"CreateProcessW", "GetModuleFileNameW", "CloseHandle"} <= imports["kernel32.dll"]
    assert "MessageBoxW" in imports["user32.dll"]
    assert executable.directories[13] == (0, 0), "There must be no delayed external DLL imports."
    assert executable.directories[14] == (0, 0), "The launcher must not require a separate .NET runtime."


def test_launcher_embeds_the_application_icon(executable):
    resources = executable.resources()
    groups = [(key, data) for key, data in resources.items() if key[0] == 14]
    assert groups
    source = (ROOT / "resources" / "hoyo_project_studio.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", source)
    assert (reserved, kind) == (0, 1) and count
    source_images = set()
    for index in range(count):
        size, start = struct.unpack_from("<II", source, 6 + index * 16 + 8)
        source_images.add(source[start:start + size])
    embedded_images = set()
    for group_key, data in groups:
        reserved, kind, count = struct.unpack_from("<HHH", data)
        assert (reserved, kind) == (0, 1) and count
        assert len(data) == 6 + count * 14
        for index in range(count):
            length, icon_id = struct.unpack_from("<IH", data, 6 + index * 14 + 8)
            image = resources[(3, icon_id, group_key[-1])]
            assert len(image) == length
            embedded_images.add(image)
    assert embedded_images == source_images, "Explorer must receive this application's complete icon."


def test_launcher_version_matches_the_application(executable):
    versions = [data for key, data in executable.resources().items() if key[0] == 16]
    assert len(versions) == 1
    data = versions[0]
    length, value_length, value_type = struct.unpack_from("<HHH", data)
    assert length == len(data) and value_type == 0 and value_length == 52
    key = "VS_VERSION_INFO\0".encode("utf-16-le")
    assert data[6:6 + len(key)] == key
    value_start = (6 + len(key) + 3) & ~3
    fixed = struct.unpack_from("<13I", data, value_start)
    assert fixed[0] == 0xFEEF04BD
    expected = tuple(map(int, __version__.split("."))) + (0,)
    assert (fixed[2] >> 16, fixed[2] & 0xFFFF, fixed[3] >> 16, fixed[3] & 0xFFFF) == expected
    assert (fixed[4] >> 16, fixed[4] & 0xFFFF, fixed[5] >> 16, fixed[5] & 0xFFFF) == expected


def test_launcher_manifest_uses_the_current_user_permissions(executable):
    manifests = [data for key, data in executable.resources().items() if key[0] == 24]
    assert len(manifests) == 1
    manifest = ET.fromstring(manifests[0])
    level = manifest.find(".//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel")
    assert level is not None and level.attrib == {"level": "asInvoker", "uiAccess": "false"}
    identity = manifest.find("{urn:schemas-microsoft-com:asm.v1}assemblyIdentity")
    assert identity is not None and identity.get("version") == f"{__version__}.0"


@pytest.fixture(scope="module")
def native_helpers(tmp_path_factory):
    """Compile the production C helpers for the host and call them directly.

    Win32 process/window APIs are outside this host harness. Explicit surrogate
    pairs below also exercise two-unit UTF-16 path content on a UCS-4 host.
    """
    if os.name != "posix":
        pytest.skip("The host C harness uses a POSIX shared library; PE checks run on every OS.")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.skip("A host C compiler is needed for the production C helper checks.")
    directory = tmp_path_factory.mktemp("native-launcher-helpers")
    source = directory / "helpers.c"
    output = directory / "helpers.so"
    source.write_text(r'''
#include "launcher_args.h"

int quote_arguments(wchar_t *output, size_t capacity,
                    const wchar_t **values, size_t count)
{
    StudioBuffer buffer = {output, capacity, 0};
    size_t index;
    if (capacity != 0) {
        output[0] = L'\0';
    }
    for (index = 0; index < count; ++index) {
        if (!studio_append_argument(&buffer, values[index])) {
            return 0;
        }
    }
    return 1;
}

int project_root(wchar_t *module, size_t length)
{
    return studio_project_root(module, length);
}

int join_path(wchar_t *output, size_t capacity,
              const wchar_t *root, const wchar_t *relative)
{
    StudioBuffer buffer = {output, capacity, 0};
    return studio_join_path(&buffer, root, relative);
}

int select_content_root(wchar_t *output, size_t capacity,
                        const wchar_t *root, int app_directory_present)
{
    StudioBuffer buffer = {output, capacity, 0};
    return studio_select_content_root(&buffer, root, app_directory_present);
}
''', encoding="utf-8")
    completed = subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2", "-fPIC", "-shared",
         "-I", str(ROOT / "launcher" / "native"), str(source), "-o", str(output)],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    library = ctypes.CDLL(str(output))
    pointer = ctypes.POINTER(ctypes.c_wchar)
    library.quote_arguments.argtypes = [pointer, ctypes.c_size_t, ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_size_t]
    library.quote_arguments.restype = ctypes.c_int
    library.project_root.argtypes = [pointer, ctypes.c_size_t]
    library.project_root.restype = ctypes.c_int
    library.join_path.argtypes = [pointer, ctypes.c_size_t, ctypes.c_wchar_p, ctypes.c_wchar_p]
    library.join_path.restype = ctypes.c_int
    library.select_content_root.argtypes = [pointer, ctypes.c_size_t, ctypes.c_wchar_p, ctypes.c_int]
    library.select_content_root.restype = ctypes.c_int
    return library


# Expected strings follow the Windows CRT contract, including the doubled
# trailing separators required before the closing quote. Specification:
# https://learn.microsoft.com/cpp/c-language/parsing-c-command-line-arguments
QUOTE_CASES = [
    ("", '""'),
    ("abc", '"abc"'),
    (r"C:\Studio folder\main.py", '"C:\\Studio folder\\main.py"'),
    ("中文 (test) & %PATH% $name `tick`; x", '"中文 (test) & %PATH% $name `tick`; x"'),
    ("tab\tvalue", '"tab\tvalue"'),
    ('a"b', '"a\\"b"'),
    ('a\\"b', '"a' + "\\" * 3 + '"b"'),
    ("C:\\", '"C:\\\\"'),
    ("\\\\server\\share\\", '"\\\\server\\share\\\\"'),
    ("\ud83d\ude80 folder\\", '"\ud83d\ude80 folder\\\\"'),
    ("a" + "\\" * 1000 + '"', '"a' + "\\" * 2001 + '""'),
]


def guarded_buffer(capacity: int):
    buffer = (ctypes.c_wchar * (capacity + 2))(*("\u25a0" * (capacity + 2)))
    pointer = ctypes.cast(ctypes.byref(buffer, ctypes.sizeof(ctypes.c_wchar)), ctypes.POINTER(ctypes.c_wchar))
    return buffer, pointer


@pytest.mark.parametrize("value,expected", QUOTE_CASES)
def test_native_argument_quoting_preserves_windows_path_content(native_helpers, value, expected):
    capacity = len(expected) + 1
    buffer, pointer = guarded_buffer(capacity)
    values = (ctypes.c_wchar_p * 1)(value)
    assert native_helpers.quote_arguments(pointer, capacity, values, 1) == 1
    assert "".join(buffer[1:-1]) == expected + "\0"
    assert buffer[0] == buffer[-1] == "\u25a0"


@pytest.mark.parametrize("value,expected", QUOTE_CASES)
def test_native_argument_overflow_fails_and_does_not_overwrite_memory(native_helpers, value, expected):
    values = (ctypes.c_wchar_p * 1)(value)
    for capacity in {0, 1, len(expected)}:
        buffer, pointer = guarded_buffer(capacity)
        assert native_helpers.quote_arguments(pointer, capacity, values, 1) == 0
        assert buffer[0] == buffer[-1] == "\u25a0"
        if capacity:
            assert "\0" in buffer[1:-1]


def test_native_argument_builder_separates_the_powershell_script_and_its_root(native_helpers):
    values = [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File",
        r"C:\Users\测试 & Co\HoYo Project Studio\launcher\startup.ps1",
        "-ProjectRoot", r"C:\Users\测试 & Co\HoYo Project Studio",
    ]
    expected = " ".join(f'"{value}"' for value in values)
    buffer, pointer = guarded_buffer(len(expected) + 1)
    arguments = (ctypes.c_wchar_p * len(values))(*values)
    assert native_helpers.quote_arguments(pointer, len(expected) + 1, arguments, len(values)) == 1
    assert "".join(buffer[1:-1]) == expected + "\0"
    assert buffer[0] == buffer[-1] == "\u25a0"


@pytest.mark.parametrize("path,expected", [
    (r"C:\Users\测试 & Co\HoYo Project Studio\HoYo Project Studio.exe", r"C:\Users\测试 & Co\HoYo Project Studio"),
    (r"C:\HoYo Project Studio.exe", "C:\\"),
    (r"\\?\C:\HoYo Project Studio.exe", "\\\\?\\C:\\"),
    (r"\\server\share\HoYo Project Studio.exe", r"\\server\share"),
    (r"\\?\UNC\server\share\Studio 文件\HoYo Project Studio.exe", r"\\?\UNC\server\share\Studio 文件"),
    ("C:/HoYo Project Studio.exe", "C:/"),
])
def test_native_project_root_preserves_absolute_drive_and_network_paths(native_helpers, path, expected):
    buffer = ctypes.create_unicode_buffer(path)
    assert native_helpers.project_root(buffer, len(path)) == 1
    assert buffer.value == expected


@pytest.mark.parametrize("path", ["", "HoYo Project Studio.exe", r"C:HoYo Project Studio.exe", "/HoYo Project Studio.exe"])
def test_native_project_root_rejects_missing_absolute_parent(native_helpers, path):
    buffer = ctypes.create_unicode_buffer(path)
    assert native_helpers.project_root(buffer, len(path)) == 0
    assert buffer.value == path


@pytest.mark.parametrize("root,relative,expected", [
    (r"C:\Studio 文件", r"launcher\startup.ps1", r"C:\Studio 文件\launcher\startup.ps1"),
    ("C:\\", r"launcher\startup.ps1", r"C:\launcher\startup.ps1"),
    ("\\\\?\\C:\\", r"launcher\startup.ps1", r"\\?\C:\launcher\startup.ps1"),
    (r"\\server\share", "main.py", r"\\server\share\main.py"),
])
def test_native_path_join_accepts_exact_capacity_and_rejects_truncation(native_helpers, root, relative, expected):
    capacity = len(expected) + 1
    buffer, pointer = guarded_buffer(capacity)
    assert native_helpers.join_path(pointer, capacity, root, relative) == 1
    assert "".join(buffer[1:-1]) == expected + "\0"
    assert buffer[0] == buffer[-1] == "\u25a0"
    for capacity in {0, 1, len(expected)}:
        buffer, pointer = guarded_buffer(capacity)
        assert native_helpers.join_path(pointer, capacity, root, relative) == 0
        assert buffer[0] == buffer[-1] == "\u25a0"
        if capacity:
            assert "\0" in buffer[1:-1]


@pytest.mark.parametrize("root,compact", [
    (r"C:\Users\测试 & Co\HoYo Project Studio", r"C:\Users\测试 & Co\HoYo Project Studio\app"),
    ("C:\\", r"C:\app"),
    ("\\\\?\\C:\\", r"\\?\C:\app"),
    (r"\\server\share\Studio 文件", r"\\server\share\Studio 文件\app"),
    (r"\\?\UNC\server\share\Studio 文件", r"\\?\UNC\server\share\Studio 文件\app"),
    ("C:/Studio 文件/", "C:/Studio 文件/app"),
    ("C:\\Studio \ud83d\ude80", "C:\\Studio \ud83d\ude80\\app"),
])
@pytest.mark.parametrize("app_directory_present", [False, True])
def test_native_content_selection_uses_one_complete_support_root(
    native_helpers, root, compact, app_directory_present,
):
    expected = compact if app_directory_present else root
    capacity = len(expected) + 1
    buffer, pointer = guarded_buffer(capacity)
    assert native_helpers.select_content_root(pointer, capacity, root, app_directory_present) == 1
    assert "".join(buffer[1:-1]) == expected + "\0"
    assert buffer[0] == buffer[-1] == "\u25a0"
    for capacity in {0, 1, len(expected)}:
        buffer, pointer = guarded_buffer(capacity)
        assert native_helpers.select_content_root(pointer, capacity, root, app_directory_present) == 0
        assert buffer[0] == buffer[-1] == "\u25a0"
        if capacity:
            assert "\0" in buffer[1:-1]


@pytest.mark.parametrize("app_directory_present", [False, True])
def test_native_content_selection_rejects_an_empty_root(native_helpers, app_directory_present):
    buffer, pointer = guarded_buffer(100)
    assert native_helpers.select_content_root(pointer, 100, "", app_directory_present) == 0
    assert buffer[1] == "\0"
    assert buffer[0] == buffer[-1] == "\u25a0"
