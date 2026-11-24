package com.liteagent.mobile.core.voice

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.OfflineFunAsrNanoModelConfig
import com.k2fsa.sherpa.onnx.OfflineModelConfig
import com.k2fsa.sherpa.onnx.OfflineQwen3AsrModelConfig
import com.k2fsa.sherpa.onnx.OfflineRecognizer
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig
import com.k2fsa.sherpa.onnx.OfflineSenseVoiceModelConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean

data class OfflineVoiceResult(
    val text: String,
    val modelId: OfflineVoiceModelId,
    val audioDurationSec: Float,
    val inferenceSec: Float
) {
    val rtf: Float
        get() = if (audioDurationSec > 0.01f) inferenceSec / audioDurationSec else 0f
}

/**
 * 基于 Sherpa-ONNX v2.0.3 的纯端侧离线语音识别引擎：
 * - 同一套 OfflineRecognizer 底座支持 Qwen3-ASR 0.6B int8 / FunASR-Nano int8 / SenseVoiceSmall int8；
 * - 推理、加载、释放与删除全部共用 recognizerMutex 互斥锁，杜绝切后台/删除与原生推理并发导致的 JNI 崩溃。
 */
class OfflineAsrEngine(
    private val modelManager: OfflineVoiceModelManager
) {
    private val engineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val recognizerMutex = Mutex()
    private val recordingFlag = AtomicBoolean(false)
    private val destroyedFlag = AtomicBoolean(false)

    private var recognizer: OfflineRecognizer? = null
    private var loadedModelId: OfflineVoiceModelId? = null
    private var idleReleaseJob: Job? = null

    private var recordJob: Job? = null
    private var audioRecord: AudioRecord? = null
    private val pcmLock = Any()
    private var pcmChunks = ArrayList<FloatArray>()
    private var totalSamples = 0

    companion object {
        const val SAMPLE_RATE = 16000
        const val MAX_RECORD_SECONDS = 60
        private const val MAX_SAMPLES = SAMPLE_RATE * MAX_RECORD_SECONDS
        private const val IDLE_RELEASE_MS = 4 * 60 * 1000L // 4 分钟无录音自动释放 Native 内存
    }

    val isRecording: Boolean
        get() = recordingFlag.get()

    private fun buildRecognizerConfig(spec: OfflineVoiceModelId, modelDir: File): OfflineRecognizerConfig {
        val dirPath = modelDir.absolutePath
        val threads = Runtime.getRuntime().availableProcessors().coerceIn(2, 4)
        val modelConfig = when (spec) {
            OfflineVoiceModelId.QWEN3_ASR_06B -> OfflineModelConfig(
                qwen3Asr = OfflineQwen3AsrModelConfig(
                    convFrontend = "$dirPath/conv_frontend.onnx",
                    encoder = "$dirPath/encoder.int8.onnx",
                    decoder = "$dirPath/decoder.int8.onnx",
                    tokenizer = "$dirPath/tokenizer",
                ),
                tokens = "",
                numThreads = threads,
                provider = "cpu",
            )
            OfflineVoiceModelId.FUNASR_NANO -> OfflineModelConfig(
                funasrNano = OfflineFunAsrNanoModelConfig(
                    encoderAdaptor = "$dirPath/encoder_adaptor.int8.onnx",
                    llm = "$dirPath/llm.int8.onnx",
                    embedding = "$dirPath/embedding.int8.onnx",
                    tokenizer = "$dirPath/Qwen3-0.6B",
                ),
                tokens = "",
                numThreads = threads,
                provider = "cpu",
            )
            OfflineVoiceModelId.SENSE_VOICE_SMALL -> OfflineModelConfig(
                senseVoice = OfflineSenseVoiceModelConfig(
                    model = "$dirPath/model.int8.onnx",
                    language = "",
                    useInverseTextNormalization = true,
                ),
                tokens = "$dirPath/tokens.txt",
                numThreads = threads,
                provider = "cpu",
            )
        }
        return OfflineRecognizerConfig(
            featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80),
            modelConfig = modelConfig,
        )
    }

    /** 调用方必须已持有 recognizerMutex。 */
    private fun ensureRecognizerLoadedLocked(spec: OfflineVoiceModelId): OfflineRecognizer {
        check(!destroyedFlag.get()) { "离线语音引擎已销毁" }
        val existing = recognizer
        if (existing != null && loadedModelId == spec) {
            return existing
        }
        releaseLocked()
        val dir = modelManager.getModelDir(spec)
        require(modelManager.isInstalled(spec)) {
            "离线语音模型 ${spec.displayName} 尚未下载或校验未通过"
        }
        val cfg = buildRecognizerConfig(spec, dir)
        val created = OfflineRecognizer(assetManager = null, config = cfg)
        recognizer = created
        loadedModelId = spec
        return created
    }

    private fun scheduleIdleRelease() {
        idleReleaseJob?.cancel()
        if (destroyedFlag.get()) return
        idleReleaseJob = engineScope.launch {
            delay(IDLE_RELEASE_MS)
            if (!recordingFlag.get()) {
                releaseModel()
            }
        }
    }

    private fun releaseLocked() {
        try {
            recognizer?.release()
        } catch (_: Exception) {
        }
        recognizer = null
        loadedModelId = null
    }

    suspend fun releaseModel() {
        idleReleaseJob?.cancel()
        idleReleaseJob = null
        recognizerMutex.withLock {
            releaseLocked()
        }
    }

    suspend fun releaseIfMatches(spec: OfflineVoiceModelId) {
        recognizerMutex.withLock {
            if (loadedModelId == spec) {
                idleReleaseJob?.cancel()
                idleReleaseJob = null
                releaseLocked()
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun startRecording(onAutoStop: (() -> Unit)? = null): Result<Unit> {
        if (destroyedFlag.get()) {
            return Result.failure(IllegalStateException("离线语音引擎已销毁"))
        }
        if (recordingFlag.get()) {
            return Result.success(Unit)
        }
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBuf <= 0) {
            return Result.failure(IllegalStateException("麦克风初始化失败 (minBuf=$minBuf)"))
        }
        val recorder = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf * 2, SAMPLE_RATE / 2)
            )
        } catch (e: Exception) {
            return Result.failure(e)
        }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            return Result.failure(IllegalStateException("无法打开麦克风，请检查录音权限"))
        }

        synchronized(pcmLock) {
            pcmChunks = ArrayList()
            totalSamples = 0
        }
        audioRecord = recorder
        recordingFlag.set(true)
        try {
            recorder.startRecording()
        } catch (e: Exception) {
            recordingFlag.set(false)
            recorder.release()
            audioRecord = null
            return Result.failure(e)
        }

        recordJob = engineScope.launch {
            val readBuf = ShortArray(1600) // 100ms @ 16kHz
            var reachedLimit = false
            try {
                while (isActive && recordingFlag.get()) {
                    val read = recorder.read(readBuf, 0, readBuf.size)
                    if (read > 0) {
                        val floats = FloatArray(read) { idx -> readBuf[idx] / 32768.0f }
                        synchronized(pcmLock) {
                            pcmChunks.add(floats)
                            totalSamples += read
                            if (totalSamples >= MAX_SAMPLES) {
                                reachedLimit = true
                                recordingFlag.set(false)
                            }
                        }
                    } else if (read < 0) {
                        break
                    }
                }
            } finally {
                try {
                    recorder.stop()
                } catch (_: Exception) {
                }
                try {
                    recorder.release()
                } catch (_: Exception) {
                }
                if (audioRecord === recorder) {
                    audioRecord = null
                }
                if (reachedLimit) {
                    onAutoStop?.invoke()
                }
            }
        }
        return Result.success(Unit)
    }

    fun cancelRecording() {
        recordingFlag.set(false)
        recordJob?.cancel()
        recordJob = null
        synchronized(pcmLock) {
            pcmChunks.clear()
            totalSamples = 0
        }
    }

    suspend fun stopAndTranscribe(spec: OfflineVoiceModelId): Result<OfflineVoiceResult> = withContext(Dispatchers.IO) {
        recordingFlag.set(false)
        recordJob?.join()
        recordJob = null

        val samples: FloatArray = synchronized(pcmLock) {
            val merged = FloatArray(totalSamples)
            var offset = 0
            for (chunk in pcmChunks) {
                System.arraycopy(chunk, 0, merged, offset, chunk.size)
                offset += chunk.size
            }
            pcmChunks.clear()
            totalSamples = 0
            merged
        }

        val audioDurationSec = samples.size.toFloat() / SAMPLE_RATE.toFloat()
        if (samples.size < SAMPLE_RATE / 4) {
            return@withContext Result.failure(IllegalStateException("说话时间太短，请再试一次"))
        }

        try {
            // 全程在 recognizerMutex 内完成加载与推理，确保 ON_STOP / 删除操作必须等待当前推理结束
            val (text, elapsedSec) = recognizerMutex.withLock {
                val rec = ensureRecognizerLoadedLocked(spec)
                val t0 = System.nanoTime()
                val stream = rec.createStream()
                val decoded = try {
                    stream.acceptWaveform(samples, SAMPLE_RATE)
                    rec.decode(stream)
                    rec.getResult(stream).text.trim()
                } finally {
                    stream.release()
                }
                val sec = (System.nanoTime() - t0) / 1_000_000_000f
                decoded to sec
            }
            scheduleIdleRelease()
            Result.success(
                OfflineVoiceResult(
                    text = text,
                    modelId = spec,
                    audioDurationSec = audioDurationSec,
                    inferenceSec = elapsedSec
                )
            )
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    fun destroy() {
        destroyedFlag.set(true)
        cancelRecording()
        idleReleaseJob?.cancel()
        engineScope.launch {
            releaseModel()
            engineScope.cancel()
        }
    }
}
