package app.qronos.link

import java.net.URI
import java.net.URISyntaxException
import java.net.URLDecoder
import java.util.Base64

/**
 * What the PC's QR code says: `qronos://pair?v=1&h=…&p=…&d=…&k=…&e=…`.
 *
 * Parsed exactly as `PairingPayload.from_uri` does on the PC: every field must
 * be present once, every value must have the right shape, and anything not
 * fully understood is refused rather than half-used. The key is the device's
 * pairing secret; this class never prints it.
 */
class PairingUri private constructor(
    val host: String,
    val port: Int,
    val deviceId: String,
    private val keyBytes: ByteArray,
    val expiresAt: Double,
) {
    /** A copy of the key, for the keystore. The caller should wipe it after. */
    fun key(): ByteArray = keyBytes.copyOf()

    override fun toString(): String =
        "PairingUri(host=$host, port=$port, deviceId=$deviceId, key=<redacted>, expiresAt=$expiresAt)"

    companion object {
        const val VERSION = 1
        const val KEY_BYTES = 32
        private val DEVICE_ID = Regex("^[0-9a-f]{16}$")

        fun isDeviceId(value: String): Boolean = DEVICE_ID.matches(value)

        fun parse(text: String): PairingUri {
            val uri = try {
                URI(text.trim())
            } catch (error: URISyntaxException) {
                throw PairingUriException("not a URI")
            }

            if (uri.scheme != "qronos") {
                throw PairingUriException("expected the qronos scheme")
            }

            // qronos://pair?… puts "pair" in the authority.
            val action = uri.rawAuthority ?: uri.rawPath?.trimStart('/') ?: ""

            if (action != "pair") {
                throw PairingUriException("unknown action")
            }

            val fields = HashMap<String, MutableList<String>>()

            for (part in (uri.rawQuery ?: "").split('&')) {
                if (part.isEmpty()) continue

                val name = decode(part.substringBefore('='))
                val value = decode(part.substringAfter('=', ""))
                fields.getOrPut(name) { mutableListOf() }.add(value)
            }

            fun single(name: String): String {
                val values = fields[name]

                if (values == null || values.size != 1) {
                    throw PairingUriException("missing field $name")
                }

                return values[0]
            }

            val version = single("v").toIntOrNull() ?: throw PairingUriException("malformed version")
            val port = single("p").toIntOrNull() ?: throw PairingUriException("malformed port")
            val expires = single("e").toDoubleOrNull() ?: throw PairingUriException("malformed expiry")

            if (version != VERSION) {
                throw PairingUriException("payload version $version is not supported")
            }

            if (port !in 1..65_535) {
                throw PairingUriException("port out of range")
            }

            val deviceId = single("d")

            if (!isDeviceId(deviceId)) {
                throw PairingUriException("bad device id")
            }

            val key = try {
                Base64.getUrlDecoder().decode(single("k"))
            } catch (error: IllegalArgumentException) {
                throw PairingUriException("malformed key")
            }

            if (key.size != KEY_BYTES) {
                throw PairingUriException("key is ${key.size} bytes, expected $KEY_BYTES")
            }

            val host = single("h")

            if (host.isEmpty()) {
                throw PairingUriException("empty host")
            }

            return PairingUri(host, port, deviceId, key, expires)
        }

        private fun decode(value: String): String = try {
            URLDecoder.decode(value, "UTF-8")
        } catch (error: IllegalArgumentException) {
            throw PairingUriException("malformed escape")
        }
    }
}

class PairingUriException(message: String) : IllegalArgumentException(message)
