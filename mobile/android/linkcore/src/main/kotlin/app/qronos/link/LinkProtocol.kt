package app.qronos.link

import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer

/**
 * The device link's wire protocol, phone side.
 *
 * The definition is `core/link_protocol.py` in the Qronos repository: a
 * four-byte big-endian length, then UTF-8 JSON. Requests go to the PC;
 * responses and events come back. This file must agree with it byte for byte,
 * and `mobile/protocol/link-vectors.json` — produced from the Python code —
 * is what the tests here check that against.
 *
 * The declared length is checked before anything is allocated, exactly as on
 * the PC, so a hostile relay or a broken PC cannot make the phone reserve a
 * gigabyte by claiming one is coming.
 */
object LinkProtocol {
    const val LENGTH_PREFIX_BYTES = 4
    const val MAX_FRAME_BYTES = 1 shl 20

    private val COMMAND_ID = Regex("^[0-9a-f]{32}$")

    fun isCommandId(value: String): Boolean = COMMAND_ID.matches(value)

    /** One request as bytes on the wire, length prefix included. */
    fun encodeRequest(id: Long, op: String, params: JSONObject, commandId: String?): ByteArray {
        require(commandId == null || isCommandId(commandId)) { "malformed command id" }

        val json = JSONObject()
        json.put("kind", "request")
        json.put("id", id)
        json.put("op", op)
        json.put("params", params)

        if (commandId != null) {
            json.put("cid", commandId)
        }

        val body = json.toString().toByteArray(Charsets.UTF_8)

        if (body.size > MAX_FRAME_BYTES) {
            throw ProtocolException("oversize", "request of ${body.size} bytes")
        }

        return ByteBuffer.allocate(LENGTH_PREFIX_BYTES + body.size)
            .putInt(body.size)
            .put(body)
            .array()
    }

    /**
     * Read one frame, or `null` at a clean end of stream between frames.
     *
     * Only responses and events are accepted: the PC never sends requests,
     * and a peer that does is refused rather than humoured.
     */
    fun readFrame(input: InputStream): Frame? {
        val prefix = ByteArray(LENGTH_PREFIX_BYTES)
        val got = readFully(input, prefix)

        if (got == 0) {
            return null
        }

        if (got < LENGTH_PREFIX_BYTES) {
            throw ProtocolException("malformed_length", "length prefix was $got bytes")
        }

        // Unsigned, as on the PC: a length above 2^31 is oversize, not negative.
        val length = ByteBuffer.wrap(prefix).int.toLong() and 0xFFFFFFFFL

        if (length == 0L) {
            throw ProtocolException("malformed_length", "zero length")
        }

        if (length > MAX_FRAME_BYTES) {
            throw ProtocolException("oversize", "peer declared $length bytes")
        }

        val body = ByteArray(length.toInt())

        if (readFully(input, body) < body.size) {
            throw ProtocolException("truncated", "expected ${body.size} bytes")
        }

        return parse(body)
    }

    fun parse(body: ByteArray): Frame {
        val json = try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (error: JSONException) {
            throw ProtocolException("malformed_json", error.message ?: "bad json")
        }

        return when (val kind = json.optString("kind", "")) {
            "response" -> {
                if (!json.has("id") || !json.has("ok")) {
                    throw ProtocolException("missing_field", "response without id or ok")
                }

                val ok = try {
                    json.getBoolean("ok")
                } catch (error: JSONException) {
                    throw ProtocolException("bad_field_type", "ok is not a boolean")
                }

                Frame.Response(
                    id = json.getLong("id"),
                    ok = ok,
                    result = if (ok && json.has("result")) json.get("result") else null,
                    error = json.optString("error", ""),
                    code = json.optString("code", ""),
                )
            }

            "event" -> {
                val topic = json.optString("topic", "")

                if (topic.isEmpty()) {
                    throw ProtocolException("missing_field", "event without topic")
                }

                Frame.Event(topic = topic, data = json.optJSONObject("data") ?: JSONObject())
            }

            "request" -> throw ProtocolException("unexpected_frame", "the PC sent a request")
            "" -> throw ProtocolException("missing_field", "kind is missing")
            else -> throw ProtocolException("unknown_kind", kind)
        }
    }

    /** Fill [buffer] or stop at end of stream. Returns how many bytes arrived. */
    fun readFully(input: InputStream, buffer: ByteArray): Int {
        var filled = 0

        while (filled < buffer.size) {
            val count = input.read(buffer, filled, buffer.size - filled)

            if (count < 0) {
                break
            }

            filled += count
        }

        return filled
    }
}

/** A frame the protocol refused, named the same way the PC names it. */
class ProtocolException(val fault: String, detail: String) : IOException("$fault: $detail")

sealed class Frame {
    data class Response(
        val id: Long,
        val ok: Boolean,
        val result: Any?,
        val error: String,
        val code: String,
    ) : Frame() {
        /** The result as an object, which every operation the phone uses returns. */
        fun resultObject(): JSONObject = result as? JSONObject ?: JSONObject()
    }

    data class Event(val topic: String, val data: JSONObject) : Frame()
}
