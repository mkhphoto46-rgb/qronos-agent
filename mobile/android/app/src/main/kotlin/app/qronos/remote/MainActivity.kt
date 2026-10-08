package app.qronos.remote

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.MotionEvent
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import app.qronos.link.LinkRefusedException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * The remote control: type a question or hold to speak, read the answer.
 *
 * Everything the phone shows comes from the PC in the moment; nothing is kept
 * once the screen moves on. When an answer comes back, the screen says which
 * way it came (home network or relay) and, as a fact rather than the model's
 * word, that nothing was done on the computer.
 */
class MainActivity : Activity() {

    private lateinit var link: QronosLink
    private lateinit var status: TextView
    private lateinit var question: EditText
    private lateinit var send: Button
    private lateinit var talk: Button
    private lateinit var speakReply: CheckBox
    private lateinit var transcript: TextView
    private lateinit var answer: TextView
    private lateinit var note: TextView

    private val worker: ExecutorService = Executors.newSingleThreadExecutor()
    private val recorder = PushToTalk()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        link = QronosLink(applicationContext)
        status = findViewById(R.id.status)
        question = findViewById(R.id.question)
        send = findViewById(R.id.send)
        talk = findViewById(R.id.talk)
        speakReply = findViewById(R.id.speak_reply)
        transcript = findViewById(R.id.transcript)
        answer = findViewById(R.id.answer)
        note = findViewById(R.id.note)

        send.setOnClickListener { ask() }
        bindPushToTalk()

        findViewById<Button>(R.id.forget).setOnClickListener {
            worker.execute {
                link.forget()
                runOnUiThread {
                    status.setText(R.string.forget_done)
                    refresh()
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onDestroy() {
        worker.shutdownNow()
        link.close()
        super.onDestroy()
    }

    private fun refresh() {
        val paired = link.paired() != null

        send.isEnabled = paired
        talk.isEnabled = paired
        question.isEnabled = paired

        if (!paired) {
            status.setText(R.string.status_unpaired)
        } else if (status.text.isNullOrEmpty()) {
            status.setText(R.string.status_ready)
        }
    }

    private fun ask() {
        val text = question.text.toString().trim()

        if (text.isEmpty()) return

        exchange { link.ask(text) }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun bindPushToTalk() {
        talk.setOnTouchListener { _, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_MIC)
                    } else {
                        recorder.start()
                        talk.setText(R.string.ptt_recording)
                    }
                    true
                }

                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    if (recorder.isRecording) {
                        talk.setText(R.string.ptt_idle)
                        val pcm = recorder.stop()
                        val wantsAudio = speakReply.isChecked

                        if (pcm.size >= PushToTalk.SAMPLE_RATE / 5) {
                            exchange { link.askByVoice(pcm, wantsAudio) }
                        }
                    }
                    true
                }

                else -> false
            }
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, results)

        if (requestCode == REQUEST_MIC && results.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
            status.setText(R.string.mic_denied)
        }
    }

    /** Run one exchange off the main thread and show what came back. */
    private fun exchange(call: () -> Answer) {
        send.isEnabled = false
        talk.isEnabled = false
        status.setText(R.string.status_working)
        transcript.text = ""
        answer.text = ""
        note.text = ""

        worker.execute {
            val outcome = runCatching(call)

            runOnUiThread {
                send.isEnabled = true
                talk.isEnabled = true
                outcome.onSuccess { show(it) }.onFailure { showError(it) }
            }

            outcome.getOrNull()?.replyWav?.let { ReplyPlayer.play(it) }
        }
    }

    private fun show(result: Answer) {
        status.setText(if (result.route == Route.RELAY) R.string.status_relay else R.string.status_local)

        result.transcript?.let {
            transcript.text = if (it.isEmpty()) getString(R.string.heard_nothing) else getString(R.string.heard, it)
        }

        answer.text = result.text
        note.text = if (result.performedNothing) getString(R.string.performed_nothing) else ""
        question.setText("")
    }

    private fun showError(error: Throwable) {
        status.text = when (error) {
            is LinkRefusedException -> when (error.code) {
                "busy", "rate_limited", "command_in_progress" -> getString(R.string.error_busy)
                else -> getString(R.string.error_refused, error.message ?: error.code)
            }
            else -> if (error.message?.contains("refused this device") == true) {
                getString(R.string.error_refused_device)
            } else {
                getString(R.string.status_unreachable)
            }
        }
    }

    companion object {
        private const val REQUEST_MIC = 7

        fun open(from: Activity) {
            from.startActivity(Intent(from, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP))
        }
    }
}
