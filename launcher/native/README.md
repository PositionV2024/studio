# Native Windows launcher

The application ZIP has two items in its top-level Studio folder:
**HoYo Project Studio.exe** and **app**. Users double-click that EXE after
extracting the complete application. All support files, including this readable
launcher source and optional maintainer rebuild tool, are inside `app`.

- `launcher.c`: Win32 startup, required-file checks, error dialogs and handoff.
- `launcher_args.h`: bounded Unicode path and argument helpers shared with tests.
- `launcher.rc` and `launcher.manifest`: embedded icon, version and Windows settings.
- `build_launcher.py`: compiler orchestration and verification of the generated PE.

The launcher is a small, unsigned, native Windows x64 GUI executable. It imports
only the Windows `KERNEL32.dll` and `USER32.dll` APIs and does not need Python,
.NET, a Visual C++ runtime installer, or a compiler to display launch errors or
start the bundled setup window. Windows PowerShell is used from its absolute
Windows system directory.

The existing `launcher/startup.ps1` owns setup: it shows a progress window, reuses
an installed compatible Python, prepares editor essentials, opens Studio, and
lets optional Preview dependencies finish in the background. This EXE does not
download a Python interpreter. The `app` folder must stay beside it. The separate **Build Windows EXE** utility
inside `app` still creates the fully bundled application.

## Rebuild for maintainers

Prerequisites: Python 3.11 or later and an existing MinGW-w64 x64 GCC/binutils
toolchain with `gcc` and `windres`. The rebuild tool installs and downloads
nothing. It can run on a Linux cross-build host or a Windows MinGW environment.

From the extracted Studio folder:

```sh
python app/launcher/native/build_launcher.py
```

For an extracted cross-toolchain, supply its command prefix:

```sh
python app/launcher/native/build_launcher.py --toolchain-prefix /path/to/toolchain/usr/bin/x86_64-w64-mingw32-
```

The default output replaces the outer `HoYo Project Studio.exe`; rebuilding from
`app/launcher/native` does not create another launcher inside `app`. Flat source
checkouts use `launcher/native/build_launcher.py` and keep the EXE at the source
root. `--output` and `--report` can select alternate paths. The JSON report records
the exact build commands, compiler versions, input hashes, binary hash, PE
structure and imported APIs.
It does not claim that a build performed on Linux has been run on Windows.

Version 0.4.12 was compiled with the official Ubuntu 24.04 Noble archive's
MinGW-w64 GCC package `13.2.0-6ubuntu1+26.1`, binutils package
`2.41.90.20240122-1ubuntu1+11.4`, and MinGW-w64 headers `11.0.1-3build1`.
The Ubuntu archive signature and each package's SHA-256 were checked before
extracting this maintainer toolchain. The compiler and its packages are not
included in the application download.

## Launch details

The launcher derives its own folder from `GetModuleFileNameW` and selects the
sibling `app` folder as its content root. When `app` is absent, it accepts the
flat source layout used by maintainers and older releases. An inaccessible,
non-directory, or incomplete `app` produces an error; it never silently falls
back to older files beside that folder.

It checks all required support files under the selected content root and calls
`CreateProcessW` with an explicit system PowerShell executable path, a mutable
Unicode command line, the application
content root as both `-ProjectRoot` and the working directory, and handle
inheritance disabled. Setup consequently keeps its virtual environment and logs
inside `app` in the simplified release. The launcher ignores arguments passed to
the EXE. Folder names containing spaces, Unicode, shell
punctuation, and trailing drive-root separators are quoted as literal arguments.
The command is never evaluated through `cmd.exe` or a PowerShell expression.

`CREATE_NO_WINDOW` suppresses the PowerShell console while allowing its setup
form to appear. The launcher does not pass a hidden-window startup override,
which could hide the first WinForms window. It exits after a successful process
handoff; the setup window and Studio own their respective startup and shutdown.
Missing support files and process-creation failures produce a native error
dialog. Launching requires no administrator elevation.

The PE includes the application's original icon, version information and an
`asInvoker` manifest. ASLR, high-entropy ASLR and NX are enabled. Build timestamps
are zero, and rebuilding the same inputs with the same toolchain produces the
same executable bytes. Tests inspect the actual distributed PE and exercise
the production path/argument helpers. A Windows desktop launch remains a
separate platform validation step.

Microsoft API references:

- [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)
- [Process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)
- [ShowWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow)
- [SetDefaultDllDirectories](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-setdefaultdlldirectories)
