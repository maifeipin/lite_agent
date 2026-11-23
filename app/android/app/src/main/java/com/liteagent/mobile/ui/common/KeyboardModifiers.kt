package com.liteagent.mobile.ui.common

import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusManager
import androidx.compose.ui.input.pointer.pointerInput

/**
 * 全局通用的输入框键盘避让与自动收起扩展
 */
fun Modifier.autoDismissKeyboardOnTap(focusManager: FocusManager): Modifier = this.pointerInput(Unit) {
    detectTapGestures(onTap = {
        focusManager.clearFocus()
    })
}

fun Modifier.imeAvoidance(): Modifier = this.imePadding()

fun Modifier.fullScreenInputPadding(): Modifier = this
    .statusBarsPadding()
    .navigationBarsPadding()
    .imePadding()
