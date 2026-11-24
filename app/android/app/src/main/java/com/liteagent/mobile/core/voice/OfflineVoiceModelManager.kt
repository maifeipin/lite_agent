package com.liteagent.mobile.core.voice

import android.content.Context
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.compressors.bzip2.BZip2CompressorInputStream
import java.io.BufferedInputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.security.DigestInputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

data class RequiredModelFile(
    val relativePath: String,
    val minBytes: Long
)

data class VerifiedModelManifest(
    val modelId: String,
    val archiveSha256: String,
    val files: Map<String, Long>
)

private val voiceManifestGson = Gson()
internal const val VERIFIED_MANIFEST_FILE = ".verified_manifest.json"

enum class OfflineVoiceModelId(
    val id: String,
    val displayName: String,
    val subtitle: String,
    val archiveDirName: String,
    val downloadUrl: String,
    val expectedArchiveSha256: String,
    val approxDownloadMb: Int,
    val approxDiskMb: Int,
    val requiredFiles: List<RequiredModelFile>
) {
    QWEN3_ASR_06B(
        id = "qwen3_asr_06b",
        displayName = "Qwen3-ASR 0.6B int8（首测推荐）",
        subtitle = "2026 新版 · 30 语种/方言 · 中英技术词准确率强",
        archiveDirName = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
        downloadUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        expectedArchiveSha256 = "393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96",
        approxDownloadMb = 838,
        approxDiskMb = 940,
        requiredFiles = listOf(
            RequiredModelFile("conv_frontend.onnx", 1024L * 100L),
            RequiredModelFile("encoder.int8.onnx", 1024L * 1024L),
            RequiredModelFile("decoder.int8.onnx", 1024L * 1024L),
            RequiredModelFile("tokenizer/vocab.json", 1024L),
            RequiredModelFile("tokenizer/merges.txt", 1024L),
            RequiredModelFile("tokenizer/tokenizer_config.json", 32L),
        )
    ),
    FUNASR_NANO(
        id = "funasr_nano",
        displayName = "FunASR-Nano int8（对比评测）",
        subtitle = "2025 年底 · 中英日与多方言 · 语义标点好",
        archiveDirName = "sherpa-onnx-funasr-nano-int8-2025-12-30",
        downloadUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
        expectedArchiveSha256 = "eb43d7ccc2e86b243f6a03b7df361033dda66db9523d1a92bf6aca2b50c9476b",
        approxDownloadMb = 803,
        approxDiskMb = 950,
        requiredFiles = listOf(
            RequiredModelFile("encoder_adaptor.int8.onnx", 1024L * 1024L),
            RequiredModelFile("llm.int8.onnx", 1024L * 1024L),
            RequiredModelFile("embedding.int8.onnx", 1024L * 1024L),
            RequiredModelFile("Qwen3-0.6B/vocab.json", 1024L),
            RequiredModelFile("Qwen3-0.6B/merges.txt", 1024L),
            RequiredModelFile("Qwen3-0.6B/tokenizer_config.json", 32L),
        )
    ),
    SENSE_VOICE_SMALL(
        id = "sense_voice_small",
        displayName = "SenseVoiceSmall int8（极速轻量）",
        subtitle = "158 MB · 松手 0.2s 秒出 · 省内存不杀后台",
        archiveDirName = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        downloadUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        expectedArchiveSha256 = "7305f7905bfcf77fa0b39388a313f3da35c68d971661a65475b56fb2162c8e63",
        approxDownloadMb = 158,
        approxDiskMb = 228,
        requiredFiles = listOf(
            RequiredModelFile("model.int8.onnx", 1024L * 1024L),
            RequiredModelFile("tokens.txt", 1024L),
        )
    );

    val archiveFileName: String
        get() = "$archiveDirName.tar.bz2"

    companion object {
        fun fromId(raw: String?): OfflineVoiceModelId {
            return values().firstOrNull { it.id == raw } ?: QWEN3_ASR_06B
        }
    }
}

data class VoiceDownloadProgress(
    val modelId: OfflineVoiceModelId,
    val downloadedBytes: Long = 0L,
    val totalBytes: Long = -1L,
    val phase: String = "downloading" // downloading | extracting | done | error
) {
    val percent: Int
        get() = if (totalBytes > 0L) ((downloadedBytes * 100L) / totalBytes).toInt().coerceIn(0, 100) else -1
}

internal fun buildCandidateDownloadUrls(spec: OfflineVoiceModelId, mirrorBaseUrl: String?): List<String> {
    val urls = ArrayList<String>(2)
    val cleanBase = mirrorBaseUrl?.trim()?.trimEnd('/')
    if (!cleanBase.isNullOrBlank() && (cleanBase.startsWith("https://") || cleanBase.startsWith("http://"))) {
        urls.add("$cleanBase/voice-models/${spec.archiveFileName}")
    }
    urls.add(spec.downloadUrl)
    return urls
}

internal fun hasAllRequiredFiles(dir: File, spec: OfflineVoiceModelId): Boolean {
    if (!dir.exists() || !dir.isDirectory) return false
    return spec.requiredFiles.all { req ->
        val f = File(dir, req.relativePath)
        f.isFile && f.length() >= req.minBytes
    }
}

internal fun writeVerifiedManifest(dir: File, spec: OfflineVoiceModelId, archiveSha256: String) {
    val sizes = spec.requiredFiles.associate { req ->
        req.relativePath to File(dir, req.relativePath).length()
    }
    val manifest = VerifiedModelManifest(
        modelId = spec.id,
        archiveSha256 = archiveSha256.lowercase(),
        files = sizes
    )
    File(dir, VERIFIED_MANIFEST_FILE).writeText(voiceManifestGson.toJson(manifest), Charsets.UTF_8)
}

internal fun isModelDirectoryComplete(dir: File, spec: OfflineVoiceModelId): Boolean {
    if (!hasAllRequiredFiles(dir, spec)) return false
    val manifestFile = File(dir, VERIFIED_MANIFEST_FILE)
    if (!manifestFile.isFile) return false
    return try {
        val parsed = voiceManifestGson.fromJson(manifestFile.readText(Charsets.UTF_8), VerifiedModelManifest::class.java)
            ?: return false
        if (parsed.modelId != spec.id) return false
        if (!parsed.archiveSha256.equals(spec.expectedArchiveSha256, ignoreCase = true)) return false
        spec.requiredFiles.all { req ->
            val recordedSize = parsed.files[req.relativePath] ?: return@all false
            val f = File(dir, req.relativePath)
            f.isFile && f.length() >= req.minBytes && f.length() == recordedSize
        }
    } catch (_: Exception) {
        false
    }
}

internal fun resolveSafeTarEntryFile(destRoot: File, entryName: String): File? {
    if (entryName.isBlank()) return null
    val normalized = entryName.replace('\\', '/').trimStart('/')
    if (normalized.isBlank() || normalized.startsWith("..") || normalized.contains("/../")) {
        return null
    }
    val destCanonical = destRoot.canonicalFile
    val target = File(destCanonical, normalized).canonicalFile
    val rootPrefix = destCanonical.path.let { if (it.endsWith(File.separator)) it else it + File.separator }
    if (target.path != destCanonical.path && !target.path.startsWith(rootPrefix)) {
        return null
    }
    return target
}

private fun ByteArray.toHexLower(): String = joinToString("") { "%02x".format(it) }

private class NonClosingInputStream(input: InputStream) : FilterInputStream(input) {
    override fun close() {
        // 交由外层调用方在排空尾部字节后统一关闭底层流
    }
}

internal fun extractTarBz2AndComputeSha256(rawStream: InputStream, stagingRoot: File): String {
    val sha256Digest = MessageDigest.getInstance("SHA-256")
    val digestStream = DigestInputStream(rawStream, sha256Digest)
    BufferedInputStream(digestStream, 64 * 1024).use { bufIn ->
        BZip2CompressorInputStream(NonClosingInputStream(bufIn), true).use { bzIn ->
            TarArchiveInputStream(bzIn).use { tarIn ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val entry = tarIn.nextEntry ?: break
                    if (entry.isSymbolicLink || entry.isLink) continue
                    val outFile = resolveSafeTarEntryFile(stagingRoot, entry.name) ?: continue
                    if (entry.isDirectory) {
                        outFile.mkdirs()
                    } else {
                        outFile.parentFile?.mkdirs()
                        FileOutputStream(outFile).use { fos ->
                            while (true) {
                                val read = tarIn.read(buffer)
                                if (read <= 0) break
                                fos.write(buffer, 0, read)
                            }
                        }
                    }
                }
            }
        }
        val drainBuf = ByteArray(4096)
        while (bufIn.read(drainBuf) > 0) {
            // draining
        }
    }
    return sha256Digest.digest().toHexLower()
}

class OfflineVoiceModelManager(private val context: Context) {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    private val modelsRootDir: File = File(context.filesDir, "voice_models").apply { mkdirs() }
    private val activeCallRef = AtomicReference<Call?>(null)
    private val cancelledRef = AtomicBoolean(false)

    private val downloadClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(2, TimeUnit.MINUTES)
        .retryOnConnectionFailure(true)
        .followRedirects(true)
        .followSslRedirects(true)
        .build()

    private val _selectedModel = MutableStateFlow(
        OfflineVoiceModelId.fromId(prefs.getString(KEY_SELECTED_MODEL, OfflineVoiceModelId.QWEN3_ASR_06B.id))
    )
    val selectedModel: StateFlow<OfflineVoiceModelId> = _selectedModel.asStateFlow()

    private val _downloadProgress = MutableStateFlow<VoiceDownloadProgress?>(null)
    val downloadProgress: StateFlow<VoiceDownloadProgress?> = _downloadProgress.asStateFlow()

    private val _installedIds = MutableStateFlow(scanInstalledModels())
    val installedIds: StateFlow<Set<OfflineVoiceModelId>> = _installedIds.asStateFlow()

    companion object {
        private const val PREFS_NAME = "pc_cli_offline_voice_prefs"
        private const val KEY_SELECTED_MODEL = "selected_voice_model_id"
        private const val MAX_RESUME_ATTEMPTS = 6
    }

    fun getModelDir(spec: OfflineVoiceModelId): File {
        return File(modelsRootDir, spec.archiveDirName)
    }

    private fun getPartFile(spec: OfflineVoiceModelId): File {
        return File(modelsRootDir, ".part_${spec.id}.tar.bz2")
    }

    fun isInstalled(spec: OfflineVoiceModelId): Boolean {
        return isModelDirectoryComplete(getModelDir(spec), spec)
    }

    fun scanInstalledModels(): Set<OfflineVoiceModelId> {
        return OfflineVoiceModelId.values().filter { isInstalled(it) }.toSet()
    }

    fun refreshInstalled() {
        _installedIds.value = scanInstalledModels()
    }

    fun selectModel(spec: OfflineVoiceModelId) {
        prefs.edit().putString(KEY_SELECTED_MODEL, spec.id).apply()
        _selectedModel.value = spec
    }

    fun getInstalledSizeMb(spec: OfflineVoiceModelId): Long {
        val dir = getModelDir(spec)
        if (!dir.exists()) return 0L
        var bytes = 0L
        dir.walkTopDown().forEach { f ->
            if (f.isFile) bytes += f.length()
        }
        return bytes / (1024L * 1024L)
    }

    fun cancelDownload() {
        cancelledRef.set(true)
        activeCallRef.getAndSet(null)?.cancel()
    }

    suspend fun deleteModel(spec: OfflineVoiceModelId): Boolean = withContext(Dispatchers.IO) {
        val dir = getModelDir(spec)
        val part = getPartFile(spec)
        if (part.exists()) part.delete()
        val ok = if (dir.exists()) dir.deleteRecursively() else true
        refreshInstalled()
        ok
    }

    private suspend fun downloadWithRangeResume(spec: OfflineVoiceModelId, url: String, partFile: File) {
        var lastError: Exception? = null
        for (attempt in 1..MAX_RESUME_ATTEMPTS) {
            if (cancelledRef.get()) throw IOException("已取消下载")
            val existing = if (partFile.exists()) partFile.length() else 0L
            val reqBuilder = Request.Builder()
                .url(url)
                .header("User-Agent", "LiteAgent-Android/1.5")
            if (existing > 0L) {
                reqBuilder.header("Range", "bytes=$existing-")
            }
            val call = downloadClient.newCall(reqBuilder.build())
            activeCallRef.set(call)
            try {
                call.execute().use { response ->
                    if (response.code == 416) {
                        // 已下载长度达到或超过远端大小：若无法确认则重置分片重试
                        if (existing > 0L) {
                            return
                        }
                    }
                    if (!response.isSuccessful && response.code != 206) {
                        throw IOException("HTTP ${response.code}")
                    }
                    val body = response.body ?: throw IOException("下载响应体为空")
                    val appendMode = (response.code == 206 && existing > 0L)
                    val baseOffset = if (appendMode) existing else 0L
                    val contentLen = body.contentLength()
                    val totalBytes = if (contentLen > 0L) baseOffset + contentLen else -1L
                    var downloaded = baseOffset
                    var lastEmitMs = 0L

                    _downloadProgress.value = VoiceDownloadProgress(spec, downloaded, totalBytes, "downloading")
                    FileOutputStream(partFile, appendMode).use { fos ->
                        val buf = ByteArray(64 * 1024)
                        val input = body.byteStream()
                        while (true) {
                            if (cancelledRef.get()) throw IOException("已取消下载")
                            val read = input.read(buf)
                            if (read <= 0) break
                            fos.write(buf, 0, read)
                            downloaded += read.toLong()
                            val now = System.currentTimeMillis()
                            if (now - lastEmitMs >= 250L || (totalBytes > 0 && downloaded >= totalBytes)) {
                                lastEmitMs = now
                                _downloadProgress.value = VoiceDownloadProgress(spec, downloaded, totalBytes, "downloading")
                            }
                        }
                        fos.flush()
                    }
                    if (totalBytes > 0L && downloaded < totalBytes) {
                        throw IOException("连接提前中断 ($downloaded / $totalBytes)")
                    }
                    return
                }
            } catch (e: Exception) {
                lastError = e
                if (cancelledRef.get()) throw e
                // 404/403 直接放弃当前源，交由调用方切换备用源
                if (e.message?.contains("HTTP 404") == true || e.message?.contains("HTTP 403") == true) {
                    throw e
                }
                if (attempt < MAX_RESUME_ATTEMPTS) {
                    delay(1500L * attempt)
                }
            }
        }
        throw lastError ?: IOException("下载失败，请稍后重试")
    }

    /**
     * 优先尝试自建服务器镜像源，失败自动回退 GitHub；全程支持 HTTP Range 断点续传与多轮自动重试；
     * 下载完成后校验官方 SHA-256 并解压，校验通过后立刻删除临时分片文件。
     */
    suspend fun downloadAndExtractModel(
        spec: OfflineVoiceModelId,
        mirrorBaseUrl: String? = null
    ): Result<File> = withContext(Dispatchers.IO) {
        if (isInstalled(spec)) {
            refreshInstalled()
            return@withContext Result.success(getModelDir(spec))
        }
        if (_downloadProgress.value != null) {
            return@withContext Result.failure(IllegalStateException("已有模型正在下载中，请稍候"))
        }

        cancelledRef.set(false)
        val partFile = getPartFile(spec)
        val stagingRoot = File(modelsRootDir, ".staging_${spec.id}_${System.currentTimeMillis()}")
        stagingRoot.mkdirs()
        _downloadProgress.value = VoiceDownloadProgress(spec, if (partFile.exists()) partFile.length() else 0L, -1L, "downloading")

        try {
            val urls = buildCandidateDownloadUrls(spec, mirrorBaseUrl)
            var downloadedOk = false
            var lastErr: Exception? = null
            for (u in urls) {
                try {
                    downloadWithRangeResume(spec, u, partFile)
                    downloadedOk = true
                    break
                } catch (e: Exception) {
                    lastErr = e
                    if (cancelledRef.get()) throw e
                }
            }
            if (!downloadedOk) {
                throw lastErr ?: IOException("所有下载源均失败，请检查网络后重试（已保留断点）")
            }

            _downloadProgress.value = VoiceDownloadProgress(spec, 100L, 100L, "extracting")
            val actualSha256 = FileInputStream(partFile).use { fis ->
                extractTarBz2AndComputeSha256(fis, stagingRoot)
            }
            if (!actualSha256.equals(spec.expectedArchiveSha256, ignoreCase = true)) {
                // 分片已损坏，删除分片以便下次干净重下
                partFile.delete()
                throw IllegalStateException("模型归档 SHA-256 校验失败，已清理损坏分片，请重试")
            }

            val extractedSubDir = File(stagingRoot, spec.archiveDirName)
            val sourceDir = when {
                hasAllRequiredFiles(extractedSubDir, spec) -> extractedSubDir
                hasAllRequiredFiles(stagingRoot, spec) -> stagingRoot
                else -> {
                    partFile.delete()
                    throw IllegalStateException("解压后的模型权重或词表文件不完整，请重试下载")
                }
            }
            writeVerifiedManifest(sourceDir, spec, actualSha256)

            val finalDir = getModelDir(spec)
            if (finalDir.exists()) {
                finalDir.deleteRecursively()
            }
            if (!sourceDir.renameTo(finalDir)) {
                sourceDir.copyRecursively(finalDir, overwrite = true)
            }
            if (!isModelDirectoryComplete(finalDir, spec)) {
                finalDir.deleteRecursively()
                throw IllegalStateException("模型目录完整性校验失败")
            }

            partFile.delete()
            selectModel(spec)
            refreshInstalled()
            Result.success(finalDir)
        } catch (e: Exception) {
            Result.failure(e)
        } finally {
            activeCallRef.set(null)
            _downloadProgress.value = null
            stagingRoot.deleteRecursively()
        }
    }
}
