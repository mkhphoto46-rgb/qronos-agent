package app.qronos.link

import java.io.ByteArrayInputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.security.KeyStore
import java.security.cert.CertificateFactory
import javax.net.SocketFactory
import javax.net.ssl.SNIHostName
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSession
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManagerFactory

/**
 * How a phone reaches its PC: a connected socket ready for the link's own TLS.
 *
 * Two ways, chosen by where the phone is. Both use the platform's ordinary
 * socket APIs. Neither creates or claims a VPN interface: the relay path is an
 * ordinary TLS connection that rides inside whatever VPN the user already
 * runs, and the local path asks for the Wi-Fi network specifically.
 */
fun interface Dialer {
    fun open(timeoutMs: Int): Socket
}

/**
 * Straight to the PC on the local network.
 *
 * On Android, [socketFactory] should be the Wi-Fi network's
 * (`Network.getSocketFactory()` after `requestNetwork` with `TRANSPORT_WIFI`),
 * so the socket uses Wi-Fi even while a VPN holds the default route. That is
 * the documented platform route around a VPN that captures private ranges; it
 * is defeated only by an always-on VPN in lockdown mode.
 */
class DirectDialer(
    private val host: String,
    private val port: Int,
    private val socketFactory: SocketFactory = SocketFactory.getDefault(),
) : Dialer {
    override fun open(timeoutMs: Int): Socket {
        val socket = socketFactory.createSocket()

        try {
            socket.connect(InetSocketAddress(host, port), timeoutMs)
            socket.soTimeout = timeoutMs

            return socket
        } catch (error: IOException) {
            socket.close()
            throw error
        }
    }
}

/** Where the relay is. The PC hands this to the phone over the local link. */
data class RelayEndpoint(
    val host: String,
    val port: Int = 443,
    val serverName: String? = null,
    val caPem: String? = null,
) {
    val tlsName: String get() = (serverName ?: host).lowercase()

    override fun toString(): String = "RelayEndpoint(host=$host, port=$port, serverName=$serverName, pinnedCa=${caPem != null})"
}

class RelayRefusedException(val status: RelayStatus?) :
    IOException("the relay refused: ${status?.name ?: "unknown answer"}")

/**
 * Through the relay, from anywhere.
 *
 * The outer TLS is verified exactly like an HTTPS connection: certificate
 * chain against the platform trust store, or against the relay's own CA when
 * the PC handed one over, and the host name — by endpoint identification,
 * plus [hostnameCheck] where the platform wants an explicit verifier (on
 * Android, `HttpsURLConnection.getDefaultHostnameVerifier()`, because a bare
 * `SSLSocket` does not check names on its own). Then the phone hello, then
 * wait for SPLICED. The socket returned is the outer connection; the link's
 * own TLS runs inside it.
 */
class RelayDialer(
    private val endpoint: RelayEndpoint,
    private val secret: ByteArray,
    private val socketFactory: SocketFactory = SocketFactory.getDefault(),
    private val hostnameCheck: ((String, SSLSession) -> Boolean)? = null,
) : Dialer {

    override fun open(timeoutMs: Int): Socket {
        val raw = socketFactory.createSocket()

        try {
            raw.connect(InetSocketAddress(endpoint.host, endpoint.port), timeoutMs)
            raw.soTimeout = timeoutMs

            val tls = sslSocketFactory().createSocket(raw, endpoint.tlsName, endpoint.port, true) as SSLSocket
            val parameters = tls.sslParameters
            parameters.endpointIdentificationAlgorithm = "HTTPS"
            parameters.serverNames = listOf(SNIHostName(endpoint.tlsName))
            tls.sslParameters = parameters
            tls.startHandshake()

            if (hostnameCheck != null && !hostnameCheck.invoke(endpoint.tlsName, tls.session)) {
                throw IOException("the relay's certificate is not for ${endpoint.tlsName}")
            }

            tls.outputStream.write(RelayWire.phoneHello(secret))
            tls.outputStream.flush()

            while (true) {
                val byte = tls.inputStream.read()
                val status = if (byte < 0) null else RelayStatus.of(byte)

                when {
                    status == RelayStatus.SPLICED -> return tls
                    status == RelayStatus.KEEPALIVE -> continue
                    else -> throw RelayRefusedException(status)
                }
            }
        } catch (error: IOException) {
            raw.close()
            throw error
        }
    }

    private fun sslSocketFactory(): SSLSocketFactory {
        val pem = endpoint.caPem ?: return SSLSocketFactory.getDefault() as SSLSocketFactory

        // A self-hosted relay's own CA replaces the system store for this one
        // connection: trusted on its own terms, and nothing else is.
        val certificate = CertificateFactory.getInstance("X.509")
            .generateCertificate(ByteArrayInputStream(pem.toByteArray(Charsets.US_ASCII)))
        val store = KeyStore.getInstance(KeyStore.getDefaultType())
        store.load(null, null)
        store.setCertificateEntry("qronos-relay-ca", certificate)

        val trust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
        trust.init(store)

        val context = SSLContext.getInstance("TLS")
        context.init(null, trust.trustManagers, null)

        return context.socketFactory
    }
}
