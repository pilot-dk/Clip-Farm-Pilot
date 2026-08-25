import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IOS_SOURCE = ROOT / "mobile-local" / "ios" / "ClipFarmPilotLocal"
ANDROID_SOURCE = ROOT / "mobile-local" / "android" / "app" / "src" / "main"


def _text_files(root: Path):
    for path in root.rglob("*"):
        if path.suffix.lower() in {".swift", ".java", ".xml", ".plist", ".xcprivacy"}:
            yield path


def test_mobile_apps_do_not_contain_cloud_runtime_endpoints():
    forbidden = (
        "onrender.com",
        "URLSession",
        "EXPO_PUBLIC_API_BASE_URL",
        "http://localhost",
        "https://localhost",
    )
    for path in [*_text_files(IOS_SOURCE), *_text_files(ANDROID_SOURCE)]:
        content = path.read_text(encoding="utf-8")
        for value in forbidden:
            assert value not in content, f"{value} must not appear in {path}"


def test_android_app_has_no_internet_permission():
    manifest = (ANDROID_SOURCE / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert '<uses-permission android:name="android.permission.INTERNET"' not in manifest
    assert 'android.permission.ACCESS_NETWORK_STATE" tools:node="remove"' in manifest
    assert 'android:usesCleartextTraffic="false"' in manifest


def test_ios_requires_on_device_speech_recognition():
    engine = (IOS_SOURCE / "LocalVideoEngine.swift").read_text(encoding="utf-8")
    assert "supportsOnDeviceRecognition" in engine
    assert "requiresOnDeviceRecognition = true" in engine


def test_privacy_manifest_declares_no_collection_or_tracking():
    privacy = (IOS_SOURCE / "PrivacyInfo.xcprivacy").read_text(encoding="utf-8")
    assert "NSPrivacyCollectedDataTypes" in privacy
    assert "NSPrivacyTracking" in privacy
    assert "<false/>" in privacy


def test_release_workflow_uses_native_local_builds():
    workflow = (ROOT / ".github" / "workflows" / "mobile-release.yml").read_text(encoding="utf-8")
    assert "build_mobile_local_android.sh" in workflow
    assert "build_mobile_local_ios.sh" in workflow
    assert "onrender.com" not in workflow
    assert "EXPO_PUBLIC_API_BASE_URL" not in workflow
    assert "update_sidestore_source.py" in workflow


def test_sidestore_source_matches_the_local_ios_app():
    source = json.loads((ROOT / "sidestore-source.json").read_text(encoding="utf-8"))
    assert source["identifier"] == "com.clipfarmpilot.source"
    assert source["sourceURL"].endswith("/main/sidestore-source.json")
    app = source["apps"][0]
    assert app["bundleIdentifier"] == "com.clipfarmpilot.local"
    assert app["permissions"] == [
        {
            "type": "photos",
            "usageDescription": "Choose livestream videos to edit entirely on this device.",
        },
        {
            "type": "speech-recognition",
            "usageDescription": "Create optional live captions using only the speech model installed on this device.",
        },
    ]
    latest = app["versions"][0]
    assert latest["version"] == "1.9.0"
    assert latest["minOSVersion"] == "17.0"
    assert latest["downloadURL"].endswith("/v1.9.0/Clip-Farm-Pilot-iOS-v1.9.0-Local-Unsigned.ipa")
    assert latest["size"] == 325032
