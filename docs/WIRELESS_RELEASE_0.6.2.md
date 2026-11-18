# 0.6.2 candidate — live acceptance pending

This candidate has passed offline checks and a same-signer APK build. It has not yet passed installation verification or HONOR live pairing acceptance. Production wireless and USB branches remain unchanged.

## Exact Wi-Fi-name trust

The existing per-AP list remains valid. A separate foreground action adds an exact Wi-Fi name, including case, spaces and Unicode, and explicitly confirms trust for all APs with that exact name. There is no prefix, wildcard or automatic network import. Only a digest is stored. Adding a name does not grant Android system trust by itself. Both modes use the original shared click budgets and recheck the actual current AP before each action. Removing a name preserves per-AP entries; clearing per-AP entries preserves names and disables auto-confirmation. Location permission/system location remain necessary to identify the current Wi-Fi.

## Automatic re-pairing

The default is disabled. A healthy verified connection returns normally without opening Settings. Recovery first uses existing connections, mDNS, cache and the bounded single-phone port discovery. An actual authentication failure for a fresh HMAC-authenticated phone IP can authorize a request. A separate explicit desktop opt-in, `allow_verified_discovery_repair: true`, also permits a request after a completed unsuccessful discovery sweep while the same phone IP still has a fresh authenticated report. A single offline/refused/timeout result or an incomplete sweep is insufficient. The phone additionally requires renewed 0.6.2 foreground policy approval; old enabled settings alone do not approve the expanded policy.

The original configured computer signs a short-lived request with the existing private address-channel key. With independent foreground approval in the helper, the first authenticated request fixes that computer's original ADB public-key fingerprint; later different host keys are rejected. No ADB key is regenerated. Installation alone does not enable pairing.

On an unlocked, interactive phone, with helper/accessibility active and an explicitly trusted AP or exact name, the helper opens the official system developer/wireless pairing page. It accepts only identified system Settings windows and exact recognized controls. The one-time code and pairing endpoint are AES-256-GCM encrypted using a separately derived key and request/host-bound additional authenticated data, then authenticated on the existing address UDP channel. They are not written to diagnostic records, clipboard, command line or files. While an authenticated request/code is in flight, the computer pauses slow discovery to prevent code expiration. It supplies the code to the pinned existing-server ADB client through stdin. Only after successful pairing, a matching offline transport for that exact authenticated connection endpoint may be disconnected once; online/other endpoints are untouched. It then separately verifies the connected phone identity. A successful pair command alone is insufficient.

On the recognized, platform-signed developer-options page only, the helper reserves at most six scroll actions in one request (including an advertised scroll-to-top action when available), against one identified list. Other pages and ambiguous lists cannot scroll. Scrolling does not touch debug switches. The flow lasts at most 30 seconds and is limited to two persisted requests in ten minutes. No permanent pairing-page polling occurs. It does not unlock the phone, approve unlisted networks, remove other pairings, disable protection, change business rules or restart any service. An unknown OEM interface fails closed and requires a compatibility fix; support must be demonstrated on the actual phone.

## First upgrade while currently unpaired

1. Download and verify the APK from this prerelease. Install as an update using the normal phone installer; do not uninstall or clear data. Existing private address configuration and AP entries must be retained.
2. Open 0.6.2. Add the exact company Wi-Fi name if desired; retain the original per-AP entries. Enable the existing whitelist auto-confirmation and check Android accessibility remains enabled for this helper.
3. Enable “本电脑自动重新配对” again in 0.6.2 and read its updated confirmation, even when it was enabled in an earlier version. This records new policy approval without changing the fixed host or retry ledger. This permits the original authenticated address-channel computer to pin its own existing debug key once. Keep the phone unlocked during live acceptance. Do not generate or send a pairing code manually.
4. The maintainer runs connection-only acceptance from the candidate layer. This opens official Settings when authenticated recovery is required. No Appium business session or collection is started.

The desktop optional `pairing_recovery` configuration has `enabled`, `host_key_sha256` (SHA256 of the original 524-byte decoded adbkey.pub token), and an absolute private `ledger_path`. These are local settings and are not provided in this public export. The shared address-channel secret remains private. Python cryptography 48.0.0 was observed and tested; it is only imported for this enabled feature.

## Evidence and remaining work

347 Python tests and 211 helper assertions passed, including legacy recovery/trust behavior, persistent retry budgets, exact SSID mode, replay/expiry/host/source rejection, encrypted Java/Python protocol interoperability, and stdin-only code handling. APK permissions, package, source aggregate and the existing signing certificate were checked. These checks do not demonstrate real HONOR UI compatibility, locked-screen recovery, natural roaming, future trust persistence, or a successful business rerun. See `recovery-0.6.2-verification.json` and release SHA256 files. No private configuration, signing material or business logs are published.

## Why 0.6.2 follows the 0.6.1 live attempt

In the initial live attempt the authenticated address channel worked, but cached offline entries returned “already connected” followed by an unusable device state, without the anticipated explicit authentication rejection. Increasing the candidate handshake timeout did not resolve this. A separately authorized initial-bootstrap request reached the phone and passed its local gates, but ended at the developer-options page with the wireless row offscreen. No pairing command was executed. These are failed acceptance observations, not a successful unattended recovery or a proven cause of historical pairing loss.

0.6.2 addresses the bounded-navigation gap, supplies the explicitly approved complete-discovery failure trigger, and prevents slow scans from outliving one-time codes. The asynchronous boundary fix from 0.6.1 is retained. Offline tests and source/APK signing checks passed; actual HONOR navigation and end-to-end pairing still require acceptance before production deployment. No business rules or host service lifecycle are changed.
