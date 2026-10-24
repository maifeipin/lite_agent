package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

data class TodoItem(
    @SerializedName("id") val id: String,
    @SerializedName("title") val title: String,
    @SerializedName("description") val description: String? = null,
    @SerializedName("status") val status: String = "pending", // pending, active, done, dropped, shelved
    @SerializedName("kind") val kind: String = "misc",
    @SerializedName("project") val project: String? = null,
    @SerializedName("due_at") val dueAt: String? = null,
    @SerializedName("notify_enabled") val notifyEnabled: Any? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("updated_at") val updatedAt: String? = null
) {
    val isCompleted: Boolean
        get() = status.equals("done", ignoreCase = true)
}

data class TodosResponse(
    @SerializedName("success") val success: Boolean,
    @SerializedName("data") val data: List<TodoItem>? = null,
    @SerializedName("error") val error: String? = null
)

data class CreateTodoRequest(
    @SerializedName("title") val title: String,
    @SerializedName("kind") val kind: String = "misc",
    @SerializedName("project") val project: String? = null,
    @SerializedName("description") val description: String? = null,
    @SerializedName("due_at") val dueAt: String? = null,
    @SerializedName("notify_enabled") val notifyEnabled: Boolean = true
)

data class CreateTodoResponse(
    @SerializedName("success") val success: Boolean,
    @SerializedName("message") val message: String? = null,
    @SerializedName("error") val error: String? = null
)

data class UpdateTodoRequest(
    @SerializedName("status") val status: String? = null,
    @SerializedName("title") val title: String? = null,
    @SerializedName("description") val description: String? = null,
    @SerializedName("project") val project: String? = null,
    @SerializedName("due_at") val dueAt: String? = null
)

data class UpdateTodoResponse(
    @SerializedName("success") val success: Boolean,
    @SerializedName("message") val message: String? = null,
    @SerializedName("error") val error: String? = null
)
