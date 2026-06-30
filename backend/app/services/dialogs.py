"""Native file-dialog bridge — windowed (pywebview) mode only (P4).

CONFIRM-FIRST (CLAUDE.md §6) result — pywebview **6.2.1**, verified against the installed package:
``Window.create_file_dialog(dialog_type, directory='', allow_multiple=False, save_filename='',
file_types=())`` returns a **tuple of selected path strings**, or ``None`` if the user cancels. Dialog
types are ``webview.FileDialog.OPEN`` / ``.FOLDER`` / ``.SAVE`` (the module-level ``OPEN_DIALOG`` /
``FOLDER_DIALOG`` / ``SAVE_DIALOG`` constants are DEPRECATED in 6.x). ``file_types`` entries are
``'Description (*.ext;*.ext)'``. This module is the ONLY place that shape is encoded.

The dialog runs against the live pywebview ``Window`` the launcher registers
(``services.cases.register_native_window``). It is invoked from the uvicorn request thread; pywebview
marshals GUI calls onto its own GUI thread internally. In the dev/browser flow (no window registered)
there is no native dialog — the frontend falls back to an HTML ``<input type=file>`` upload + a path
field, which is the always-available path. TODO: confirm the cross-thread invocation on each packaged
platform during P7 (it is not exercisable in headless CI); the browser fallback is unaffected.
"""

from __future__ import annotations

# Allowed picker kinds -> (dialog_type selector, extra kwargs). The dialog_type is resolved lazily
# (importing webview) so this module imports cleanly with no GUI backend present.
DIALOG_KINDS = ("casefile", "casedb", "folder")


def _file_dialog_args(kind: str) -> tuple[int, dict]:
    import webview

    if kind == "folder":
        return webview.FileDialog.FOLDER, {}
    if kind == "casefile":
        return webview.FileDialog.OPEN, {"file_types": ("Case file (*.casefile)", "All files (*.*)")}
    if kind == "casedb":
        return webview.FileDialog.OPEN, {"file_types": ("Case database (*.db)", "All files (*.*)")}
    raise ValueError(f"unknown dialog kind {kind!r} (expected one of {DIALOG_KINDS})")


def pick_path(window, kind: str) -> list[str]:
    """Open the native dialog on ``window`` and return the selected path(s) as a list (empty on
    cancel). ``window`` is any object exposing ``create_file_dialog`` (a real pywebview Window in the
    app; a fake in tests) so the kind->args mapping is verifiable without a GUI."""
    dialog_type, kwargs = _file_dialog_args(kind)
    result = window.create_file_dialog(dialog_type, **kwargs)
    return list(result) if result else []
