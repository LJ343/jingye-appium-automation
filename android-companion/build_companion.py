"""Offline manual build. Default is a read-only plan; never installs an APK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent
JDK = Path(r"C:\Program Files\Java\jdk-25.0.3\bin")
TOOLS = Path(r"C:\Users\YOUR_USER\adb\build-tools\35.0.0")
PLATFORM = Path(r"C:\Users\YOUR_USER\.openclaw-migration\migration\review-20261104-jingye-connection-recovery\android-companion\build-deps\android.jar")


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_files():
    files = [ROOT / "AndroidManifest.xml", *sorted((ROOT / "src").rglob("*.java"))]
    if (ROOT / "res").is_dir():
        files += sorted(p for p in (ROOT / "res").rglob("*") if p.is_file())
    if len(files) < 2 or any(not p.is_file() for p in files):
        raise RuntimeError("companion_sources_missing")
    return files


def source_hash():
    value = hashlib.sha256()
    for path in source_files():
        raw = path.read_bytes()
        value.update(path.relative_to(ROOT).as_posix().encode("utf-8") + b"\0")
        value.update(str(len(raw)).encode("ascii") + b"\0" + raw + b"\0")
    return value.hexdigest()


def plan():
    return {"planOnly": True, "sourceSHA256": source_hash(),
            "sourceFileCount": len(source_files()), "androidJarPresent": PLATFORM.is_file(),
            "jdk": str(JDK), "buildTools": str(TOOLS),
            "stages": ["existing build-unsigned.ps1 (javac --release 17, D8, aapt2)",
                       "zipalign", "apksigner verify"],
            "installsAPK": False, "deviceCommands": False}


def build(expected):
    if not expected or len(expected) != 64 or source_hash() != expected:
        raise RuntimeError("source_freeze_hash_missing_or_mismatch")
    dependency_receipt = ROOT / "build-deps" / "dependency-receipt.json"
    receipt = json.loads(dependency_receipt.read_text(encoding="utf-8"))
    if not PLATFORM.is_file() or sha256(PLATFORM) != receipt["androidJarSHA256"]:
        raise RuntimeError("pinned_platform_jar_not_verified")
    tool_files = {"java": JDK / "java.exe", "javac": JDK / "javac.exe",
                  "keytool": JDK / "keytool.exe", "aapt": TOOLS / "aapt.exe", "aapt2": TOOLS / "aapt2.exe",
                  "d8": TOOLS / "lib" / "d8.jar", "zipalign": TOOLS / "zipalign.exe",
                  "apksigner": TOOLS / "lib" / "apksigner.jar"}
    if any(not path.is_file() for path in tool_files.values()):
        raise RuntimeError("build_tool_missing")
    run = ROOT / "build" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
    run.mkdir(parents=True, exist_ok=False)
    log_path = run / "build.log"
    steps = []

    def command(stage, args, timeout=120):
        if source_hash() != expected:
            raise RuntimeError("source_changed_during_build")
        started = time.monotonic()
        result = subprocess.run([str(value) for value in args], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(stage + "\n" + result.stdout + result.stderr + "\n")
        steps.append({"stage": stage, "returnCode": result.returncode,
                      "elapsedSeconds": round(time.monotonic() - started, 3)})
        print(json.dumps(steps[-1]), flush=True)
        if result.returncode:
            raise RuntimeError("build_stage_failed_" + stage)
        return result

    # Reuse the source agent's build implementation, with a fresh output tree.
    unsigned_output = run / "unsigned"
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command("build_unsigned", [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               ROOT / "build-unsigned.ps1", "-AndroidJar", PLATFORM,
                               "-BuildTools", TOOLS, "-JdkHome", JDK.parent,
                               "-OutputDirectory", unsigned_output])
    unsigned = run / "companion-unsigned.apk"
    shutil.copyfile(unsigned_output / "jingye-connectionguard-unsigned.apk", unsigned)
    aligned = run / "companion-aligned.apk"
    command("zipalign", [tool_files["zipalign"], "-P", "16", "4", unsigned, aligned])
    private = Path(r"C:\Users\YOUR_USER\.openclaw-migration\migration\review-20261104-jingye-connection-recovery\android-companion\build-private")
    keystore, password_file = private / "companion-review.p12", private / "keystore-password.private.txt"
    if keystore.exists() != password_file.exists():
        raise RuntimeError("signing_material_incomplete_no_replacement")
    if not keystore.exists():
        raise RuntimeError("existing_signer_required_no_key_generation")
    signed = run / "connection-companion-review.apk"
    command("apksigner_sign", [tool_files["java"], "-jar", tool_files["apksigner"], "sign",
                               "--ks", keystore, "--ks-key-alias", "companion-review",
                               "--ks-pass", "file:" + str(password_file), "--out", signed, aligned])
    verified = command("apksigner_verify", [tool_files["java"], "-jar", tool_files["apksigner"],
                                           "verify", "--verbose", "--print-certs", signed])
    certificate_digests = re.findall(
        r"^Signer #\d+ certificate SHA-256 digest:\s*([0-9a-fA-F]{64})\s*$",
        verified.stdout, re.MULTILINE)
    if len(certificate_digests) != 1:
        raise RuntimeError("single_signer_certificate_digest_unconfirmed")
    command("zipalign_verify", [tool_files["zipalign"], "-c", "-P", "16", "4", signed])
    badging = command("manifest_verify", [tool_files["aapt"], "dump", "badging", signed]).stdout
    manifest = ET.parse(ROOT / "AndroidManifest.xml").getroot()
    namespace = "{http://schemas.android.com/apk/res/android}"
    expected_permissions = sorted(node.attrib[namespace + "name"] for node in manifest.findall("uses-permission"))
    packaged_permissions = sorted(re.findall(r"^uses-permission: name='([^']+)'", badging, re.MULTILINE))
    if ("package: name='" + manifest.attrib["package"] + "'") not in badging:
        raise RuntimeError("packaged_application_identity_mismatch")
    if "application-debuggable" in badging or packaged_permissions != expected_permissions:
        raise RuntimeError("packaged_permissions_or_debug_mode_mismatch")
    if source_hash() != expected:
        raise RuntimeError("source_changed_before_receipt")
    with zipfile.ZipFile(signed) as apk:
        names = apk.namelist()
        if "AndroidManifest.xml" not in names or "classes.dex" not in names or apk.testzip() is not None:
            raise RuntimeError("apk_content_invalid")
    output = {"passed": True, "sourceSHA256": expected,
              "sourceFiles": [{"path": p.relative_to(ROOT).as_posix(), "sha256": sha256(p)} for p in source_files()],
              "androidJarSHA256": sha256(PLATFORM),
              "toolSHA256": {name: sha256(path) for name, path in tool_files.items()},
              "buildScriptsSHA256": {name: sha256(ROOT / name) for name in
                                     ["build_companion.py", "build-companion.ps1", "build-unsigned.ps1"]},
              "apk": str(signed.relative_to(ROOT)), "apkBytes": signed.stat().st_size,
              "apkSHA256": sha256(signed), "steps": steps,
              "signatureVerified": True, "alignmentVerified": True,
              "signerCertificateSHA256": certificate_digests[0].lower(),
              "package": manifest.attrib["package"],
              "versionCode": manifest.attrib[namespace + "versionCode"],
              "versionName": manifest.attrib[namespace + "versionName"],
              "debuggable": False, "declaredPermissions": packaged_permissions,
              "installed": False, "deviceCommands": False, "globalSDKChanged": False}
    (run / "build-result.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-source-sha256")
    args = parser.parse_args()
    if args.execute:
        build(args.expected_source_sha256)
    else:
        print(json.dumps(plan()), flush=True)
