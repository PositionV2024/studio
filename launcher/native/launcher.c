/* HoYo Project Studio's prebuilt Windows entry point.
 *
 * The native launcher has no Python, .NET, MinGW runtime, Git, or GitHub CLI
 * dependency. It only hands off to the bundled PowerShell setup window. That
 * window checks installed Python, opens Studio after core dependencies are
 * ready, and lets Studio finish optional Preview setup in the background.
 *
 * All paths come from Win32 APIs, never PATH or caller-supplied arguments.
 * Only kernel32.dll and user32.dll are imported. The explicit entry point and
 * bounded buffers avoid adding a C runtime dependency to the download.
 */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0A00
#include <windows.h>
#include "launcher_args.h"

#define STUDIO_PATH_CAPACITY 32768u
/* CreateProcessW counts the terminating NUL in its 32,767-unit limit. */
#define STUDIO_COMMAND_CAPACITY 32767u

static const wchar_t *const required_files[] = {
    L"launcher\\startup.ps1",
    L"launcher\\startup.py",
    L"launcher\\python_discovery.ps1",
    L"launcher\\launcher.pyw",
    L"studio\\application.py",
    L"main.py",
    L"requirements.txt"
};

static int is_file(const wchar_t *path)
{
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES &&
           (attributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

static void show_error(const wchar_t *message, DWORD error_code)
{
    /* An error must still be visible if allocation failed. */
    wchar_t *formatted = NULL;
    wchar_t *details = NULL;
    if (error_code != ERROR_SUCCESS) {
        FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER |
                           FORMAT_MESSAGE_FROM_SYSTEM |
                           FORMAT_MESSAGE_IGNORE_INSERTS,
                       NULL, error_code, 0, (wchar_t *)&details, 0, NULL);
    }
    if (details != NULL) {
        formatted = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY,
                              STUDIO_PATH_CAPACITY * sizeof(wchar_t));
        if (formatted != NULL) {
            StudioBuffer buffer = {formatted, STUDIO_PATH_CAPACITY, 0};
            if (!studio_append(&buffer, message) ||
                !studio_append(&buffer, L"\n\nWindows reported: ") ||
                !studio_append(&buffer, details)) {
                HeapFree(GetProcessHeap(), 0, formatted);
                formatted = NULL;
            }
        }
    }
    MessageBoxW(NULL, formatted != NULL ? formatted : message,
                L"HoYo Project Studio", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
    if (formatted != NULL) {
        HeapFree(GetProcessHeap(), 0, formatted);
    }
    if (details != NULL) {
        LocalFree(details);
    }
}

static int launch_studio(void)
{
    HANDLE heap = GetProcessHeap();
    /* Allocate all large strings on the heap, avoiding a CRT stack probe. */
    wchar_t *storage = HeapAlloc(heap, HEAP_ZERO_MEMORY,
                                 5 * STUDIO_PATH_CAPACITY * sizeof(wchar_t));
    wchar_t *exe_root;
    wchar_t *root;
    wchar_t *script;
    wchar_t *powershell;
    wchar_t *command;
    DWORD module_length;
    DWORD app_attributes;
    DWORD app_error;
    UINT system_length;
    size_t index;
    int result = 1;
    int app_directory_present = 0;
    STARTUPINFOW startup = {0};
    PROCESS_INFORMATION process = {0};
    StudioBuffer path;
    StudioBuffer args;
    StudioBuffer content_root;

    if (storage == NULL) {
        show_error(L"Windows could not allocate memory to open Studio. "
                   L"Close another application and try again.", ERROR_SUCCESS);
        return 1;
    }
    exe_root = storage;
    root = storage + STUDIO_PATH_CAPACITY;
    script = storage + 2 * STUDIO_PATH_CAPACITY;
    powershell = storage + 3 * STUDIO_PATH_CAPACITY;
    command = storage + 4 * STUDIO_PATH_CAPACITY;
    path.text = script;
    path.capacity = STUDIO_PATH_CAPACITY;
    path.length = 0;
    args.text = command;
    args.capacity = STUDIO_COMMAND_CAPACITY;
    args.length = 0;
    content_root.text = root;
    content_root.capacity = STUDIO_PATH_CAPACITY;
    content_root.length = 0;

    module_length = GetModuleFileNameW(NULL, exe_root, STUDIO_PATH_CAPACITY);
    if (module_length == 0 || module_length >= STUDIO_PATH_CAPACITY ||
        !studio_project_root(exe_root, module_length)) {
        show_error(L"Studio could not locate its application folder. "
                   L"Extract the complete ZIP into a folder and open "
                   L"HoYo Project Studio.exe from that folder.", GetLastError());
        goto cleanup;
    }

    /* Select exactly one support root before checking or executing any files.
     * An incomplete app directory must not launch a stale flat installation
     * left beside it by an older update. Only an absent app permits flat mode. */
    if (!studio_join_path(&path, exe_root, L"app")) {
        goto command_too_long;
    }
    app_attributes = GetFileAttributesW(script);
    app_error = app_attributes == INVALID_FILE_ATTRIBUTES ? GetLastError() : ERROR_SUCCESS;
    if (app_attributes != INVALID_FILE_ATTRIBUTES) {
        if ((app_attributes & FILE_ATTRIBUTE_DIRECTORY) == 0) {
            show_error(L"Studio needs the app folder beside HoYo Project Studio.exe. "
                       L"An item named app is present but is not a folder. "
                       L"Extract the complete application ZIP into a new folder "
                       L"and try again.", ERROR_DIRECTORY);
            goto cleanup;
        }
        app_directory_present = 1;
    } else if (app_error != ERROR_FILE_NOT_FOUND && app_error != ERROR_PATH_NOT_FOUND) {
        show_error(L"Studio could not access the app folder beside "
                   L"HoYo Project Studio.exe. Check that the complete application "
                   L"folder is accessible, then try again.", app_error);
        goto cleanup;
    }
    if (!studio_select_content_root(&content_root, exe_root, app_directory_present)) {
        goto command_too_long;
    }

    for (index = 0; index < sizeof(required_files) / sizeof(required_files[0]);
         ++index) {
        if (!studio_join_path(&path, root, required_files[index])) {
            show_error(L"The application folder path is too long to open Studio. "
                       L"Extract the ZIP into a folder with a shorter path.",
                       ERROR_SUCCESS);
            goto cleanup;
        }
        if (!is_file(script)) {
            DWORD file_error = GetLastError();
            /* The missing relative name is useful after an incomplete extract. */
            if (studio_append(&args, L"Studio's application files are incomplete. "
                              L"Extract the complete application ZIP into a new "
                              L"folder. Keep HoYo Project Studio.exe beside the "
                              L"complete app folder.\n\n"
                              L"Missing or inaccessible file: ") &&
                studio_append(&args, app_directory_present ? L"app\\" : L"") &&
                studio_append(&args, required_files[index])) {
                show_error(command, file_error);
            } else {
                show_error(L"Studio's application files are incomplete. "
                           L"Extract the complete application ZIP and try again.",
                           ERROR_SUCCESS);
            }
            goto cleanup;
        }
    }
    /* Restore the script path after using the same buffer for required files. */
    if (!studio_join_path(&path, root, required_files[0])) {
        goto command_too_long;
    }

    /* x64 binary -> native System32. Do not trust %SystemRoot%, PATH, or a
     * powershell.exe in the application directory. Windows 10/11 include this. */
    system_length = GetSystemDirectoryW(powershell, STUDIO_PATH_CAPACITY);
    if (system_length == 0 || system_length >= STUDIO_PATH_CAPACITY) {
        show_error(L"Studio could not locate Windows PowerShell.", GetLastError());
        goto cleanup;
    }
    {
        StudioBuffer system_path = {powershell, STUDIO_PATH_CAPACITY, system_length};
        if (!studio_append(&system_path,
                           L"\\WindowsPowerShell\\v1.0\\powershell.exe")) {
            goto command_too_long;
        }
    }
    if (!is_file(powershell)) {
        show_error(L"Studio could not find Windows PowerShell in the Windows "
                   L"system folder. It is a built-in Windows component. "
                   L"Restore or enable Windows PowerShell, then try again.",
                   GetLastError());
        goto cleanup;
    }

    if (!studio_append_argument(&args, powershell) ||
        !studio_append_argument(&args, L"-NoProfile") ||
        !studio_append_argument(&args, L"-STA") ||
        !studio_append_argument(&args, L"-ExecutionPolicy") ||
        !studio_append_argument(&args, L"Bypass") ||
        !studio_append_argument(&args, L"-File") ||
        !studio_append_argument(&args, script) ||
        !studio_append_argument(&args, L"-ProjectRoot") ||
        !studio_append_argument(&args, root)) {
        goto command_too_long;
    }

    startup.cb = sizeof(startup);
    /* CREATE_NO_WINDOW suppresses the PowerShell console. Do not also pass
     * STARTF_USESHOWWINDOW/SW_HIDE: Windows may apply that override to the
     * setup form's first ShowWindow call, hiding the very window we need. */
    if (!CreateProcessW(powershell, command, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                        NULL, root, &startup, &process)) {
        show_error(L"Studio could not open its setup window. Check that the "
                   L"application folder is accessible and Windows PowerShell "
                   L"is allowed to run, then try again.", GetLastError());
        goto cleanup;
    }
    /* The setup window owns the new process and the eventual Studio handoff.
     * The launcher exits immediately; there is no console tab to leave open. */
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    result = 0;
    goto cleanup;

command_too_long:
    show_error(L"The application folder path is too long to open Studio. "
               L"Extract the ZIP into a folder with a shorter path.", ERROR_SUCCESS);
cleanup:
    HeapFree(heap, 0, storage);
    return result;
}

void WINAPI StudioEntryPoint(void)
{
    /* No DLLs are loaded from the application folder, working directory, or
     * PATH by this launcher. This does not change a system-wide setting. */
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) {
        show_error(L"Studio could not configure its Windows launcher. "
                   L"This launcher requires 64-bit Windows 10 or Windows 11.",
                   GetLastError());
        ExitProcess(1);
    }
    ExitProcess((UINT)launch_studio());
}
