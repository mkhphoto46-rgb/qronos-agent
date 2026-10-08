package app.qronos.link

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File

/**
 * The phone against the PC's own bytes.
 *
 * `mobile/protocol/link-vectors.json` is produced from the Python modules the
 * PC runs, and a Python test fails if it falls out of date. So passing here
 * means agreeing with the PC, not with a second reading of the spec.
 */
class VectorsTest {

    private val vectors: JSONObject by lazy {
        val dir = System.getProperty("qronos.protocolDir") ?: "../../protocol"

        JSONObject(File(dir, "link-vectors.json").readText(Charsets.UTF_8))
    }

    private fun hex(text: String): ByteArray =
        ByteArray(text.length / 2) { i -> text.substring(i * 2, i * 2 + 2).toInt(16).toByte() }

    @Test
    fun relayRoutesAndHelloMatchThePc() {
        val cases = vectors.getJSONArray("relay")

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val secret = hex(case.getString("secret"))

            assertArrayEquals(hex(case.getString("route_key")), RelayWire.routeKey(secret))
            assertArrayEquals(hex(case.getString("route_id")), RelayWire.routeId(secret))
            assertArrayEquals(hex(case.getString("phone_hello")), RelayWire.phoneHello(secret))
            assertEquals(RelayWire.HELLO_BYTES, RelayWire.phoneHello(secret).size)
        }
    }

    @Test
    fun thePhoneHelloNeverCarriesTheRouteKeyOrThePairingKey() {
        val case = vectors.getJSONArray("relay").getJSONObject(0)
        val hello = RelayWire.phoneHello(hex(case.getString("secret"))).toHex()

        assertFalse(hello.contains(case.getString("route_key")))
        assertFalse(hello.contains(case.getString("secret")))
    }

    @Test
    fun framesFromThePcDecode() {
        val cases = vectors.getJSONArray("frames")

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val expect = case.getJSONObject("expect")
            val frame = LinkProtocol.readFrame(ByteArrayInputStream(hex(case.getString("bytes"))))

            when (expect.getString("kind")) {
                "response" -> {
                    frame as Frame.Response
                    assertEquals(expect.getLong("id"), frame.id)
                    assertEquals(expect.getBoolean("ok"), frame.ok)

                    if (frame.ok) {
                        assertTrue(sameJson(expect.get("result"), frame.result))
                    } else {
                        assertEquals(expect.getString("code"), frame.code)
                        assertEquals(expect.getString("error"), frame.error)
                    }
                }

                "event" -> {
                    frame as Frame.Event
                    assertEquals(expect.getString("topic"), frame.topic)
                    assertTrue(sameJson(expect.getJSONObject("data"), frame.data))
                }

                else -> fail("unknown vector kind")
            }
        }
    }

    @Test
    fun badFramesAreRefusedWithThePcsFaultNames() {
        val cases = vectors.getJSONArray("bad_frames")

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)

            try {
                LinkProtocol.readFrame(ByteArrayInputStream(hex(case.getString("bytes"))))
                fail("accepted ${case.getString("bytes")}")
            } catch (error: ProtocolException) {
                assertEquals(case.getString("bytes"), case.getString("fault"), error.fault)
            }
        }
    }

    @Test
    fun anEmptyStreamIsACleanEnd() {
        assertNull(LinkProtocol.readFrame(ByteArrayInputStream(ByteArray(0))))
    }

    @Test
    fun theFrameLimitMatches() {
        assertEquals(vectors.getInt("max_frame_bytes"), LinkProtocol.MAX_FRAME_BYTES)
    }

    @Test
    fun aRequestIsLengthPrefixedJsonTheServerUnderstands() {
        val cid = "0123456789abcdef0123456789abcdef"
        val bytes = LinkProtocol.encodeRequest(3, "ask", JSONObject().put("question", "سلام"), cid)
        val length = ((bytes[0].toInt() and 0xff) shl 24) or ((bytes[1].toInt() and 0xff) shl 16) or
            ((bytes[2].toInt() and 0xff) shl 8) or (bytes[3].toInt() and 0xff)
        val body = JSONObject(String(bytes, 4, bytes.size - 4, Charsets.UTF_8))

        assertEquals(bytes.size - 4, length)
        assertEquals("request", body.getString("kind"))
        assertEquals(3L, body.getLong("id"))
        assertEquals("ask", body.getString("op"))
        assertEquals("سلام", body.getJSONObject("params").getString("question"))
        assertEquals(cid, body.getString("cid"))
    }

    @Test
    fun aPingCarriesNoCommandIdAndAMalformedOneIsRefused() {
        val ping = JSONObject(String(LinkProtocol.encodeRequest(1, "ping", JSONObject(), null).drop(4).toByteArray(), Charsets.UTF_8))

        assertFalse(ping.has("cid"))

        try {
            LinkProtocol.encodeRequest(1, "ask", JSONObject(), "NOT-HEX")
            fail("malformed command id accepted")
        } catch (expected: IllegalArgumentException) {
        }
    }

    @Test
    fun validPairingCodesParseExactly() {
        val cases = vectors.getJSONObject("pairing").getJSONArray("valid")

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)
            val parsed = PairingUri.parse(case.getString("uri"))

            assertEquals(case.getString("host"), parsed.host)
            assertEquals(case.getInt("port"), parsed.port)
            assertEquals(case.getString("device_id"), parsed.deviceId)
            assertArrayEquals(hex(case.getString("key")), parsed.key())
            assertEquals(case.getDouble("expires_at"), parsed.expiresAt, 0.0)
            assertFalse(parsed.toString().contains(case.getString("key")))
        }
    }

    @Test
    fun invalidPairingCodesAreRefusedLikeThePcRefusesThem() {
        val cases = vectors.getJSONObject("pairing").getJSONArray("invalid")

        for (i in 0 until cases.length()) {
            val case = cases.getJSONObject(i)

            try {
                PairingUri.parse(case.getString("uri"))
                fail("accepted (${case.getString("why")}): ${case.getString("uri")}")
            } catch (expected: PairingUriException) {
            }
        }
    }

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it.toInt() and 0xff) }

    /** Structural JSON equality, ignoring key order and number boxing. */
    private fun sameJson(a: Any?, b: Any?): Boolean = when {
        a == null || a == JSONObject.NULL -> b == null || b == JSONObject.NULL
        a is JSONObject && b is JSONObject ->
            a.keySet() == b.keySet() && a.keySet().all { sameJson(a.get(it), b.get(it)) }
        a is JSONArray && b is JSONArray ->
            a.length() == b.length() && (0 until a.length()).all { sameJson(a.get(it), b.get(it)) }
        a is Number && b is Number -> a.toDouble() == b.toDouble()
        else -> a == b
    }
}
