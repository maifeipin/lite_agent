package com.liteagent.mobile.core.network.models

import com.google.gson.annotations.SerializedName

data class MeetingItem(
    @SerializedName("id") val id: String,
    @SerializedName("title") val title: String,
    @SerializedName("state") val state: String, // open, awaiting_approval, approved, rejected, changes_requested
    @SerializedName("round") val round: Int = 1,
    @SerializedName("created_at") val createdAt: Long = 0L,
    @SerializedName("updated_at") val updatedAt: Long = 0L,
    @SerializedName("archived_at") val archivedAt: Long? = null
) {
    val isAwaitingApproval: Boolean
        get() = state.equals("awaiting_approval", ignoreCase = true) || state.equals("waiting", ignoreCase = true)

    val isApproved: Boolean
        get() = state.equals("approved", ignoreCase = true)

    val isRejected: Boolean
        get() = state.equals("rejected", ignoreCase = true)

    val isChangesRequested: Boolean
        get() = state.equals("changes_requested", ignoreCase = true)
}

data class ReviewMeetingsResponse(
    @SerializedName("meetings") val meetings: List<MeetingItem> = emptyList(),
    @SerializedName("error") val error: String? = null
)

data class MeetingParticipant(
    @SerializedName("name") val name: String,
    @SerializedName("invited_round") val invitedRound: Int = 1,
    @SerializedName("online") val online: Boolean = false,
    @SerializedName("last_poll_at") val lastPollAt: Long? = null
)

data class MeetingEvent(
    @SerializedName("seq") val seq: Int,
    @SerializedName("round") val round: Int,
    @SerializedName("actor") val actor: String,
    @SerializedName("kind") val kind: String, // created, review, comment, approval_requested, decision, management_action
    @SerializedName("body") val body: Map<String, Any?> = emptyMap(),
    @SerializedName("created_at") val createdAt: Long = 0L
) {
    val reviewPosition: String?
        get() = (body["position"] as? String)

    val reviewText: String?
        get() = (body["text"] as? String) ?: (body["summary"] as? String) ?: (body["note"] as? String)

    val decisionValue: String?
        get() = (body["decision"] as? String)

    val requestId: String?
        get() = (body["request_id"] as? String)
}

data class MeetingDetail(
    @SerializedName("id") val id: String,
    @SerializedName("title") val title: String,
    @SerializedName("state") val state: String,
    @SerializedName("round") val round: Int = 1,
    @SerializedName("brief") val brief: String? = null,
    @SerializedName("last_seq") val lastSeqRaw: Int? = null,
    @SerializedName("next_since") val nextSince: Int? = null,
    @SerializedName("participants") val participants: List<MeetingParticipant> = emptyList(),
    @SerializedName("events") val events: List<MeetingEvent> = emptyList(),
    @SerializedName("hint") val hint: String? = null,
    @SerializedName("error") val error: String? = null
) {
    val lastSeq: Int
        get() = lastSeqRaw ?: nextSince ?: events.maxOfOrNull { it.seq } ?: 0

    val isAwaitingApproval: Boolean
        get() = state.equals("awaiting_approval", ignoreCase = true) || state.equals("waiting", ignoreCase = true)
}

data class DecideMeetingRequest(
    @SerializedName("decision") val decision: String, // approve, reject, revise
    @SerializedName("note") val note: String? = null,
    @SerializedName("expected_version") val expectedVersion: Int? = null,
    @SerializedName("request_id") val requestId: String? = null
)

data class CreateMeetingRequest(
    @SerializedName("title") val title: String,
    @SerializedName("brief") val brief: String? = null,
    @SerializedName("invite_ttl_seconds") val inviteTtlSeconds: Int = 86400
)

data class CreateMeetingResponse(
    @SerializedName("id") val id: String? = null,
    @SerializedName("owner_token") val ownerToken: String? = null,
    @SerializedName("invite_url") val inviteUrl: String? = null,
    @SerializedName("error") val error: String? = null
)
