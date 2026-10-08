package app.qronos.link

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.Socket
import java.security.SecureRandom
import java.util.Base64

/**
 * The phone's half of the device link: the Kotlin counterpart of
 * `core/link_client.py`, which is the specification it follows.
 *
 *  1. Get a socket from a [Dialer] — straight to the PC on Wi-Fi, or through
 *     the relay.
 *  2. Run TLS 1.3 with the pairing key inside it ([PskSession]).
 *  3. Send length-prefixed JSON requests; treat event frames as progress.
 *  4. Give every request but `ping` a fresh command id, and reuse it when
 *     retrying that request ([callWithRetry]), so the PC answers a retry from
 *     its ledger instead of running the command twice.
 *
 * The phone holds no model and no project data. What passes through here is
 * the user's question, the PC's answer, and push-to-talk audio on its way to
 * the PC; none of it is stored by this class.
 */
class LinkClient(
    private val deviceId: String,
    secret: ByteArray,
    private val dialer: Dialer,
    private val timeoutMs: Int = 30_000,
    private val random: SecureRandom = SecureRandom(),
) : AutoCloseable {

    private val secret = secret.copyOf()
    private var socket: Socket? = null
    private var session: PskSession? = null
    private var nextId = 1L
    private var destroyed = false

    val connected: Boolean get() = session != null

    override fun toString(): String = "LinkClient(deviceId=$deviceId, connected=$connected)"

    fun connect() {
        check(!destroyed) { "this client was closed" }
        check(session == null) { "already connected" }

        val opened = dialer.open(timeoutMs)

        try {
            session = PskSession.open(opened.getInputStream(), opened.getOutputStream(), deviceId, secret, random)
            socket = opened
        } catch (error: IOException) {
            opened.close()
            throw LinkException("the computer refused this device", error)
        }
    }

    /** Drop the connection; [connect] may be called again. */
    fun disconnect() {
        session?.close()
        session = null

        try {
            socket?.close()
        } catch (_: IOException) {
        }

        socket = null
    }

    /** Disconnect for good and wipe this client's copy of the pairing key. */
    override fun close() {
        disconnect()
        secret.fill(0)
        destroyed = true
    }

    fun tlsSummary(): String = session?.summary() ?: "not connected"

    /**
     * One request and its response. Events arriving meanwhile go to [onEvent].
     *
     * A connection that fails mid-call is dropped, so the caller can connect
     * again; a half-read stream cannot be trusted to be in step.
     */
    fun call(
        op: String,
        params: JSONObject = JSONObject(),
        commandId: String? = if (op == "ping") null else newCommandId(),
        onEvent: (Frame.Event) -> Unit = {},
    ): Frame.Response {
        val active = session ?: throw LinkException("not connected")
        val id = nextId++

        try {
            active.output.write(LinkProtocol.encodeRequest(id, op, params, commandId))
            active.output.flush()

            while (true) {
                when (val frame = LinkProtocol.readFrame(active.input)) {
                    null -> throw LinkException("the computer closed the connection")
                    is Frame.Event -> onEvent(frame)
                    is Frame.Response -> {
                        if (frame.id != id) {
                            throw LinkException("response ${frame.id} does not match request $id")
                        }

                        return frame
                    }
                }
            }
        } catch (error: IOException) {
            disconnect()
            throw LinkException("the connection failed", error)
        } catch (error: LinkException) {
            disconnect()
            throw error
        }
    }

    /**
     * One command that survives dropped connections without being repeated.
     *
     * The command id is chosen once and reused on every attempt. A command the
     * PC already ran is answered from its ledger; one still running comes back
     * as `command_in_progress` and is asked again after [waitMs]. Retrying with
     * a fresh id would be a second command, which is the mistake this exists
     * to prevent.
     */
    fun callWithRetry(
        op: String,
        params: JSONObject = JSONObject(),
        attempts: Int = 4,
        waitMs: Long = 1_000,
        sleep: (Long) -> Unit = { Thread.sleep(it) },
        onEvent: (Frame.Event) -> Unit = {},
    ): Frame.Response {
        val commandId = newCommandId()
        var lastError: LinkException? = null

        for (attempt in 0 until maxOf(1, attempts)) {
            if (attempt > 0) {
                sleep(waitMs)
            }

            try {
                if (!connected) {
                    connect()
                }

                val response = call(op, params, commandId, onEvent)

                if (!response.ok && response.code in RETRYABLE) {
                    lastError = null
                    continue
                }

                return response
            } catch (error: LinkException) {
                lastError = error
            }
        }

        throw lastError ?: LinkException("the computer kept saying it was still busy")
    }

    /**
     * Push-to-talk: send a recording, get the transcript and the answer.
     *
     * [pcm] is 16 kHz mono 16-bit little-endian PCM, at most 30 seconds — what
     * `AudioRecord` produces with `ENCODING_PCM_16BIT`. Each chunk is its own
     * command, so a chunk resent after a dropped connection is acknowledged,
     * not appended twice. With [replyAudio], the spoken answer arrives as WAV.
     */
    fun askByVoice(
        pcm: ByteArray,
        language: String = "fa",
        replyAudio: Boolean = false,
        onEvent: (Frame.Event) -> Unit = {},
    ): VoiceAnswer {
        require(pcm.isNotEmpty() && pcm.size % 2 == 0) { "PCM must be whole 16-bit samples" }

        val begun = callWithRetry(
            "voice_begin",
            JSONObject()
                .put("format", "pcm_s16le")
                .put("sample_rate", 16_000)
                .put("channels", 1)
                .put("language", language),
        ).orThrow()

        val voiceId = begun.resultObject().getString("voice_id")
        val step = begun.resultObject().getInt("max_chunk_bytes")
        val encoder = Base64.getEncoder()

        var seq = 0
        var offset = 0

        while (offset < pcm.size) {
            val end = minOf(pcm.size, offset + step)
            callWithRetry(
                "voice_chunk",
                JSONObject()
                    .put("voice_id", voiceId)
                    .put("seq", seq)
                    .put("data", encoder.encodeToString(pcm.copyOfRange(offset, end))),
            ).orThrow()
            seq += 1
            offset = end
        }

        val audio = ByteArrayOutputStream()
        var expected = 0

        val ended = callWithRetry(
            "voice_end",
            JSONObject().put("voice_id", voiceId).put("reply", if (replyAudio) "audio" else "text"),
            onEvent = { event ->
                if (event.topic == "voice_reply" && event.data.optInt("seq", -1) == expected) {
                    audio.write(Base64.getDecoder().decode(event.data.getString("data")))
                    expected += 1
                } else {
                    onEvent(event)
                }
            },
        ).orThrow()

        val result = ended.resultObject()
        val chunks = result.optInt("reply_audio_chunks", 0)

        return VoiceAnswer(
            transcript = result.optString("transcript", ""),
            answer = result.optString("answer", ""),
            refusedAction = result.optBoolean("refused_action", false),
            heardNothing = result.optBoolean("heard_nothing", false),
            replyWav = if (chunks > 0 && expected == chunks) audio.toByteArray() else null,
        )
    }

    private fun newCommandId(): String {
        val bytes = ByteArray(16)
        random.nextBytes(bytes)

        return bytes.joinToString("") { "%02x".format(it.toInt() and 0xff) }
    }

    companion object {
        /** Answers meaning "the same command, asked again later, may succeed". */
        val RETRYABLE = setOf("command_in_progress", "rate_limited")
    }
}

data class VoiceAnswer(
    val transcript: String,
    val answer: String,
    val refusedAction: Boolean,
    val heardNothing: Boolean,
    val replyWav: ByteArray?,
)

class LinkException(message: String, cause: Throwable? = null) : Exception(message, cause)

class LinkRefusedException(val code: String, message: String) : Exception(message)

/** The response if it succeeded; otherwise its code and message, as an exception. */
fun Frame.Response.orThrow(): Frame.Response {
    if (!ok) {
        throw LinkRefusedException(code, error)
    }

    return this
}
