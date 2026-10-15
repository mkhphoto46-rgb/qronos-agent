"""
The phone app's contract with the PC, checked from the PC's side.

These checks need no Android SDK or Gradle run (the standard build is
documented in mobile/README.md), so they hold against the sources alone:

    the protocol vectors the phone's tests read are exactly what the PC's
    Python code produces, and every case agrees with the PC's own parser;
    the manifest asks for no VPN service and only the permissions the link
    needs, keeps nothing in backups and allows no cleartext;
    the product text is Persian;
    the constants the phone hard-codes match the PC's.

The phone's own JUnit tests and the interop run cover behaviour; these keep
the two sides from drifting apart without anyone compiling the phone.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from core.link_pairing import PairingPayload, PairingPayloadError
from core.link_protocol import MAX_FRAME_BYTES, ProtocolError, parse_frame, read_frame, reader_over
from core.link_relay_wire import HELLO_BYTES, MAGIC, ROUTE_ID_LABEL, ROUTE_KEY_LABEL
from core.link_voice import MAX_VOICE_SECONDS, VOICE_FORMAT, VOICE_SAMPLE_RATE
from tests.fixtures.link_vectors import VECTORS_PATH, render


ROOT = Path(__file__).resolve().parent.parent
MOBILE = ROOT / "mobile"
APP = MOBILE / "android" / "app" / "src" / "main"
CORE_KT = MOBILE / "android" / "linkcore" / "src" / "main" / "kotlin" / "app" / "qronos" / "link"
ANDROID = "{http://schemas.android.com/apk/res/android}"


class TestVectors(unittest.TestCase):
    def test_the_committed_vectors_are_what_the_pc_produces(self) -> None:
        self.assertEqual(
            VECTORS_PATH.read_text(encoding="utf-8"),
            render(),
            "mobile/protocol/link-vectors.json is stale; run python -m tests.fixtures.link_vectors",
        )

    def test_every_invalid_pairing_code_is_also_refused_by_the_pc(self) -> None:
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

        for case in vectors["pairing"]["invalid"]:
            with self.subTest(why=case["why"]):
                with self.assertRaises(PairingPayloadError):
                    PairingPayload.from_uri(case["uri"])

    def test_every_bad_frame_is_also_refused_by_the_pc(self) -> None:
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

        for case in vectors["bad_frames"]:
            if case["fault"] == "unexpected_frame":
                # A request is valid for the PC to receive; only the phone
                # refuses one, because the PC never sends it.
                continue

            with self.subTest(fault=case["fault"]):
                with self.assertRaises(ProtocolError) as caught:
                    payload = read_frame(reader_over(bytes.fromhex(case["bytes"])))
                    parse_frame(payload)

                self.assertEqual(caught.exception.fault.value, case["fault"])


class TestManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = ET.parse(APP / "AndroidManifest.xml").getroot()

    def test_no_vpn_service_or_permission(self) -> None:
        # The phone must never take the VPN slot the user's own VPN occupies.
        # Parsed elements only: the manifest's comments explain this rule and
        # are allowed to name what they rule out.
        values = [
            value
            for element in self.manifest.iter()
            for value in element.attrib.values()
        ]

        self.assertFalse([v for v in values if "VPN" in v.upper()])
        self.assertEqual(list(self.manifest.iter("service")), [])

        code = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (APP / "kotlin").rglob("*.kt")
        )

        self.assertNotIn("VpnService", code)
        self.assertNotIn("import android.net.Vpn", code)

    def test_only_the_permissions_the_link_needs(self) -> None:
        permissions = {
            element.get(f"{ANDROID}name") for element in self.manifest.iter("uses-permission")
        }

        self.assertEqual(
            permissions,
            {
                "android.permission.INTERNET",
                "android.permission.ACCESS_NETWORK_STATE",
                "android.permission.CHANGE_NETWORK_STATE",
                "android.permission.RECORD_AUDIO",
            },
        )

    def test_nothing_is_backed_up_and_nothing_is_cleartext(self) -> None:
        application = self.manifest.find("application")

        self.assertEqual(application.get(f"{ANDROID}allowBackup"), "false")
        self.assertEqual(application.get(f"{ANDROID}usesCleartextTraffic"), "false")
        self.assertEqual(application.get(f"{ANDROID}fullBackupContent"), "false")

        rules = (APP / "res" / "xml" / "data_extraction_rules.xml").read_text(encoding="utf-8")
        self.assertEqual(rules.count('<exclude domain="sharedpref" path="." />'), 2)

        security = (APP / "res" / "xml" / "network_security_config.xml").read_text(encoding="utf-8")
        self.assertIn('cleartextTrafficPermitted="false"', security)
        self.assertNotIn('src="user"', security)

    def test_pairing_opens_from_the_qr_link(self) -> None:
        schemes = [
            (data.get(f"{ANDROID}scheme"), data.get(f"{ANDROID}host"))
            for data in self.manifest.iter("data")
        ]

        self.assertEqual(schemes, [("qronos", "pair")])


class TestPersianProduct(unittest.TestCase):
    def test_every_string_is_persian(self) -> None:
        strings = ET.parse(APP / "res" / "values" / "strings.xml").getroot()
        persian = re.compile(r"[؀-ۿ]")

        for item in strings.iter("string"):
            with self.subTest(name=item.get("name")):
                self.assertRegex(item.text or "", persian)

    def test_the_screens_are_right_to_left(self) -> None:
        for layout in ("activity_main.xml", "activity_pair.xml"):
            with self.subTest(layout=layout):
                text = (APP / "res" / "layout" / layout).read_text(encoding="utf-8")
                self.assertIn('android:layoutDirection="rtl"', text)

        self.assertIn('android:supportsRtl="true"', (APP / "AndroidManifest.xml").read_text(encoding="utf-8"))


class TestPhoneConstantsMatchThePc(unittest.TestCase):
    def source(self, name: str) -> str:
        return (CORE_KT / name).read_text(encoding="utf-8")

    def test_relay_labels_and_sizes(self) -> None:
        wire = self.source("RelayWire.kt")

        self.assertIn(f'"{MAGIC.decode()}"', wire)
        self.assertIn(f'"{ROUTE_KEY_LABEL.decode()}"', wire)
        self.assertIn(f'"{ROUTE_ID_LABEL.decode()}"', wire)
        self.assertEqual(HELLO_BYTES, 4 + 1 + 32)
        self.assertIn("const val HELLO_BYTES = 4 + 1 + TOKEN_BYTES", wire)

    def test_frame_limit(self) -> None:
        self.assertEqual(MAX_FRAME_BYTES, 1 << 20)
        self.assertIn("const val MAX_FRAME_BYTES = 1 shl 20", self.source("LinkProtocol.kt"))

    def test_push_to_talk_format(self) -> None:
        client = self.source("LinkClient.kt")
        recorder = (APP / "kotlin" / "app" / "qronos" / "remote" / "PushToTalk.kt").read_text(encoding="utf-8")

        self.assertIn(f'.put("format", "{VOICE_FORMAT}")', client)
        self.assertIn(f'.put("sample_rate", {VOICE_SAMPLE_RATE:_})', client)
        self.assertIn(f"const val SAMPLE_RATE = {VOICE_SAMPLE_RATE:_}", recorder)
        self.assertIn(f"const val MAX_SECONDS = {MAX_VOICE_SECONDS}", recorder)

    def test_the_link_tls_is_tls13_psk_dhe_only(self) -> None:
        tls = self.source("PskTls.kt")

        self.assertIn("ProtocolVersion.TLSv13.only()", tls)
        self.assertIn("shortArrayOf(PskKeyExchangeMode.psk_dhe_ke)", tls)
        self.assertNotIn("psk_ke)", tls.replace("psk_dhe_ke)", ""))


#: The one binary allowed under mobile/: the official Gradle 9.8.1 wrapper jar, pinned by its published SHA-256.
GRADLE_WRAPPER = ("mobile/android/gradle/wrapper/gradle-wrapper.jar",
                  "3b8a25775a69158b5ad2b1d17a80a88dc7b40a352a73eb27d06c612a7ce68e98")


class TestNothingHeavyInTheApp(unittest.TestCase):
    def test_no_model_or_binary_files_are_committed_under_mobile(self) -> None:
        forbidden = {".onnx", ".gguf", ".bin", ".tflite", ".pt", ".apk", ".aab", ".jar", ".so", ".keystore", ".jks"}
        found = [
            path
            for path in MOBILE.rglob("*")
            if path.is_file()
            and "build" not in path.relative_to(MOBILE).parts
            and ".gradle" not in path.relative_to(MOBILE).parts
            and path.suffix.lower() in forbidden
        ]
        wrapper_path, wrapper_sha256 = GRADLE_WRAPPER
        offenders = []
        for path in found:
            relative = path.relative_to(ROOT).as_posix()
            if relative == wrapper_path and hashlib.sha256(path.read_bytes()).hexdigest() == wrapper_sha256:
                continue  # exactly the pinned official wrapper; any other bytes there are an offender
            offenders.append(relative)

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
