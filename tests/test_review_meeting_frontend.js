// Run with: node tests/test_review_meeting_frontend.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web_dashboard/meeting_api.js', 'utf8');
const values = new Map();
const calls = [];
let status = 200;
const context = {
    window: { location: { href: '' } }, Headers, Date,
    sessionStorage: {
        getItem: k => values.get(k) ?? null,
        setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k),
    },
    fetch: async (url, options) => { calls.push({ url, options }); return { status }; },
};
vm.runInNewContext(source, context);
const api = context.window.ReviewMeetingAPI;
function login() {
    values.set('review_token', 'temporary-host-session');
    values.set('review_token_expires_at', String(Date.now() / 1000 + 3600));
    values.set('dashboard_auth', 'ok');
}
(async () => {
    login();
    await api.fetch('/agent/api/v1/review-meetings?since=0', { credentials: 'include' });
    assert.equal(calls[0].url, 'https://edge.maifeipin.com/agent/api/v1/review-meetings?since=0');
    assert.equal(calls[0].options.credentials, 'omit');
    assert.equal(calls[0].options.headers.get('Authorization'), 'Bearer temporary-host-session');
    await assert.rejects(api.fetch('/agent/api/v1/chat'));
    assert.equal(calls.length, 1, 'scoped token must never be sent to global API');
    values.set('review_token_expires_at', '1');
    await assert.rejects(api.fetch('/agent/api/v1/review-meetings'));
    assert.equal(calls.length, 1, 'expired token must not be sent');
    assert.equal(values.has('review_token'), false);
    assert.equal(context.window.location.href, '/login.html');
    login(); status = 401;
    await api.fetch('/agent/api/v1/review-meetings');
    assert.equal(values.has('review_token'), false, '401 clears stale credentials');
    login(); status = 200;
    await api.logout();
    const logout = calls.at(-1);
    assert.equal(logout.url, 'https://edge.maifeipin.com/agent/api/v1/review-meetings/logout');
    assert.equal(logout.options.method, 'POST');
    assert.equal(logout.options.body, '{}');
    assert.equal(values.has('review_token'), false);
    console.log('Meeting frontend: 5 scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
