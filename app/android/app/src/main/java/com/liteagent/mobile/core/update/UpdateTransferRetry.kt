package com.liteagent.mobile.core.update

import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive

/** A rejected HTTP/range response is not a transient transport interruption. */
internal class UpdateTransferRejected(message: String) : IOException(message)

internal suspend fun <T> retryUpdateTransfer(
    pause: suspend (Long) -> Unit = { delay(it) },
    transfer: suspend () -> T
): T {
    for (attempt in 0..3) {
        currentCoroutineContext().ensureActive()
        try {
            return transfer()
        } catch (e: CancellationException) {
            throw e
        } catch (e: UpdateTransferRejected) {
            throw e
        } catch (e: java.io.FileNotFoundException) {
            throw IOException("无法写入更新文件，请检查可用存储空间", e)
        } catch (e: javax.net.ssl.SSLHandshakeException) {
            throw IOException("更新服务器安全连接校验失败，请稍后重试", e)
        } catch (e: IOException) {
            currentCoroutineContext().ensureActive()
            if (attempt == 3) throw IOException("网络中断，已重试 3 次，请检查网络后重试", e)
            pause(1000L shl attempt)
        }
    }
    error("Unreachable")
}
