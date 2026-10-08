package app.qronos.link

import org.json.JSONObject
import java.io.File
import kotlin.math.PI
import kotlin.math.sin
import kotlin.system.exitProcess

/**
 * Drives the real PC link server with this client. Not a unit test: it needs a
 * running Qronos link server, and `mobile/android/linkcore/interop/run_interop.py`
 * starts one — on loopback, with a fresh throwaway device key and, for the
 * relay path, the real relay with a throwaway certificate authority.
 *
 *     direct <host> <port> <deviceId> <keyHex>
 *     relay  <relayHost> <relayPort> <serverName> <caPemFile> <deviceId> <keyHex>
 *
 * Prints one JSON object per check and exits non-zero on the first failure.
 */
fun main(args: Array<String>) {
    try {
        run(args)
    } catch (error: Throwable) {
        println(JSONObject().put("check", "fatal").put("ok", false).put("error", error.toString()))
        exitProcess(1)
    }
}

private fun hex(text: String): ByteArray =
    ByteArray(text.length / 2) { i -> text.substring(i * 2, i * 2 + 2).toInt(16).toByte() }

private fun report(check: String, ok: Boolean, detail: Any? = null) {
    println(JSONObject().put("check", check).put("ok", ok).put("detail", detail ?: JSONObject.NULL))

    if (!ok) {
        exitProcess(1)
    }
}

private fun run(args: Array<String>) {
    val (dialer, deviceId, key) = when (args[0]) {
        "direct" -> Triple(DirectDialer(args[1], args[2].toInt()), args[3], hex(args[4]))
        "relay" -> {
            val endpoint = RelayEndpoint(
                host = args[1],
                port = args[2].toInt(),
                serverName = args[3],
                caPem = File(args[4]).readText(Charsets.US_ASCII),
            )
            Triple(RelayDialer(endpoint, hex(args[6])), args[5], hex(args[6]))
        }
        else -> error("unknown mode ${args[0]}")
    }

    LinkClient(deviceId, key, dialer, timeoutMs = 15_000).use { client ->
        client.connect()
        val summary = client.tlsSummary()
        report("tls13_psk_handshake", summary.startsWith("TLSv1.3"), summary)

        val ping = client.call("ping")
        report("ping", ping.ok, ping.resultObject().optString("scope"))

        // The harness's stand-in assistant echoes the question, so an exact
        // match proves Persian text survived both directions intact.
        val question = "پایتخت فرانسه کجاست"
        val asked = client.callWithRetry("ask", JSONObject().put("question", question))
        report("ask_persian_round_trip", asked.ok && asked.resultObject().optString("answer").endsWith(question), asked.resultObject().optString("answer"))
        report("ask_no_actions", asked.resultObject().getJSONArray("performed_actions").length() == 0, null)

        // The harness cuts the connection right after running this command
        // once. The retry must be answered from the PC's ledger, not run again;
        // the harness counts executions and checks there was exactly one.
        val dropOnce = "یک پرسش ساده برای آزمودن تکرار"
        val retried = client.callWithRetry("ask", JSONObject().put("question", dropOnce), sleep = { Thread.sleep(300) })
        report("retry_after_drop_answered", retried.ok && retried.resultObject().optString("answer").endsWith(dropOnce), retried.resultObject().optString("answer"))

        val refused = client.call("ask", JSONObject().put("question", "فایل گزارش را پاک کن"))
        report("ask_action_refused", refused.resultObject().optBoolean("refused_action"), refused.resultObject().optString("answer"))

        val outOfScope = client.call("write_file", JSONObject())
        report(
            "ceiling_or_not_implemented",
            !outOfScope.ok && outOfScope.code in setOf("out_of_scope", "not_implemented", "needs_approval"),
            outOfScope.code,
        )

        val pcm = ByteArray(16_000 * 2 * 3 / 2)  // 1.5 s of a 440 Hz tone
        for (n in 0 until pcm.size / 2) {
            val sample = (8000 * sin(2 * PI * 440 * n / 16_000)).toInt()
            pcm[2 * n] = (sample and 0xff).toByte()
            pcm[2 * n + 1] = ((sample shr 8) and 0xff).toByte()
        }

        val voice = client.askByVoice(pcm, language = "fa", replyAudio = true)
        report("voice_transcript", voice.transcript == "سلام قرونوس", voice.transcript)
        report("voice_answer", voice.answer.isNotEmpty(), voice.answer)
        report("voice_reply_audio", (voice.replyWav?.size ?: 0) > 0, voice.replyWav?.size)
    }
}
