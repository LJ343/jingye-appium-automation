# Wireless connection status

Jingye-Wireless-Status.cmd checks once; Jingye-Wireless-Watch.cmd checks repeatedly. Review their Python path placeholder first. Or run the chosen Python executable with -B -X utf8 operations/jingye_wireless_status.py; --watch repeats and --json emits a sanitized result.

The tool reads operations/private/jingye-wireless.private.json and the existing local ADB smart socket. It does not start ADB, connect/disconnect, scan ports, pair, change settings, query/create Appium sessions or run business. It may read the one fixed phone identity property and compare its hash in memory. CONNECTED requires a real response from the expected phone, not just a device-list entry.

It checks the single configured IP or a MAC-authenticated, same-identity verified endpoint cache no older than 24 hours. Invalid cache falls back to original configured scope. Actual identity/endpoint/key values are not printed. OFFLINE or DISCONNECTED can mean a stale endpoint even while wireless debugging is enabled. The helper's switch-on/PC-unverified text cannot replace this PC check. Status viewing during idle time does not trigger connection recovery; tasks use the separate on-demand recovery layer.
