# Qronos phone app

A remote control for Qronos on the PC: type a question or hold a button and
speak; the PC answers. All reasoning, speech recognition, speech synthesis,
file access and computer control stay on the PC. The phone runs no model and
keeps no project data — not the answers, not the transcripts, not the audio.

## What exists, precisely

| Part | State | Evidence |
|---|---|---|
| `android/linkcore` — protocol core in Kotlin (frames, pairing code, relay hello, TLS 1.3 PSK client, command ids and retry, push-to-talk upload) | Implemented | Compiled with kotlinc 2.4.21 and through the committed Gradle build (Gradle 9.8.1, AGP 9.4.1 plugin resolved); 10 JUnit tests pass against vectors generated from the PC's Python code |
| Interoperability with the real PC link server | Verified on loopback | `linkcore/interop/run_interop.py`: Kotlin + BouncyCastle client against `core/link_server.py` (Python ssl/OpenSSL) directly and through the real relay with its outer TLS verified against a pinned throwaway CA — TLS 1.3 PSK handshake, Persian round trip, remote ceiling, action refusal, push-to-talk with spoken reply, a dropped connection retried without running the command twice; a wrong key and a revoked device are refused |
| `android/app` — the Android application (Persian RTL UI, deep-link pairing, Keystore-sealed key, Wi-Fi binding, relay fallback, push-to-talk, reply playback) | **Compiled and packaged as an unsigned APK without the Android SDK; not built through Gradle; never run** | `android/buildcheck/check_without_sdk.py`: resources compiled and linked by aapt2 9.4.1 against the Android 16 (API 36) framework from Robolectric's AOSP-built `android-all` jar, the Kotlin compiled against that API with no errors or warnings, dexed by D8 9.4.28 for minSdk 26. `aapt2 dump badging` shows `app.qronos.remote` 0.1.0, min 26 / target 36, exactly four permissions, label «کرونوس». The Gradle route still needs the Android SDK, whose licence was not accepted: AGP 9.4.1 evaluated the module and stopped at "SDK location not found" |
| iOS | **Not implemented, not supported** | No macOS/Xcode here. Apple's documentation for `sec_protocol_options_add_pre_shared_key` does not say it supports TLS 1.3 external PSKs, which the PC requires; that has to be settled on a Mac before any iOS work |

Nothing here has run on a phone. Pairing from a real camera app, Wi-Fi binding
with a real VPN running, microphone capture and playback, and the relay over a
mobile network are all untested, and are the first things to check when
acceptance testing is authorised.

## Why the TLS is BouncyCastle, not the platform

The PC speaks TLS 1.3 with an external pre-shared key and an ephemeral key
exchange (`psk_dhe_ke`), which gives forward secrecy (QRONOS_DEVICE_LINK.md
§4.1). Android's own TLS stack (Conscrypt) lists PSK cipher suites only for
TLS 1.0–1.2 — the Android `SSLSocket` reference and Conscrypt's
`CAPABILITIES.md` both say so — so it cannot complete that handshake.
BouncyCastle's TLS implements TLS 1.3 external PSKs (`BasicTlsPSKExternal`,
`psk_dhe_ke`); the client offers only TLS 1.3, only `psk_dhe_ke`, and refuses
any server that presents a certificate instead of the key. No cryptography is
written in this repository. The outer connection to the relay uses the
platform's own `SSLSocket`, verified like HTTPS.

Dependencies: `org.bouncycastle:bctls-jdk18on:1.86.2` and
`bcprov-jdk18on:1.86` (MIT-style BouncyCastle licence). `org.json` is part of
the Android platform; the JVM tests use the Maven artifact of the same API.

## How the phone reaches the PC

- **Never a VPN.** The manifest declares no `VpnService`; the app never takes
  the phone's VPN slot (QRONOS_DEVICE_LINK.md §2).
- **At home:** `ConnectivityManager.requestNetwork` with `TRANSPORT_WIFI`, and
  sockets from that network, so the PC is reachable on Wi-Fi even while a VPN
  holds the default route. Defeated only by an always-on VPN in lockdown mode.
- **Away:** through the relay, as an ordinary TLS connection that rides inside
  whatever VPN is running. The relay's address (and its CA, for a self-hosted
  relay) comes from the PC over the local link once the PC has given this
  phone remote access; it is never typed and never in the QR code.

## Pairing

The PC shows a QR code holding a `qronos://pair` link. The phone's camera app
opens it in this app (no scanning library), the app shows the computer's
address and asks for confirmation, and pairing completes on the local network
inside the PC's two-minute window. A scanned code is kept in memory only, never
shown or saved. The pairing key is sealed with an AES-256-GCM key generated in
the Android Keystore (usable only while the phone is unlocked, on Android 9+);
nothing of the app is backed up.

## Building

Protocol core, on any machine with JDK 17+ and Gradle 9.1+:

```sh
gradle -p mobile/android -Pqronos.withApp=false :linkcore:test
```

The app, through Gradle, needs the Android SDK (platform 36, build-tools 36)
and a `local.properties` with `sdk.dir`, then `gradle -p mobile/android
:app:assembleDebug`.

Without the SDK, as evidence (every tool open-source, no licence to accept;
the list and versions are in the script's docstring):

```sh
python mobile/android/buildcheck/check_without_sdk.py --jdk <jdk> --kotlinc <kotlinc home> \
    --aapt2 <aapt2> --android-all <android-all jar> --r8 <r8 jar> --maven <jar dir> --out <empty dir>
```

It produces an **unsigned** APK, which cannot be installed. Signing,
installing and distributing are separate steps that have not been taken. No Gradle wrapper jar is committed; run `gradle wrapper`
once to create one. The app is unsigned; signing and distribution are separate,
unauthorised steps.

Interop check (needs the core compiled, its test classes, kotlin-stdlib,
BouncyCastle and org.json on one classpath):

```sh
python mobile/android/linkcore/interop/run_interop.py --java <java> --classpath <classpath>
```

## Protocol contract

`protocol/link-vectors.json` is generated from the PC's Python modules by
`tests/fixtures/link_vectors.py`; `tests/test_link_mobile_contract.py` fails if
it is out of date, and the Kotlin tests read it. Change the protocol on the PC
first, regenerate, then make the phone agree.
