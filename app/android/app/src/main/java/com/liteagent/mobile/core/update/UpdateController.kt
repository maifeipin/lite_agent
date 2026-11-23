package com.liteagent.mobile.core.update

import android.content.Context
import com.liteagent.mobile.core.security.SecureTokenStorage
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File

class UpdateController(
    private val context: Context,
    private val tokenStorage: SecureTokenStorage,
    private val scope: CoroutineScope,
    private val beforeInstall: () -> Unit = {}
) {

    sealed interface State {
        data object Idle : State
        data object Checking : State
        data class UpToDate(val versionName: String) : State
        data class Available(val info: AppVersionDto) : State
        data class Downloading(val info: AppVersionDto, val progress: UpdateDownloadProgress) : State
        data class ReadyToInstall(
            val info: AppVersionDto,
            val apk: File,
            val needsPermission: Boolean
        ) : State
        data class Failed(val info: AppVersionDto?, val message: String) : State
    }

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state.asStateFlow()

    private var job: Job? = null
    private var autoChecked = false
    private var installLaunched = false

    val currentVersionName: String get() = AppUpdateManager.currentVersionName
    val currentVersionCode: Int get() = AppUpdateManager.currentVersionCode
    val channel: String get() = AppUpdateManager.channel

    fun autoCheckOnce() {
        if (autoChecked) return
        autoChecked = true
        check(silent = true)
    }

    fun checkManually() {
        when (_state.value) {
            is State.Downloading, is State.ReadyToInstall -> return
            else -> Unit
        }
        job?.cancel()
        job = null
        check(silent = false)
    }

    private fun check(silent: Boolean) {
        if (job?.isActive == true) return
        if (!silent) _state.value = State.Checking

        job = scope.launch {
            try {
                val serverUrl = tokenStorage.getActiveServerUrl()
                val session = tokenStorage.getSession()
                val token = session?.sessionToken

                val info = AppUpdateManager.checkForUpdate(
                    context = context,
                    serverUrl = serverUrl,
                    token = token
                )
                val hasNewer = info.hasUpdate &&
                    info.versionCode > AppUpdateManager.currentVersionCode

                val skippedCode = prefs.getInt(KEY_SKIPPED_CODE, 0)
                _state.value = when {
                    !hasNewer -> if (silent) State.Idle else State.UpToDate(AppUpdateManager.currentVersionName)
                    info.forceUpdate -> State.Available(info)
                    silent && skippedCode == info.versionCode -> State.Idle
                    else -> State.Available(info)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = if (silent) State.Idle else State.Failed(null, e.localizedMessage ?: "检查更新失败")
            }
        }
    }

    fun download() {
        val available = (_state.value as? State.Available)?.info
            ?: (_state.value as? State.Failed)?.info
            ?: return
        if (job?.isActive == true) return

        installLaunched = false
        _state.value = State.Downloading(available, UpdateDownloadProgress(0, available.sizeBytes))

        job = scope.launch {
            try {
                val serverUrl = tokenStorage.getActiveServerUrl()
                val session = tokenStorage.getSession()
                val token = session?.sessionToken

                val apk = AppUpdateManager.downloadApk(
                    context = context,
                    serverUrl = serverUrl,
                    info = available,
                    token = token
                ) { progress ->
                    _state.value = State.Downloading(available, progress)
                }
                val canInstall = AppUpdateManager.canInstallPackages(context)
                _state.value = State.ReadyToInstall(available, apk, needsPermission = !canInstall)
                if (canInstall) {
                    launchInstall(apk)
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.value = State.Failed(available, e.localizedMessage ?: "下载安装包失败")
            }
        }
    }

    fun requestInstallPermission() {
        AppUpdateManager.openInstallPermissionSettings(context)
    }

    fun onResume() {
        val ready = _state.value as? State.ReadyToInstall ?: return
        if (ready.needsPermission && AppUpdateManager.canInstallPackages(context)) {
            _state.value = ready.copy(needsPermission = false)
            launchInstall(ready.apk)
        }
    }

    fun installNow() {
        val ready = _state.value as? State.ReadyToInstall ?: return
        if (!ready.needsPermission) {
            launchInstall(ready.apk)
        } else {
            requestInstallPermission()
        }
    }

    private fun launchInstall(apk: File) {
        if (installLaunched) return
        installLaunched = true
        beforeInstall()
        AppUpdateManager.installApk(context, apk)
    }

    fun skipThisVersion() {
        val available = (_state.value as? State.Available)?.info ?: return
        prefs.edit().putInt(KEY_SKIPPED_CODE, available.versionCode).apply()
        _state.value = State.Idle
    }

    fun dismiss() {
        val current = _state.value
        if (current is State.Downloading) {
            job?.cancel()
            job = null
        }
        _state.value = State.Idle
    }

    companion object {
        private const val PREFS_NAME = "lite_agent_app_update_prefs"
        private const val KEY_SKIPPED_CODE = "skipped_version_code"
    }
}
