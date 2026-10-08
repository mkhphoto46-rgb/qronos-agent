package app.qronos.remote

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.net.SocketFactory

/**
 * A socket factory bound to the phone's Wi-Fi network, VPN or not.
 *
 * Measured on a phone in Iran (QRONOS_DEVICE_LINK.md §2): with a VPN running,
 * a browser could not reach the PC on the home network, because the VPN takes
 * the default route for private addresses too. A native app can ask Android
 * for the Wi-Fi network itself — `requestNetwork` with `TRANSPORT_WIFI` — and
 * open its sockets there, which the platform documents as bypassing the
 * default route. The VPN keeps running untouched; this app never asks for
 * the VPN slot. An always-on VPN in lockdown mode still blocks this, and the
 * app then falls back to the relay.
 *
 * Whether this works with the VPNs users actually run is the first thing to
 * check on a real phone; it has not been tested on one.
 */
class WifiRoute(context: Context) {

    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private var callback: ConnectivityManager.NetworkCallback? = null

    @Volatile
    private var network: Network? = null

    /** The Wi-Fi network's socket factory, or null if there is no Wi-Fi in [timeoutMs]. */
    fun socketFactory(timeoutMs: Long = 3_000): SocketFactory? {
        network?.let { return it.socketFactory }

        val ready = CountDownLatch(1)
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()

        val listener = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(available: Network) {
                network = available
                ready.countDown()
            }

            override fun onLost(lost: Network) {
                if (network == lost) {
                    network = null
                }
            }

            override fun onUnavailable() {
                ready.countDown()
            }
        }

        release()
        callback = listener
        connectivity.requestNetwork(request, listener, timeoutMs.toInt())
        ready.await(timeoutMs + 500, TimeUnit.MILLISECONDS)

        return network?.socketFactory
    }

    fun release() {
        callback?.let {
            try {
                connectivity.unregisterNetworkCallback(it)
            } catch (_: IllegalArgumentException) {
            }
        }

        callback = null
        network = null
    }
}
