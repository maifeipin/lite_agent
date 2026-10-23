package com.liteagent.mobile.core.network

import com.liteagent.mobile.core.network.models.*
import retrofit2.Response
import retrofit2.http.*

interface ApiService {
    @GET("agent/api/v1/sessions")
    suspend fun getSessions(): Response<SessionsListResponse>

    @GET("agent/api/v1/sessions/messages")
    suspend fun getSessionMessages(
        @Query("session_key") sessionKey: String,
        @Query("limit") limit: Int = 50
    ): Response<MessagesListResponse>

    @POST("agent/api/v1/chat")
    suspend fun sendChatMessage(
        @Body request: ChatRequest
    ): Response<ChatResponse>

    @GET("agent/api/v1/dashboard")
    suspend fun testConnection(): Response<Map<String, Any>>
}
