#!/usr/bin/env python3
"""Let pywebview's Windows backend import under modern .NET (Windows ARM64 build only).

pywebview 6.2.1 builds its folder-picker class by reflecting into .NET Framework
WinForms internals (FileDialogNative) while the module is imported. Modern .NET
WinForms has no such internals, so the lookup returns null and the whole Windows
backend fails to import before any window exists.

This moves that reflection to the moment a folder dialog is opened and uses the
standard FolderBrowserDialog when the internals are missing. Clip Farm Pilot never
opens a folder dialog; the change only has to keep the import from failing. The
patch refuses to run against any other pywebview version or source, so an upgrade
cannot be patched silently or incorrectly.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path


PYWEBVIEW_VERSION = "6.2.1"
PATCH_MARKER = "# Clip Farm Pilot: folder-dialog reflection deferred for modern .NET."

ORIGINAL = """class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    )
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    fosPickFoldersBitFlag = (
        windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
        .GetField('FOS_PICKFOLDERS')
        .GetValue(None)
    )

    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents'
    ).GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        openFileDialog = WinForms.OpenFileDialog()
"""

REPLACEMENT = f"""class OpenFolderDialog:
    {PATCH_MARKER}
    foldersFilter = 'Folders|\\n'
    _framework = None

    @classmethod
    def _load_framework_internals(cls):
        if cls._framework is not None:
            return cls._framework
        flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
        windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
        iFileDialogType = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialogNative+IFileDialog'
        )
        if iFileDialogType is None:
            cls._framework = False
            return cls._framework
        OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
        FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
        cls.createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
        cls.onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
        cls.getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
        cls.setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
        cls.fosPickFoldersBitFlag = (
            windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
            .GetField('FOS_PICKFOLDERS')
            .GetValue(None)
        )
        cls.vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialog+VistaDialogEvents'
        ).GetConstructor(flags, None, [FileDialogType], [])
        cls.adviseMethodInfo = iFileDialogType.GetMethod('Advise')
        cls.unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
        cls.showMethodInfo = iFileDialogType.GetMethod('Show')
        cls._framework = True
        return cls._framework

    @classmethod
    def show(cls, parent=None, initialDirectory=None, allow_multiple=False, title=None):
        if not cls._load_framework_internals():
            folderDialog = WinForms.FolderBrowserDialog()
            if initialDirectory:
                folderDialog.InitialDirectory = initialDirectory
            if title:
                folderDialog.Description = title
                folderDialog.UseDescriptionForTitle = True
            folderDialog.Multiselect = allow_multiple
            if folderDialog.ShowDialog(parent) != WinForms.DialogResult.OK:
                return None
            return tuple(folderDialog.SelectedPaths)
        openFileDialog = WinForms.OpenFileDialog()
"""


def winforms_module_path() -> Path:
    spec = importlib.util.find_spec("webview")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("pywebview is not installed in this environment.")
    return Path(list(spec.submodule_search_locations)[0]) / "platforms" / "winforms.py"


def main() -> None:
    installed = importlib.metadata.version("pywebview")
    if installed != PYWEBVIEW_VERSION:
        raise RuntimeError(
            f"This patch is written for pywebview {PYWEBVIEW_VERSION}, but {installed} is installed. "
            "Check whether the new version still needs it before updating the patch."
        )
    path = winforms_module_path()
    source = path.read_text(encoding="utf-8")
    if PATCH_MARKER in source:
        print(f"pywebview's Windows backend is already patched for modern .NET at {path}")
        return
    if source.count(ORIGINAL) != 1:
        raise RuntimeError(f"pywebview's folder-dialog code at {path} no longer matches the expected source.")
    path.write_text(source.replace(ORIGINAL, REPLACEMENT), encoding="utf-8")
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
    print(f"Patched pywebview's Windows backend for modern .NET at {path}")


if __name__ == "__main__":
    main()
