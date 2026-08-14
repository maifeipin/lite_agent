// ============================================================
//  Universal Modal Component (通用 UI 对话框组件)
// ============================================================

function showModal(options = {}) {
    document.querySelectorAll('.universal-modal-overlay, .session-modal-overlay').forEach(el => el.remove());

    const overlay = document.createElement('div');
    overlay.className = 'universal-modal-overlay';

    let bodyContent = '';
    if (options.contentType === 'markdown' && options.content) {
        bodyContent = typeof marked !== 'undefined' ? marked.parse(options.content) : options.content;
    } else if (options.content) {
        bodyContent = options.content;
    }

    let inputHtml = '';
    if (options.input) {
        const val = (options.input.value || '').replace(/"/g, '&quot;');
        const ph = options.input.placeholder || '';
        inputHtml = `<input type="text" class="universal-modal-input" value="${val}" placeholder="${ph}" id="universal-modal-input-field" />`;
    }

    let buttonsHtml = '';
    if (options.buttons && options.buttons.length > 0) {
        buttonsHtml = `<div class="universal-modal-footer">` +
            options.buttons.map((btn, idx) => `<button class="modal-btn ${btn.class || 'modal-btn-secondary'}" data-btn-idx="${idx}">${btn.text}</button>`).join('') +
            `</div>`;
    }

    const modalHtml = `
        <div class="universal-modal" style="${options.width ? 'width:' + options.width : ''}">
            <div class="universal-modal-header">
                ${options.icon ? `<span class="universal-modal-icon">${options.icon}</span>` : ''}
                <span class="universal-modal-title">${options.title || '提示'}</span>
                <button class="universal-modal-close">&times;</button>
            </div>
            <div class="universal-modal-body">
                ${bodyContent}
                ${inputHtml}
            </div>
            ${buttonsHtml}
        </div>
    `;

    overlay.innerHTML = modalHtml;
    document.body.appendChild(overlay);
    if (typeof renderMath === 'function') {
        renderMath(overlay.querySelector('.universal-modal-body'));
    }

    const closeModal = () => overlay.remove();

    const closeBtn = overlay.querySelector('.universal-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closeModal();
    });

    const handleKeyDown = (e) => {
        if (e.key === 'Escape') {
            closeModal();
            document.removeEventListener('keydown', handleKeyDown);
        }
    };
    document.addEventListener('keydown', handleKeyDown);

    const inputEl = overlay.querySelector('#universal-modal-input-field');
    if (inputEl) {
        inputEl.focus();
        inputEl.select();
        inputEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const primaryBtn = options.buttons ? options.buttons.find(b => b.class && b.class.includes('primary')) : null;
                if (primaryBtn && primaryBtn.onClick) {
                    primaryBtn.onClick({ close: closeModal }, inputEl.value.trim());
                }
            }
        });
    }

    if (options.buttons) {
        options.buttons.forEach((btn, idx) => {
            const btnEl = overlay.querySelector(`[data-btn-idx="${idx}"]`);
            if (btnEl) {
                btnEl.addEventListener('click', () => {
                    const inputVal = inputEl ? inputEl.value.trim() : '';
                    if (btn.onClick) {
                        btn.onClick({ close: closeModal }, inputVal);
                    } else {
                        closeModal();
                    }
                });
            }
        });
    }
}

function showSessionMessagesModal(sessionKey, messages) {
    if (!messages || messages.length === 0) {
        showModal({
            title: `会话消息历史 (${sessionKey})`,
            icon: '💬',
            content: '*（暂无历史消息）*',
            contentType: 'markdown'
        });
        return;
    }

    const roleIcons = { user: '👤 **用户**', assistant: '🤖 **AI**', system: '⚙️ **System**', tool: '🔧 **Tool**' };
    const mdText = messages.map(m => {
        const iconStr = roleIcons[m.role] || `💬 **${m.role}**`;
        const timeStr = m.time ? ` *(${new Date(m.time * 1000).toLocaleTimeString('zh-CN')})*` : '';
        const safeContent = typeof h === 'function' ? h(m.content || '(空)') : (m.content || '(空)');
        return `### ${iconStr}${timeStr}\n${safeContent}`;
    }).join('\n\n---\n\n');

    showModal({
        title: `会话消息历史`,
        icon: '📜',
        content: mdText,
        contentType: 'markdown',
        width: '680px'
    });
}

function initModalClose() {
    const modal = document.getElementById('command-modal');
    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === e.currentTarget && typeof closeCommandModal === 'function') {
                closeCommandModal();
            }
        });
    }
}
