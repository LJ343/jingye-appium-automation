# 0.4 update

Address reporting and provisioning below are unchanged. The recovery policy is now the bounded retry policy in [0.4 release](../docs/WIRELESS_RELEASE_0.4.md); statements below about byte-identical 0.2 policy describe the historical 0.3 release only. Same-signer 0.4 adds no permissions.

# Companion 0.3 address reporting

VersionCode 3, versionName 0.3-review. Only INTERNET is newly added. RecoveryController, FailureBoundary and StateStore are unchanged from 0.2. The APK uses the same certificate as 0.2; SHA256SUMS.txt and verification.json are Release assets. No signing keys or SDK/build dependencies are included.

Keep the existing private wireless configuration and add address_report using docs/address-report.example.json as a shape reference. The example is disabled and deliberately not usable. Generate a fresh 16-byte random reporter ID and 32-byte random HMAC key, record their lowercase hex locally, choose one fixed private PC IPv4 and UDP port, and restrict allowed_ipv4_cidrs to the authorized phone Wi-Fi /24. verified_cache_path must be in the same private directory used by the status configuration. Never commit the real configuration or paste its key in an issue, terminal transcript or release asset.

The computer's firewall needs one inbound UDP rule restricted to that fixed PC address/port, the actual Python image, current network category and authorized phone /24. Do not use an all-program/all-profile/all-source rule. The receiver is bound on demand and closed after connection recovery; no permanent PC service is installed.

Provisioning requires an already authorized, identity-verified ADB shell. Encode a local UTF-8 JSON object with exactly host (PC IPv4), port (integer), id (32 hex) and key (64 hex) as Base64. Call the package's status provider using method configure_address_report and that encoded argument. The call requires shell UID 2000 and DUMP; it writes only the app-private reporting configuration and does not start/arm the service or write the wireless switch. Base64 is not encryption: do not log or publish the command argument. Open the helper visibly and use Start / Resume once after installing/provisioning. The original stop button and notification remain the user controls.

To revoke reporting while retaining the helper, the same authorized shell can call clear_address_report with no argument; no switch write or service start results. Revoke/delete any matching PC-only private materials and precisely owned firewall rule after checking current jobs. Do not clear app data or generate a replacement signer merely to update or revoke reporting.

The shell /status query remains read-only and never returns the reporting key. Normal third-party apps cannot provision. Existing settings-write and notification grants must still be verified after any update. An already-enabled wireless switch alone does not establish the ADB transport; use the PC status viewer for a live identity check.

Source builds require Java, Android platform/build tools and a private signing store supplied locally. Archived wrapper default paths are placeholders and must be reviewed for the local environment; dependencies and original signing materials are intentionally excluded. A newly self-signed build cannot update an installed APK signed by the original certificate. Rebuilding is not a byte-for-byte reproducibility claim. Pure Java and Python tests need no phone or network; run tests/run_offline_export.py from the repository root with the chosen Python executable, setting JAVA_HOME if needed.
