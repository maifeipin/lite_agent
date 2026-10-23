package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

data class ChatRequest(
    @SerializedName("session_id") val sessionId: String,
    @SerializedName("text") val text: String,
    @SerializedName("notify_channels") val notifyChannels: List<String> = listOf("api")
)

data class ChatResponse(
    @SerializedName("type") val type: String = "sync",
    @SerializedName("status") val status: String? = null,
    @SerializedName("response") val response: String? = null,
    @SerializedName("task_id") val taskId: String? = null,
    @SerializedName("message") val message: String? = null,
    @SerializedName("logs") val logs: List<Any>? = null,
    @SerializedName("title") val title: String? = null,
    @SerializedName("session_key") val sessionKey: String? = null
)

data class SessionItem(
    @SerializedName("session_key") val sessionKey: String,
    @SerializedName("title") val title: String = "",
    @SerializedName("status") val status: String = "chatting",
    @SerializedName("tool_calls") val toolCalls: Int = 0,
    @SerializedName("token_usage") val tokenUsage: Long = 0L,
    @SerializedName("model") val model: String = "",
    @SerializedName("goal") val goal: String = "",
    @SerializedName("updated_at") val updatedAt: Double = 0.0
) {
    val displayTitle: String
        get() = if (title.isNotBlank()) title else sessionKey.removePrefix("api:")
}

data class SessionsListResponse(
    @SerializedName("sessions") val sessions: List<SessionItem> = emptyList(),
    @SerializedName("total") val total: Int = 0
)

data class MessageItem(
    @SerializedName("role") val role: String,
    @SerializedName("content") val content: String = "",
    @SerializedName("reasoning_content") val reasoningContent: String? = null,
    @SerializedName("time") val time: Double = 0.0
)

data class MessagesListResponse(
    @SerializedName("messages") val messages: List<MessageItem> = emptyList()
)

data class TaskStreamData(
    @SerializedName("status") val status: String? = null,
    @SerializedName("response") val response: String? = null,
    @SerializedName("progress") val progress: Map<String, Any>? = null,
    @SerializedName("logs") val logs: List<Any>? = null,
    @SerializedName("total_logs") val totalLogs: Int = 0
)
