"""P9 packaging/installer/signing guards — fast, build-free, machine-independent.

These pin the load-bearing P9 promises so a future edit can't silently break them:
  * the installer's data-dir name equals the app's real %APPDATA% folder (so uninstall preserves cases);
  * the .iss never deletes user data and wires shortcuts to 8.ico + per-user/Program-Files choice;
  * the exe's version-info resource is NON-BLANK (the metadata feeds a real Win32 resource);
  * signing is genuinely OPTIONAL — it skips cleanly with no cert and never raises for lack of one.
The actual embed-into-the-exe + end-to-end install are verified by the build (`make installer`) and the
frozen smoke, not here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
ISS = ROOT / "installer" / "bih.iss"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_p9_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


meta = _load("app_metadata")
sign = _load("sign")


# --------------------------------------------------------------------------- identity / version

def test_app_id_equals_real_appdata_folder_name():
    """The installer's data-dir name MUST equal backend app_paths.APP_NAME — the uninstaller leaves that
    %APPDATA% folder alone, so if these drift, uninstall could orphan (or fail to find) the user's cases."""
    from backend.app.app_paths import APP_NAME

    assert meta.APP_ID == APP_NAME


def test_version_tuple_is_4_part_and_matches_version_string():
    assert len(meta.VERSION_TUPLE) == 4
    parts = [int(x) for x in meta.VERSION.split(".")]
    assert list(meta.VERSION_TUPLE[: len(parts)]) == parts


def test_version_info_resource_is_not_metadata_blank():
    """Build the same VSVersionInfo bih.spec embeds and assert the identity strings are present — a blank
    resource is a major SmartScreen/AV red flag, so the metadata must actually populate it."""
    vi = importlib.util.find_spec("PyInstaller.utils.win32.versioninfo")
    if vi is None:  # PyInstaller not installed in this env
        pytest.skip("PyInstaller not available")
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=meta.VERSION_TUPLE, prodvers=meta.VERSION_TUPLE, mask=0x3F,
                          flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", meta.COMPANY_NAME),
            StringStruct("FileDescription", meta.FILE_DESCRIPTION),
            StringStruct("FileVersion", meta.VERSION),
            StringStruct("LegalCopyright", meta.COPYRIGHT),
            StringStruct("ProductName", meta.APP_DISPLAY_NAME),
            StringStruct("ProductVersion", meta.VERSION),
        ])]), VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])])],
    )
    rendered = str(info)
    for token in (meta.COMPANY_NAME, meta.APP_DISPLAY_NAME, meta.VERSION, meta.COPYRIGHT):
        assert token in rendered


# --------------------------------------------------------------------------- the Inno Setup script

def test_iss_preserves_user_data_and_wires_icon_and_install_scope():
    text = ISS.read_text(encoding="utf-8")
    # uninstall must NOT delete user data — there is deliberately no [UninstallDelete] *section* (a
    # comment may mention the token, so check real section headers, not raw substring).
    headers = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith("[") and ln.strip().endswith("]") and not ln.strip().startswith(";")]
    assert "[UninstallDelete]" not in headers
    # …and the data-dir name is documented in the script so the intent is explicit + greppable.
    assert meta.APP_ID in text
    # shortcuts reference 8.ico, for Start-Menu AND Desktop.
    assert "8.ico" in text
    assert "{autoprograms}" in text and "{autodesktop}" in text
    # per-user OR Program Files (the wizard asks).
    assert "PrivilegesRequired=lowest" in text
    assert "PrivilegesRequiredOverridesAllowed=dialog" in text
    # the STABLE installer identity is wired (keyed for upgrade/uninstall).
    assert meta.INSTALLER_APP_GUID in text


# --------------------------------------------------------------------------- optional signing

def test_signing_is_optional_and_skips_cleanly_without_a_cert(monkeypatch):
    for var in ("BIH_SIGN_PFX", "BIH_SIGN_PASSWORD", "BIH_SIGN_THUMBPRINT"):
        monkeypatch.delenv(var, raising=False)
    assert sign.sign_config() is None
    result = sign.maybe_sign([ROOT / "does-not-exist.exe"])  # must NOT raise for lack of a cert
    assert result["skipped"] is True and result["signed"] == []


# --------------------------------------------------------------------------- bundled sample (P39)

def test_bih_spec_bundles_the_first_run_sample_case():
    """P39: the first-run sample .casefile must be declared in app_paths.BUNDLED_RESOURCES, present on
    disk, AND wired into bih.spec's datas — else the packaged app ships without it and 'Explore the
    sample case' 404s in the frozen build (which source-mode tests can't catch)."""
    from backend.app.app_paths import BUNDLED_RESOURCES

    rel = BUNDLED_RESOURCES.get("sample_casefile")
    assert rel and rel.endswith(".casefile"), "sample_casefile missing from BUNDLED_RESOURCES"
    assert (ROOT / rel).exists(), f"bundled sample casefile not found at {rel}"
    spec = (ROOT / "bih.spec").read_text(encoding="utf-8")
    assert rel in spec, "bih.spec datas do not bundle the sample .casefile"


def test_sign_config_reads_pfx_and_thumbprint(monkeypatch):
    monkeypatch.delenv("BIH_SIGN_THUMBPRINT", raising=False)
    monkeypatch.setenv("BIH_SIGN_PFX", r"C:\certs\bih.pfx")
    monkeypatch.setenv("BIH_SIGN_PASSWORD", "secret")
    cfg = sign.sign_config()
    assert cfg["method"] == "pfx" and cfg["pfx"].endswith("bih.pfx") and cfg["password"] == "secret"

    monkeypatch.delenv("BIH_SIGN_PFX", raising=False)
    monkeypatch.delenv("BIH_SIGN_PASSWORD", raising=False)
    monkeypatch.setenv("BIH_SIGN_THUMBPRINT", "ABCD1234")
    cfg = sign.sign_config()
    assert cfg["method"] == "thumbprint" and cfg["thumbprint"] == "ABCD1234"
