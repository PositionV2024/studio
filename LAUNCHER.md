# HoYo Project Studio launcher

## Recommended first run

1. Double-click `Setup HoYo Project Studio.cmd`.
2. Wait for the dependencies to install.
3. Allow the setup to create the desktop shortcut.
4. Launch the application from the new **HoYo Project Studio** desktop shortcut.

## Files

| File | Purpose |
|---|---|
| `Setup HoYo Project Studio.cmd` | Creates `.venv`, installs dependencies, and creates a shortcut |
| `Launch HoYo Project Studio.vbs` | Launches the application without a console window |
| `Launch HoYo Project Studio.cmd` | Launches the application and automatically starts setup when required |
| `Create Desktop Shortcut.cmd` | Recreates the desktop shortcut |
| `Build Windows EXE.cmd` | Builds a Windows executable with PyInstaller |

## Build output

`Build Windows EXE.cmd` creates:

```text
dist\HoYo Project Studio\HoYo Project Studio.exe
```

Keep every file inside that generated folder together. Qt WebEngine will not work if only the `.exe` is copied elsewhere.

## Python requirement for managed projects

The standalone editor can open without a Python console, but Streamlit projects still require a Python interpreter. Project Studio checks, in order:

1. The project's configured `python_executable`.
2. The project's `.venv` or `venv`.
3. The Windows `py` or `python` command.

Using a separate `.venv` inside each managed project remains the recommended setup.
