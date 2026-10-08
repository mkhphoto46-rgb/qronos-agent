package app.qronos.link

import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * The 37-byte relay hello and the relay's one-byte answers, phone side.
 *
 * Defined in `core/link_relay_wire.py`. The phone sends its route id — the
 * SHA-256 of the route key, which is an HMAC of the pairing key — and never
 * the route key or the pairing key itself. The relay can therefore join the
 * phone to its PC and do nothing else with what it was told.
 */
object RelayWire {
    val MAGIC: ByteArray = "QRL1".toByteArray(Charsets.US_ASCII)
    const val TOKEN_BYTES = 32
    const val HELLO_BYTES = 4 + 1 + TOKEN_BYTES

    const val ROLE_HOST: Byte = 1
    const val ROLE_PHONE: Byte = 2

    private val ROUTE_KEY_LABEL = "qronos relay route key v1".toByteArray(Charsets.US_ASCII)
    private val ROUTE_ID_LABEL = "qronos relay route id v1".toByteArray(Charsets.US_ASCII)

    fun routeKey(secret: ByteArray): ByteArray {
        require(secret.isNotEmpty()) { "a route needs a pairing key" }

        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret, "HmacSHA256"))

        return mac.doFinal(ROUTE_KEY_LABEL)
    }

    fun routeId(secret: ByteArray): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        digest.update(ROUTE_ID_LABEL)
        digest.update(routeKey(secret))

        return digest.digest()
    }

    fun phoneHello(secret: ByteArray): ByteArray = MAGIC + byteArrayOf(ROLE_PHONE) + routeId(secret)
}

/** The relay's answers. Anything else is treated as the end, as on the PC. */
enum class RelayStatus(val code: Int, val terminal: Boolean) {
    PARKED(0x00, false),
    SPLICED(0x01, false),
    KEEPALIVE(0x02, false),
    NO_HOST(0x10, true),
    ROUTE_FULL(0x11, true),
    BAD_HELLO(0x12, true),
    OVER_CAPACITY(0x13, true),
    PARK_EXPIRED(0x14, true);

    companion object {
        fun of(code: Int): RelayStatus? = values().firstOrNull { it.code == code }
    }
}
