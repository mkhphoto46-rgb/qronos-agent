package app.qronos.link

import org.bouncycastle.tls.AlertDescription
import org.bouncycastle.tls.BasicTlsPSKExternal
import org.bouncycastle.tls.CertificateRequest
import org.bouncycastle.tls.CipherSuite
import org.bouncycastle.tls.PRFAlgorithm
import org.bouncycastle.tls.ProtocolVersion
import org.bouncycastle.tls.PskKeyExchangeMode
import org.bouncycastle.tls.AbstractTlsClient
import org.bouncycastle.tls.TlsAuthentication
import org.bouncycastle.tls.TlsClientProtocol
import org.bouncycastle.tls.TlsCredentials
import org.bouncycastle.tls.TlsFatalAlert
import org.bouncycastle.tls.TlsServerCertificate
import org.bouncycastle.tls.crypto.impl.bc.BcTlsCrypto
import java.io.InputStream
import java.io.OutputStream
import java.security.SecureRandom
import java.util.Vector

/**
 * The link's own TLS: TLS 1.3 with an external pre-shared key, end to end.
 *
 * Why BouncyCastle and not the platform: the PC speaks TLS 1.3 with an
 * external PSK and an ephemeral key exchange (`psk_dhe_ke`), measured to give
 * forward secrecy. Android's platform TLS (Conscrypt) lists PSK cipher suites
 * only for TLS 1.0–1.2 (Android `SSLSocket` reference; Conscrypt
 * CAPABILITIES.md), so it cannot complete this handshake. BouncyCastle's TLS
 * implements TLS 1.3 external PSKs (`BasicTlsPSKExternal`, binder label
 * "ext binder", `psk_dhe_ke`). It is an audited, widely used library; nothing
 * cryptographic is written here.
 *
 * Offered: TLS 1.3 only; ChaCha20-Poly1305 and AES-128-GCM, the SHA-256 suites
 * a SHA-256 PSK can use; `psk_dhe_ke` only, so a server that tried to skip the
 * ephemeral exchange could not complete the handshake. A server that answered
 * with a certificate instead of the key is refused: the PC has no
 * certificate, so anything presenting one is not the PC.
 */
internal class LinkPskClient(
    crypto: BcTlsCrypto,
    deviceId: String,
    key: ByteArray,
) : AbstractTlsClient(crypto) {

    private val identity = deviceId.toByteArray(Charsets.US_ASCII)
    private val psk = BasicTlsPSKExternal(identity, crypto.createSecret(key), PRFAlgorithm.tls13_hkdf_sha256)

    override fun getSupportedVersions(): Array<ProtocolVersion> = ProtocolVersion.TLSv13.only()

    override fun getSupportedCipherSuites(): IntArray = intArrayOf(
        CipherSuite.TLS_CHACHA20_POLY1305_SHA256,
        CipherSuite.TLS_AES_128_GCM_SHA256,
    )

    override fun getExternalPSKs(): Vector<Any?> = Vector<Any?>().apply { add(psk) }

    override fun getPskKeyExchangeModes(): ShortArray = shortArrayOf(PskKeyExchangeMode.psk_dhe_ke)

    override fun getAuthentication(): TlsAuthentication = object : TlsAuthentication {
        override fun notifyServerCertificate(serverCertificate: TlsServerCertificate?) {
            throw TlsFatalAlert(AlertDescription.handshake_failure)
        }

        override fun getClientCredentials(certificateRequest: CertificateRequest?): TlsCredentials? = null
    }

    /** "TLSv1.3 TLS_CHACHA20_POLY1305_SHA256", for a diagnostics screen. */
    fun summary(): String {
        val parameters = context?.securityParametersConnection ?: return "not connected"
        val suite = when (parameters.cipherSuite) {
            CipherSuite.TLS_CHACHA20_POLY1305_SHA256 -> "TLS_CHACHA20_POLY1305_SHA256"
            CipherSuite.TLS_AES_128_GCM_SHA256 -> "TLS_AES_128_GCM_SHA256"
            else -> "suite ${parameters.cipherSuite}"
        }

        // Named as Python's ssl names it, so both ends describe a session alike.
        val version = if (parameters.negotiatedVersion == ProtocolVersion.TLSv13) "TLSv1.3" else "${parameters.negotiatedVersion}"

        return "$version $suite"
    }
}

/** One handshaken link session over any pair of streams — a socket's, or the relay's. */
class PskSession internal constructor(
    private val protocol: TlsClientProtocol,
    private val client: LinkPskClient,
) {
    val input: InputStream get() = protocol.inputStream
    val output: OutputStream get() = protocol.outputStream

    fun summary(): String = client.summary()

    fun close() {
        try {
            protocol.close()
        } catch (_: Exception) {
        }
    }

    companion object {
        /** Run the handshake; throws if the PC does not hold the same key. */
        fun open(
            input: InputStream,
            output: OutputStream,
            deviceId: String,
            key: ByteArray,
            random: SecureRandom = SecureRandom(),
        ): PskSession {
            require(PairingUri.isDeviceId(deviceId)) { "bad device id" }
            require(key.size == PairingUri.KEY_BYTES) { "bad key length" }

            val client = LinkPskClient(BcTlsCrypto(random), deviceId, key)
            val protocol = TlsClientProtocol(input, output)
            protocol.connect(client)

            return PskSession(protocol, client)
        }
    }
}
