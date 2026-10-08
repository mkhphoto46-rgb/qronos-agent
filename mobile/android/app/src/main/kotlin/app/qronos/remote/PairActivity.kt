package app.qronos.remote

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import app.qronos.link.PairingUri
import app.qronos.link.PairingUriException
import java.util.concurrent.Executors

/**
 * Pairing: the PC's QR code arrives here as a `qronos://pair` link.
 *
 * The phone's camera app reads the code and opens it here, so no scanning
 * library is needed. Nothing happens without the user's confirmation, because
 * any app could open such a link; the screen names the computer's address and
 * says to continue only if the code was just read from the user's own PC.
 * Pairing then completes on the local network, inside the PC's two-minute
 * window — the PC refuses it from anywhere else, relay included.
 */
class PairActivity : Activity() {

    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var explain: TextView
    private lateinit var code: EditText
    private lateinit var result: TextView
    private lateinit var pair: Button
    private var scanned: PairingUri? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_pair)

        explain = findViewById(R.id.explain)
        code = findViewById(R.id.code)
        result = findViewById(R.id.result)
        pair = findViewById(R.id.pair)

        show(intent)
        pair.setOnClickListener { confirm() }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        show(intent)
    }

    override fun onDestroy() {
        worker.shutdownNow()
        super.onDestroy()
    }

    /**
     * A scanned link is parsed straight from the intent and kept only in
     * memory. It is never put in the text field: it carries the pairing key,
     * which has no business on screen or in saved instance state.
     */
    private fun show(intent: Intent?) {
        val uri = intent?.data?.toString() ?: return

        scanned = describe(uri)
    }

    private fun describe(text: String): PairingUri? {
        val parsed = try {
            PairingUri.parse(text)
        } catch (error: PairingUriException) {
            explain.text = if (text.isBlank()) "" else getString(R.string.pair_invalid)
            return null
        }

        explain.text = getString(R.string.pair_explain, "${parsed.host}:${parsed.port}")

        return parsed
    }

    private fun confirm() {
        val typed = code.text.toString()
        val parsed = (if (typed.isNotBlank()) describe(typed) else scanned) ?: return

        if (parsed.expiresAt * 1000 < System.currentTimeMillis()) {
            result.setText(R.string.pair_expired)
            return
        }

        pair.isEnabled = false
        result.setText(R.string.pair_working)

        worker.execute {
            val link = QronosLink(applicationContext)
            val outcome = runCatching {
                KeyVault(applicationContext).save(parsed)
                link.completePairing()
            }

            if (outcome.isFailure) {
                // A pairing that did not complete leaves nothing behind.
                link.forget()
            }

            link.close()

            runOnUiThread {
                pair.isEnabled = true

                if (outcome.isSuccess) {
                    result.setText(R.string.pair_done)
                    code.setText("")
                    scanned = null
                    MainActivity.open(this)
                    finish()
                } else {
                    result.setText(R.string.pair_failed)
                }
            }
        }
    }
}
