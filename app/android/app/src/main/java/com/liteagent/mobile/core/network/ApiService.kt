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

    @POST("agent/api/v1/auth")
    suspend fun login(
        @Body request: LoginRequest
    ): Response<LoginResponse>

    @POST("agent/api/v1/auth/refresh")
    suspend fun refresh(
        @Body request: RefreshRequest
    ): Response<RefreshResponse>

    @POST("agent/api/v1/auth/logout")
    suspend fun logout(
        @Body request: LogoutRequest
    ): Response<LogoutResponse>

    @GET("agent/api/v1/dashboard")
    suspend fun testConnection(): Response<Map<String, Any>>

    // --- 待办事项 (Todos GTD) ---
    @GET("agent/api/v1/todos")
    suspend fun getTodos(
        @Query("status") status: String = "pending,active,done"
    ): Response<TodosResponse>

    @POST("agent/api/v1/todos")
    suspend fun createTodo(
        @Body request: CreateTodoRequest
    ): Response<CreateTodoResponse>

    @PATCH("agent/api/v1/todos/{id}")
    suspend fun updateTodo(
        @Path("id") id: String,
        @Body request: UpdateTodoRequest
    ): Response<UpdateTodoResponse>

    @DELETE("agent/api/v1/todos/{id}")
    suspend fun deleteTodo(
        @Path("id") id: String
    ): Response<Map<String, Any>>

    // --- 会审决策中枢 (Review Meetings) ---
    @GET("agent/api/v1/review-meetings")
    suspend fun getReviewMeetings(): Response<ReviewMeetingsResponse>

    @GET("agent/api/v1/review-meetings/{id}")
    suspend fun getReviewMeetingDetail(
        @Path("id") id: String,
        @Query("since") since: Int = 0
    ): Response<MeetingDetail>

    @POST("agent/api/v1/review-meetings/{id}")
    suspend fun decideReviewMeeting(
        @Path("id") id: String,
        @Body request: DecideMeetingRequest
    ): Response<MeetingDetail>

    @POST("agent/api/v1/review-meetings")
    suspend fun createReviewMeeting(
        @Body request: CreateMeetingRequest
    ): Response<CreateMeetingResponse>

    // --- 发现中枢：邮件与账单 ---
    @GET("agent/api/v1/mail/recent")
    suspend fun getRecentEmails(
        @Query("limit") limit: Int = 20
    ): Response<EmailListResponse>

    @GET("agent/api/v1/billing/summary")
    suspend fun getBillingSummary(): Response<BillingSummaryResponse>
}
