package app.qronos.remote

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import app.qronos.link.PairingUri
import app.qronos.link.RelayEndpoint
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** What the phone remembers about its PC. Nothing in here is secret. */
data class PairedPc(
    val deviceId: String,
    val host: String,
    val port: Int,
    val relay: RelayEndpoint?,
)

/**
 * The pairing key, kept the way Android keeps secrets.
 *
 * The 32-byte pairing key is encrypted with an AES-256-GCM key that lives in
 * the Android Keystore — generated there, never exported, and on Android 9+
 * usable only while the phone is unlocked. Only the ciphertext reaches app
 * storage, which is private to the app and excluded from every backup (see the
 * manifest). The plain key exists in memory only for the moment a handshake
 * needs it, and is wiped afterwards.
 *
 * The limit, stated in QRONOS_DEVICE_LINK.md §4.11: the pairing key is
 * symmetric, so it cannot itself be a non-extractable Keystore key — the TLS
 * library needs its bytes. A phone that is unlocked and compromised can leak
 * it; the answer is revocation at the PC.
 */
class KeyVault(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun pairedPc(): PairedPc? {
        val deviceId = prefs.getString(DEVICE_ID, null) ?: return null
        val host = prefs.getString(HOST, null) ?: return null
        val relayHost = prefs.getString(RELAY_HOST, null)

        return PairedPc(
            deviceId = deviceId,
            host = host,
            port = prefs.getInt(PORT, 0),
            relay = relayHost?.let {
                RelayEndpoint(
                    host = it,
                    port = prefs.getInt(RELAY_PORT, 443),
                    serverName = prefs.getString(RELAY_NAME, null),
                    caPem = prefs.getString(RELAY_CA, null),
                )
            },
        )
    }

    /** Store a freshly scanned pairing: the key is copied out, sealed, and the copy wiped. */
    fun save(pairing: PairingUri) {
        val key = pairing.key()

        try {
            val (iv, sealed) = seal(key)

            prefs.edit()
                .clear()
                .putString(DEVICE_ID, pairing.deviceId)
                .putString(HOST, pairing.host)
                .putInt(PORT, pairing.port)
                .putString(KEY_IV, Base64.encodeToString(iv, Base64.NO_WRAP))
                .putString(KEY_SEALED, Base64.encodeToString(sealed, Base64.NO_WRAP))
                .apply()
        } finally {
            key.fill(0)
        }
    }

    /** Remember where the relay is, as the PC told us over the local link. */
    fun saveRelay(relay: RelayEndpoint?) {
        val edit = prefs.edit()

        if (relay == null) {
            edit.remove(RELAY_HOST).remove(RELAY_PORT).remove(RELAY_NAME).remove(RELAY_CA)
        } else {
            edit.putString(RELAY_HOST, relay.host)
                .putInt(RELAY_PORT, relay.port)
                .putString(RELAY_NAME, relay.serverName)
                .putString(RELAY_CA, relay.caPem)
        }

        edit.apply()
    }

    /** Run [block] with the plain key, then wipe it. */
    fun <T> withKey(block: (ByteArray) -> T): T {
        val iv = Base64.decode(prefs.getString(KEY_IV, null) ?: throw IllegalStateException("not paired"), Base64.NO_WRAP)
        val sealed = Base64.decode(prefs.getString(KEY_SEALED, null) ?: throw IllegalStateException("not paired"), Base64.NO_WRAP)

        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, wrappingKey(), GCMParameterSpec(128, iv))
        val key = cipher.doFinal(sealed)

        try {
            return block(key)
        } finally {
            key.fill(0)
        }
    }

    /** Forget the PC on this phone. Revocation itself happens at the PC. */
    fun forget() {
        prefs.edit().clear().apply()

        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }

        if (store.containsAlias(ALIAS)) {
            store.deleteEntry(ALIAS)
        }
    }

    private fun seal(plain: ByteArray): Pair<ByteArray, ByteArray> {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, wrappingKey())

        return cipher.iv to cipher.doFinal(plain)
    }

    private fun wrappingKey(): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }

        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .setRandomizedEncryptionRequired(true)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            spec.setUnlockedDeviceRequired(true)
        }

        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).run {
            init(spec.build())
            generateKey()
        }
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "qronos-link-pairing-key-wrap"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val PREFS = "qronos_link"
        const val DEVICE_ID = "device_id"
        const val HOST = "host"
        const val PORT = "port"
        const val KEY_IV = "key_iv"
        const val KEY_SEALED = "key_sealed"
        const val RELAY_HOST = "relay_host"
        const val RELAY_PORT = "relay_port"
        const val RELAY_NAME = "relay_server_name"
        const val RELAY_CA = "relay_ca_pem"
    }
}
