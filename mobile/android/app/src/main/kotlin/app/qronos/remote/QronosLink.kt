package app.qronos.remote

import android.content.Context
import app.qronos.link.DirectDialer
import app.qronos.link.Frame
import app.qronos.link.LinkClient
import app.qronos.link.LinkException
import app.qronos.link.RelayDialer
import app.qronos.link.RelayEndpoint
import app.qronos.link.VoiceAnswer
import app.qronos.link.orThrow
import org.json.JSONObject
import javax.net.ssl.HttpsURLConnection

/** Which way the phone reached the PC. */
enum class Route { LOCAL, RELAY }

/** One answer, as the screen shows it. */
data class Answer(
    val text: String,
    val transcript: String?,
    val route: Route,
    val refusedAction: Boolean,
    val performedNothing: Boolean,
    val replyWav: ByteArray?,
)

/**
 * The phone's connection to its PC: local first, then the relay.
 *
 * Every call runs on a background thread (the activities use one executor).
 * A session is opened per request and closed after it: the phone is a remote
 * control used in short bursts, and a fresh session each time means a fresh
 * handshake, fresh ephemeral keys, and a revocation at the PC taking effect
 * at the very next press of a button.
 *
 * The phone stores no answers, no transcripts and no audio. Push-to-talk audio
 * goes from the recorder to the PC; a spoken reply goes from the PC to the
 * speaker. Both live only in memory.
 */
class QronosLink(context: Context) {

    private val vault = KeyVault(context)
    private val wifi = WifiRoute(context)

    fun paired(): PairedPc? = vault.pairedPc()

    fun forget() {
        wifi.release()
        vault.forget()
    }

    fun close() = wifi.release()

    /**
     * Complete pairing: the first handshake, from the local network, inside
     * the PC's two-minute window. Remote access is never part of pairing.
     */
    fun completePairing(): Route {
        val pc = paired() ?: throw LinkException("not paired")
        val factory = wifi.socketFactory() ?: throw LinkException("no Wi-Fi network")

        vault.withKey { key ->
            LinkClient(pc.deviceId, key, DirectDialer(pc.host, pc.port, factory), timeoutMs = 10_000).use { client ->
                client.connect()
                learnRelay(client.call("ping").orThrow())
            }
        }

        return Route.LOCAL
    }

    fun ask(question: String): Answer = withClient { client, route ->
        val response = client.callWithRetry("ask", JSONObject().put("question", question)).orThrow()
        val result = response.resultObject()

        Answer(
            text = result.optString("answer", ""),
            transcript = null,
            route = route,
            refusedAction = result.optBoolean("refused_action", false),
            performedNothing = (result.optJSONArray("performed_actions")?.length() ?: 0) == 0,
            replyWav = null,
        )
    }

    fun askByVoice(pcm: ByteArray, replyAudio: Boolean): Answer = withClient { client, route ->
        val voice: VoiceAnswer = client.askByVoice(pcm, language = "fa", replyAudio = replyAudio)

        Answer(
            text = voice.answer,
            transcript = voice.transcript,
            route = route,
            refusedAction = voice.refusedAction,
            performedNothing = true,
            replyWav = voice.replyWav,
        )
    }

    /** Local network first; the relay only if the PC told us about one. */
    private fun <T> withClient(block: (LinkClient, Route) -> T): T {
        val pc = paired() ?: throw LinkException("not paired")

        return vault.withKey { key ->
            val local = wifi.socketFactory(timeoutMs = 2_000)?.let { factory ->
                tryConnect(LinkClient(pc.deviceId, key, DirectDialer(pc.host, pc.port, factory), timeoutMs = 4_000))
            }

            if (local != null) {
                local.use { client ->
                    learnRelay(client.call("ping").orThrow())
                    block(client, Route.LOCAL)
                }
            } else {
                val relay = pc.relay ?: throw LinkException("the computer is not reachable on this network, and remote access is not set up")
                val dialer = RelayDialer(
                    relay,
                    key,
                    hostnameCheck = { name, session -> HttpsURLConnection.getDefaultHostnameVerifier().verify(name, session) },
                )

                LinkClient(pc.deviceId, key, dialer, timeoutMs = 20_000).use { client ->
                    client.connect()
                    block(client, Route.RELAY)
                }
            }
        }
    }

    private fun tryConnect(client: LinkClient): LinkClient? = try {
        client.connect()
        client
    } catch (_: Exception) {
        client.close()
        null
    }

    /**
     * Keep the relay details the PC hands over on the local network, or forget
     * them when it stops offering them (the opt-in lapsed or was withdrawn).
     */
    private fun learnRelay(ping: Frame.Response) {
        val remote = ping.resultObject().optJSONObject("remote")
        val relay = remote?.optJSONObject("relay")

        vault.saveRelay(
            relay?.let {
                RelayEndpoint(
                    host = it.getString("host"),
                    port = it.optInt("port", 443),
                    serverName = if (it.has("server_name")) it.getString("server_name") else null,
                    caPem = if (it.has("ca_pem")) it.getString("ca_pem") else null,
                )
            },
        )
    }
}
