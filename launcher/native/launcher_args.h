#ifndef HOYO_LAUNCHER_ARGS_H
#define HOYO_LAUNCHER_ARGS_H

/* These helpers use no C runtime. Capacities count wchar_t units, including NUL.
 * Build-time host tests exercise the same helpers used by the Windows binary. */
#include <stddef.h>
#include <wchar.h>

typedef struct StudioBuffer {
    wchar_t *text;
    size_t capacity;
    size_t length;
} StudioBuffer;

static int studio_append_char(StudioBuffer *buffer, wchar_t value)
{
    if (buffer->length + 1 >= buffer->capacity) {
        return 0;
    }
    buffer->text[buffer->length++] = value;
    buffer->text[buffer->length] = L'\0';
    return 1;
}

static int studio_append(StudioBuffer *buffer, const wchar_t *text)
{
    while (*text != L'\0') {
        if (!studio_append_char(buffer, *text++)) {
            return 0;
        }
    }
    return 1;
}

/* Quote one argument using the Windows CRT rules. A run of backslashes doubles
 * before a closing quote, and doubles plus one before a literal quote.
 * No cmd.exe, PowerShell expression, or environment expansion is involved. */
static int studio_append_argument(StudioBuffer *buffer, const wchar_t *value)
{
    if (buffer->length != 0 && !studio_append_char(buffer, L' ')) {
        return 0;
    }
    if (!studio_append_char(buffer, L'"')) {
        return 0;
    }
    while (1) {
        size_t slashes = 0;
        size_t copies;
        while (*value == L'\\') {
            ++slashes;
            ++value;
        }
        copies = slashes;
        if (*value == L'"' || *value == L'\0') {
            copies *= 2;
        }
        while (copies-- != 0) {
            if (!studio_append_char(buffer, L'\\')) {
                return 0;
            }
        }
        if (*value == L'\0') {
            break;
        }
        if (*value == L'"' && !studio_append_char(buffer, L'\\')) {
            return 0;
        }
        if (!studio_append_char(buffer, *value++)) {
            return 0;
        }
    }
    return studio_append_char(buffer, L'"');
}

/* GetModuleFileNameW supplies an absolute path. Keep the separator at a drive
 * root (C:\) so the result cannot become the drive-relative path C:. */
static int studio_project_root(wchar_t *module, size_t length)
{
    size_t separator = length;
    while (separator != 0) {
        --separator;
        if (module[separator] == L'\\' || module[separator] == L'/') {
            if (separator == 2 && module[1] == L':') {
                module[3] = L'\0';
            } else if (separator == 6 && module[0] == L'\\' &&
                       module[1] == L'\\' && module[2] == L'?' &&
                       module[3] == L'\\' && module[5] == L':') {
                /* The extended form \\?\C:\ also retains its drive separator. */
                module[7] = L'\0';
            } else if (separator != 0) {
                module[separator] = L'\0';
            } else {
                return 0;
            }
            return 1;
        }
    }
    return 0;
}

static int studio_join_path(StudioBuffer *buffer, const wchar_t *root,
                            const wchar_t *relative)
{
    buffer->length = 0;
    if (buffer->capacity == 0) {
        return 0;
    }
    buffer->text[0] = L'\0';
    if (!studio_append(buffer, root) || buffer->length == 0) {
        return 0;
    }
    if (buffer->text[buffer->length - 1] != L'\\' &&
        buffer->text[buffer->length - 1] != L'/' &&
        !studio_append_char(buffer, L'\\')) {
        return 0;
    }
    return studio_append(buffer, relative);
}

/* The release keeps all support files in app beside the EXE. Flat source
 * checkouts remain launchable only when that sibling directory is absent.
 * Win32 probes the directory before calling this helper; required-file checks
 * then apply to this one selected root, with no fallback after a missing file.
 * The input root and output storage must not alias. */
static inline int studio_select_content_root(StudioBuffer *buffer,
                                              const wchar_t *exe_root,
                                              int app_directory_present)
{
    if (app_directory_present) {
        return studio_join_path(buffer, exe_root, L"app");
    }
    buffer->length = 0;
    if (buffer->capacity == 0) {
        return 0;
    }
    buffer->text[0] = L'\0';
    return studio_append(buffer, exe_root) && buffer->length != 0;
}

#endif
