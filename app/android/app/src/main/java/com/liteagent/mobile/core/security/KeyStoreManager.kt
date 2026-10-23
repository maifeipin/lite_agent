package com.liteagent.mobile.core.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.nio.ByteBuffer
import java.security.Key
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.spec.GCMParameterSpec

interface CryptoEngine {
    @Throws(SecurityException::class)
    fun encrypt(plainText: String): String

    @Throws(SecurityException::class)
    fun decrypt(cipherTextBase64: String): String
}

class AndroidKeyStoreCryptoEngine : CryptoEngine {
    companion object {
        private const val ANDROID_KEYSTORE_PROVIDER = "AndroidKeyStore"
        private const val KEY_ALIAS = "LiteAgentSecureKey_v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_IV_LENGTH = 12
        private const val GCM_TAG_LENGTH_BITS = 128
    }

    private fun getOrCreateKey(): Key {
        return try {
            val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE_PROVIDER).apply { load(null) }
            if (!keyStore.containsAlias(KEY_ALIAS)) {
                val keyGenerator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE_PROVIDER)
                val builder = KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .setRandomizedEncryptionRequired(true)

                keyGenerator.init(builder.build())
                keyGenerator.generateKey()
            }
            keyStore.getKey(KEY_ALIAS, null) ?: throw SecurityException("未能从 AndroidKeyStore 获取秘钥")
        } catch (e: Exception) {
            throw SecurityException("AndroidKeyStore 秘钥管理异常: ${e.localizedMessage ?: e.message}", e)
        }
    }

    override fun encrypt(plainText: String): String {
        if (plainText.isEmpty()) return ""
        try {
            val key = getOrCreateKey()
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, key)
            val iv = cipher.iv ?: throw SecurityException("初始化向量 IV 生成失败")
            val encryptedBytes = cipher.doFinal(plainText.toByteArray(Charsets.UTF_8))

            val buffer = ByteBuffer.allocate(iv.size + encryptedBytes.size)
            buffer.put(iv)
            buffer.put(encryptedBytes)
            return Base64.encodeToString(buffer.array(), Base64.NO_WRAP)
        } catch (e: SecurityException) {
            throw e
        } catch (e: Exception) {
            throw SecurityException("凭证硬件加密失败，终止保存操作: ${e.localizedMessage ?: e.message}", e)
        }
    }

    override fun decrypt(cipherTextBase64: String): String {
        if (cipherTextBase64.isEmpty()) return ""
        try {
            val combined = Base64.decode(cipherTextBase64, Base64.NO_WRAP)
            if (combined.size < GCM_IV_LENGTH) {
                throw SecurityException("密文长度小于 GCM IV 长度，数据可能被篡改或损坏")
            }

            val buffer = ByteBuffer.wrap(combined)
            val iv = ByteArray(GCM_IV_LENGTH)
            buffer.get(iv)

            val cipherBytes = ByteArray(buffer.remaining())
            buffer.get(cipherBytes)

            val key = getOrCreateKey()
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val spec = GCMParameterSpec(GCM_TAG_LENGTH_BITS, iv)
            cipher.init(Cipher.DECRYPT_MODE, key, spec)

            val decrypted = cipher.doFinal(cipherBytes)
            return String(decrypted, Charsets.UTF_8)
        } catch (e: SecurityException) {
            throw e
        } catch (e: Exception) {
            throw SecurityException("凭证硬件解密失败: ${e.localizedMessage ?: e.message}", e)
        }
    }
}
