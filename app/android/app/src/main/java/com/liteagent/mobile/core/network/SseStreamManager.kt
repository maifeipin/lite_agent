package com.liteagent.mobile.core.network

import com.google.gson.Gson
import com.liteagent.mobile.core.network.models.TaskStreamData
import com.liteagent.mobile.core.security.SecureTokenStorage
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.sse.EventSource
import okhttp3.sse.EventSourceListener
import okhttp3.sse.EventSources
import java.util.concurrent.TimeUnit

open class SseStreamManager(
    private val tokenStorage: SecureTokenStorage
) {
    companion object {
        val TERMINAL_STATES = setOf(
            "done", "completed", "succeeded", "partial", "failed", "error", "cancelled"
        )
    }

    private val gson = Gson()
    private var activeEventSource: EventSource? = null

    private val sseClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    open suspend fun startStream(
        serverUrl: String,
        taskId: String,
        sessionId: String,
        onEvent: (TaskStreamData) -> Unit,
        onTerminal: (status: String, finalData: TaskStreamData) -> Unit,
        onPrematureClosed: (lastStatus: String?) -> Unit,
        onError: (Throwable?, Int?) -> Unit
    ) {
        cancel()

        // 显式使用传入的目标服务器 URL，严禁暗中依赖当前活动服务器
        val normalizedServer = ServerUrlNormalizer.normalize(serverUrl)
        val token = tokenStorage.getGeneralToken(normalizedServer)
        val cleanSessionId = sessionId.removePrefix("api:")
        val streamUrl = "$normalizedServer/agent/api/v1/task/stream?task_id=$taskId&session_id=$cleanSessionId"

        val requestBuilder = Request.Builder()
            .url(streamUrl)
            .addHeader("Accept", "text/event-stream")

        if (token.isNotBlank()) {
            requestBuilder.addHeader("Authorization", "Bearer $token")
        }

        val request = requestBuilder.build()
        val factory = EventSources.createFactory(sseClient)

        var isTerminalReached = false
        var lastReportedStatus: String? = null

        activeEventSource = factory.newEventSource(request, object : EventSourceListener() {
            override fun onOpen(eventSource: EventSource, response: Response) {
                // Connected
            }

            override fun onEvent(eventSource: EventSource, id: String?, type: String?, data: String) {
                if (data == "[DONE]") {
                    if (!isTerminalReached) {
                        onPrematureClosed(lastReportedStatus)
                    }
                    return
                }
                try {
                    val streamData = gson.fromJson(data, TaskStreamData::class.java)
                    val status = streamData.status?.lowercase()
                    lastReportedStatus = status
                    onEvent(streamData)

                    if (status != null && status in TERMINAL_STATES) {
                        isTerminalReached = true
                        onTerminal(status, streamData)
                    }
                } catch (_: Exception) {
                    // Ignore parse errors on raw keepalives
                }
            }

            override fun onClosed(eventSource: EventSource) {
                if (!isTerminalReached) {
                    // 服务端 300s 轮询结束或网络关闭，非终态退出，进入待恢复状态
                    onPrematureClosed(lastReportedStatus)
                }
            }

            override fun onFailure(eventSource: EventSource, t: Throwable?, response: Response?) {
                if (!isTerminalReached) {
                    onError(t, response?.code)
                }
            }
        })
    }

    open fun cancel() {
        activeEventSource?.cancel()
        activeEventSource = null
    }
}
