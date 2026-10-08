package app.qronos.remote

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.ByteArrayOutputStream

/**
 * Records while the button is held, and only then.
 *
 * 16 kHz mono 16-bit PCM — exactly what the PC's `voice_begin` accepts — into
 * memory, capped at 30 seconds. There is no speech recognition on the phone:
 * the recording goes to the PC, which transcribes it with its own model.
 * Nothing is written to disk.
 */
class PushToTalk {

    @Volatile
    private var recording = false
    private var thread: Thread? = null
    private val audio = ByteArrayOutputStream()

    val isRecording: Boolean get() = recording

    /** Caller must hold RECORD_AUDIO; the activity asks for it first. */
    @SuppressLint("MissingPermission")
    fun start() {
        if (recording) return

        val minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val buffer = maxOf(minimum, SAMPLE_RATE / 5 * 2)
        val recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            buffer,
        )

        audio.reset()
        recording = true

        thread = Thread {
            val chunk = ByteArray(buffer)

            try {
                recorder.startRecording()

                while (recording && audio.size() < MAX_BYTES) {
                    val read = recorder.read(chunk, 0, minOf(chunk.size, MAX_BYTES - audio.size()))

                    if (read > 0) {
                        audio.write(chunk, 0, read - read % 2)
                    }
                }
            } finally {
                recording = false
                recorder.stop()
                recorder.release()
            }
        }.apply {
            name = "qronos-ptt"
            start()
        }
    }

    /** Stop and hand over the recording; empty if nothing was captured. */
    fun stop(): ByteArray {
        recording = false
        thread?.join(1_000)
        thread = null

        val pcm = audio.toByteArray()
        audio.reset()

        return pcm
    }

    companion object {
        const val SAMPLE_RATE = 16_000
        const val MAX_SECONDS = 30
        const val MAX_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
    }
}
