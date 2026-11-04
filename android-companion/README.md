# Android connection companion source

Pure Java/API35 source and local build scripts only. APKs, private signing material, downloaded dependencies, device state and diagnostics are excluded. This app observes Wi-Fi and can request adb_wifi_enabled=1 after explicit visible user activation; it cannot enable Wi-Fi or approve network trust dialogs. Installation and WRITE_SECURE_SETTINGS must be separately authorized.

Run test-offline.ps1 for the deterministic local tests. fetch_build_dependency.py defaults to plan mode; --execute downloads only its pinned official platform archive into ignored build-deps. build-companion.ps1 defaults to plan mode; -Build requires the exact reviewed source aggregate SHA256. Adjust local build-tool/Python paths for your environment. Signing material remains in ignored build-private; retain it privately for consistent updates. No build script installs an APK or contacts a device.

Phone recovery and long observation must be evidenced separately. See the [wireless release record](../docs/WIRELESS_RELEASE.md) and the source's visible pause/stop behavior. The companion has no boot receiver; reopening and rearming are required after a phone reboot. A new Wi-Fi network may require the system trust confirmation.
