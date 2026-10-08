package app.qronos.remote

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Plays the PC's spoken answer straight from memory.
 *
 * The answer arrives as a 16-bit PCM WAV. This reads its format and data
 * chunks and hands the samples to an `AudioTrack`, so the reply is never
 * written to a file on the phone.
 */
object ReplyPlayer {

    fun play(wav: ByteArray) {
        val pcm = parse(wav) ?: return

        val track = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(pcm.sampleRate)
                    .setChannelMask(if (pcm.channels == 2) AudioFormat.CHANNEL_OUT_STEREO else AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(maxOf(pcm.sampleRate / 5 * 2 * pcm.channels, 4_096))
            .build()

        try {
            track.play()
            track.write(pcm.samples, 0, pcm.samples.size)
            track.stop()
        } finally {
            track.release()
        }
    }

    private class Pcm(val sampleRate: Int, val channels: Int, val samples: ByteArray)

    /** The format and data chunks of a 16-bit PCM WAV, or null for anything else. */
    private fun parse(wav: ByteArray): Pcm? {
        if (wav.size < 12 || String(wav, 0, 4, Charsets.US_ASCII) != "RIFF" || String(wav, 8, 4, Charsets.US_ASCII) != "WAVE") {
            return null
        }

        val buffer = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN)
        var offset = 12
        var rate = 0
        var channels = 0
        var bits = 0

        while (offset + 8 <= wav.size) {
            val id = String(wav, offset, 4, Charsets.US_ASCII)
            val size = buffer.getInt(offset + 4)
            val body = offset + 8

            if (size < 0 || body + size > wav.size) {
                return null
            }

            if (id == "fmt " && size >= 16) {
                if (buffer.getShort(body).toInt() != 1) return null
                channels = buffer.getShort(body + 2).toInt()
                rate = buffer.getInt(body + 4)
                bits = buffer.getShort(body + 14).toInt()
            } else if (id == "data") {
                if (bits != 16 || rate <= 0 || channels !in 1..2) return null

                return Pcm(rate, channels, wav.copyOfRange(body, body + size))
            }

            offset = body + size + (size and 1)
        }

        return null
    }
}
