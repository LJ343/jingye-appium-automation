"""Fetch the pinned official platform archive; extract only android.jar locally."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
URL = "https://dl.google.com/android/repository/platform-35-ext15_r01.zip"
SIZE = 64492044
SHA1 = "ce757091f933b7ac646bf197ce8957cc73b95614"
CURL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def valid_archive(path):
    return path.is_file() and path.stat().st_size == SIZE and digest(path, "sha1") == SHA1


def fetch():
    deps = ROOT / "build-deps"
    deps.mkdir(exist_ok=True)
    archive = deps / "platform-35-ext15_r01.zip"
    attempts = []
    if archive.exists() and not valid_archive(archive):
        raise RuntimeError("existing_platform_archive_failed_verification")
    if not archive.exists():
        for index in range(1, 4):
            part = deps / ("platform-download-" + uuid.uuid4().hex + ".part")
            started = time.monotonic()
            command = [str(CURL), "--fail", "--location", "--proto", "=https",
                       "--tlsv1.2", "--connect-timeout", "15", "--max-time", "180",
                       "--retry", "0", "--max-filesize", str(SIZE),
                       "--silent", "--show-error", "--output", str(part), URL]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=190)
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = "process_timeout"
            report = {"attempt": index, "returnCode": code,
                      "elapsedSeconds": round(time.monotonic() - started, 3),
                      "bytes": part.stat().st_size if part.exists() else 0}
            report["verified"] = valid_archive(part)
            attempts.append(report)
            print(json.dumps({"download": report}), flush=True)
            if report["verified"]:
                os.replace(part, archive)
                break
            if index < 3:
                time.sleep(2)
        if not valid_archive(archive):
            (deps / "download-failed.json").write_text(json.dumps(attempts, indent=2), encoding="utf-8")
            raise RuntimeError("official_platform_download_exhausted")
    with zipfile.ZipFile(archive) as package:
        entries = [info for info in package.infolist()
                   if not info.is_dir() and Path(info.filename).name == "android.jar"]
        if len(entries) != 1 or not 1024 < entries[0].file_size < 128 * 1024 * 1024:
            raise RuntimeError("platform_android_jar_entry_invalid")
        output = deps / "android.jar"
        part = deps / ("android.jar." + uuid.uuid4().hex + ".part")
        with package.open(entries[0]) as source, part.open("xb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        with zipfile.ZipFile(part) as jar:
            names = set(jar.namelist())
            required = {"android/app/Activity.class", "android/app/Service.class",
                        "android/provider/Settings$Global.class"}
            if not required.issubset(names) or jar.testzip() is not None:
                raise RuntimeError("android_jar_content_invalid")
        if output.exists() and digest(output) != digest(part):
            raise RuntimeError("existing_android_jar_differs_from_pinned_archive")
        os.replace(part, output)
    receipt = {"url": URL, "archiveBytes": archive.stat().st_size,
               "archiveSHA1": digest(archive, "sha1"), "archiveSHA256": digest(archive),
               "extractedEntry": entries[0].filename, "extractedFiles": ["android.jar"],
               "androidJarBytes": output.stat().st_size, "androidJarSHA256": digest(output),
               "attempts": attempts, "globalSDKChanged": False, "licenseAccepted": False}
    (deps / "dependency-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        fetch()
    else:
        print(json.dumps({"planOnly": True, "url": URL, "expectedBytes": SIZE,
                          "expectedSHA1": SHA1, "output": str(ROOT / "build-deps"),
                          "extractOnly": ["android.jar"], "attemptLimit": 3,
                          "attemptTimeoutSeconds": 190, "licenseAcceptance": False}))
