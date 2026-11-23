package com.liteagent.mobile

import com.liteagent.mobile.core.security.CryptoEngine
import java.nio.ByteBuffer
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

class FakeCryptoEngine : CryptoEngine {
    private val testKey = SecretKeySpec("UnitTestKeyMustBe256BitsLong123!".toByteArray(Charsets.UTF_8), "AES")

    override fun encrypt(plainText: String): String {
        if (plainText.isEmpty()) return ""
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, testKey)
        val iv = cipher.iv
        val encrypted = cipher.doFinal(plainText.toByteArray(Charsets.UTF_8))
        val buf = ByteBuffer.allocate(iv.size + encrypted.size)
        buf.put(iv)
        buf.put(encrypted)
        return Base64.getEncoder().encodeToString(buf.array())
    }

    override fun decrypt(cipherTextBase64: String): String {
        if (cipherTextBase64.isEmpty()) return ""
        val combined = Base64.getDecoder().decode(cipherTextBase64)
        val buf = ByteBuffer.wrap(combined)
        val iv = ByteArray(12)
        buf.get(iv)
        val cipherBytes = ByteArray(buf.remaining())
        buf.get(cipherBytes)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        val spec = GCMParameterSpec(128, iv)
        cipher.init(Cipher.DECRYPT_MODE, testKey, spec)
        return String(cipher.doFinal(cipherBytes), Charsets.UTF_8)
    }
}
