package com.liteagent.mobile.core.network

import com.liteagent.mobile.core.network.models.*
import okhttp3.MultipartBody
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

    @POST("agent/api/v1/session/title")
    suspend fun renameSession(
        @Body request: RenameSessionRequest
    ): Response<RenameSessionResponse>

    @POST("agent/api/v1/chat")
    suspend fun sendChatMessage(
        @Body request: ChatRequest
    ): Response<ChatResponse>

    @Multipart
    @POST("agent/api/v1/ocr")
    suspend fun uploadOcrImage(
        @Part file: MultipartBody.Part
    ): Response<OcrResponse>

    @GET("agent/api/v1/dashboard")
    suspend fun getDashboardCommands(): Response<List<DashboardCommandItem>>

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

    // --- Phone App 真机自动化执行中枢 ---
    @GET("agent/api/v1/phone-app/devices")
    suspend fun getPhoneDevices(): Response<PhoneDevicesResponse>

    @GET("agent/api/v1/phone-app/apps")
    suspend fun getPhoneApps(): Response<PhoneAppsResponse>

    @GET("agent/api/v1/phone-app/batches")
    suspend fun getPhoneBatches(): Response<PhoneBatchesResponse>

    @GET("agent/api/v1/phone-app/batches/{batchId}")
    suspend fun getPhoneBatchDetail(
        @Path("batchId") batchId: String
    ): Response<PhoneBatchDetailResponse>

    @POST("agent/api/v1/phone-app/batches")
    suspend fun createPhoneBatch(
        @Body request: CreatePhoneBatchRequest
    ): Response<CreatePhoneBatchResponse>

    @POST("agent/api/v1/phone-app/batches/{batchId}/approve")
    suspend fun approvePhoneBatch(
        @Path("batchId") batchId: String,
        @Body request: Map<String, String> = emptyMap()
    ): Response<PhoneActionResponse>

    @POST("agent/api/v1/phone-app/batches/{batchId}/execute")
    suspend fun executePhoneBatch(
        @Path("batchId") batchId: String,
        @Body request: Map<String, String> = emptyMap()
    ): Response<PhoneActionResponse>

    @POST("agent/api/v1/phone-app/batches/{batchId}/reject")
    suspend fun rejectPhoneBatch(
        @Path("batchId") batchId: String,
        @Body request: PhoneActionRequest
    ): Response<PhoneActionResponse>

    @POST("agent/api/v1/phone-app/batches/{batchId}/cancel")
    suspend fun cancelPhoneBatch(
        @Path("batchId") batchId: String,
        @Body request: PhoneActionRequest
    ): Response<PhoneActionResponse>

    @POST("agent/api/v1/phone-app/device/resume")
    suspend fun resumePhoneDevice(
        @Body request: PhoneActionRequest
    ): Response<PhoneActionResponse>

    // --- 发现中枢：邮件、账单、RSS 简报、复杂任务触发器、Socks5 节点 ---
    @GET("agent/api/v1/mail/recent")
    suspend fun getRecentEmails(
        @Query("limit") limit: Int = 20
    ): Response<EmailListResponse>

    @GET("agent/api/v1/billing/summary")
    suspend fun getBillingSummary(): Response<BillingSummaryResponse>

    @GET("agent/api/v1/rss/brief")
    suspend fun getRssBrief(): Response<RssBriefResponse>

    @GET("agent/api/v1/task-specs")
    suspend fun getTaskSpecs(): Response<TaskSpecsListResponse>

    @POST("agent/api/v1/task-specs/{id}/run")
    suspend fun runTaskSpec(
        @Path("id") id: String,
        @Body request: Map<String, String> = emptyMap()
    ): Response<TaskSpecRunResponse>

    @GET("agent/api/v1/socks5")
    suspend fun getSocks5Nodes(): Response<Socks5ListResponse>

    @GET("agent/api/v1/socks5/test")
    suspend fun testSocks5Node(
        @Query("id") id: Int,
        @Query("host") host: String? = null
    ): Response<Socks5TestResponse>

    @GET("agent/api/v1/socks5/health")
    suspend fun checkSocks5Outbound(
        @Query("id") id: Int
    ): Response<Socks5TestResponse>

    @POST("agent/api/v1/socks5/active")
    suspend fun setActiveSocks5Node(
        @Body request: Socks5SetActiveRequest
    ): Response<Socks5SetActiveResponse>
}
