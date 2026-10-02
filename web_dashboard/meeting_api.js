// Meeting credentials are separate from the global API secret.
window.ReviewMeetingAPI = (() => {
    const base = 'https://edge.maifeipin.com';
    const clear = () => {
        sessionStorage.removeItem('review_token');
        sessionStorage.removeItem('review_token_expires_at');
        sessionStorage.removeItem('dashboard_auth');
    };
    const request = async (path, options = {}) => {
        if (!/^\/agent\/api\/v1\/review-meetings(?:[/?]|$)/.test(path)) {
            throw new Error('会议凭据仅可用于会议接口');
        }
        const token = sessionStorage.getItem('review_token');
        const expires = Number(sessionStorage.getItem('review_token_expires_at'));
        if (!token || !expires || Date.now() / 1000 >= expires) {
            clear();
            window.location.href = '/login.html';
            throw new Error('会议登录已过期，请重新登录');
        }
        const headers = new Headers(options.headers || {});
        headers.set('Authorization', `Bearer ${token}`);
        const response = await fetch(base + path, { ...options, headers, credentials: 'omit' });
        if (response.status === 401 || response.status === 403) {
            clear();
            window.location.href = '/login.html';
        }
        return response;
    };
    const logout = async () => {
        try {
            await request('/agent/api/v1/review-meetings/logout', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
        } finally {
            clear();
            window.location.href = '/login.html';
        }
    };
    return { fetch: request, logout };
})();
