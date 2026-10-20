// Run with: node dev/tests/frontend/test_phone_apps_injection.js
// 反例回归 (R1): 设备离线诊断文本来自 Worker 上报 (最终含 CLI stderr)，进入 innerHTML 前必须转义。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const mainSrc = fs.readFileSync('web_dashboard/main.js', 'utf8');
const hSource = mainSrc.match(/function h\(s\) \{[\s\S]*?\n\}/)[0];
const moduleSrc = fs.readFileSync('web_dashboard/modules/phone_apps.js', 'utf8');

function makeElement(tag) {
    return {
        tagName: tag,
        id: '',
        style: {},
        innerHTML: '',
        textContent: '',
        inserted: [],
        parentNode: null,
    };
}

function bootstrap(devices) {
    const created = [];
    let bar = null;
    const header = {
        parentNode: {
            insertBefore(node) {
                created.push(node);
                node.inserted.push('topbar');
            },
        },
    };
    const context = {
        console,
        window: {},
        document: {
            querySelector: sel => (sel === '.results-header' ? header : null),
            getElementById: () => null,
            createElement: tag => {
                const el = makeElement(tag);
                if (tag === 'div') bar = el;
                return el;
            },
        },
        registerTabModule: () => {},
        alert: () => {},
        // 模块加载时自检会触发一次 init()，此处只需让请求失败而不是抛 ReferenceError
        fetch: async () => ({ ok: false, json: async () => ({}) }),
    };
    vm.runInNewContext(hSource, context);
    assert.equal(typeof context.h, 'function', 'main.js 中的 h() 转义函数必须可加载');
    vm.runInNewContext(moduleSrc, context);
    const ui = context.window.PhoneAppUI;
    ui._cachedDevices = devices;
    return { ui, bar: () => bar, created };
}

// 1. 核心反例：带 onerror 的 img 标签 + 特殊字符
const payload = '</span><img src=x onerror="alert(1)">&<>"\'\n换行';
const { ui, bar } = bootstrap([
    { device_id: 'c68a3cf2', model: 'OnePlus 9', status: 'offline', offline_reason: payload },
]);
ui.renderTopBar();
const html = bar().innerHTML;

assert.ok(html.length > 0, '顶栏必须渲染');
assert.ok(!html.includes('<img'), '注入的 img 标签不得原样进入 innerHTML');
assert.ok(!html.includes('</span><img'), '闭合标签注入必须被转义');
assert.ok(html.includes('&lt;img'), '尖括号必须转义为实体');
assert.ok(html.includes('&amp;'), '& 必须转义');
assert.ok(html.includes('&quot;'), '双引号必须转义');
assert.ok(html.includes('🔴 离线：'), '离线态必须披露原因前缀');

// 2. 正常中文原因不被破坏
const { ui: ui2, bar: bar2 } = bootstrap([
    { device_id: 'c68a3cf2', model: 'OnePlus 9', status: 'offline', offline_reason: 'ADB 未识别到设备 (未插线/未连接)' },
]);
ui2.renderTopBar();
assert.ok(bar2().innerHTML.includes('ADB 未识别到设备'));

// 3. 暂停设备的 device_id 也必须转义，且不再拼进 onclick 字符串
const { ui: ui3, bar: bar3 } = bootstrap([
    { device_id: "x' onmouseover='alert(1)", model: 'OnePlus 9', status: 'paused' },
]);
ui3.renderTopBar();
const pausedHtml = bar3().innerHTML;
assert.ok(!pausedHtml.includes("resumeDevice('"), 'onclick 不得内联拼接设备标识');
assert.ok(pausedHtml.includes('resumeDevice(this.dataset.deviceId)'), '恢复按钮应改用 dataset 传参');
assert.ok(pausedHtml.includes('&quot;') || pausedHtml.includes('data-device-id="x\' onmouseover=\'alert(1)"'),
    '设备标识必须落在双引号属性值内部，不得逃逸出新属性');

// 4. 在线态不显示离线原因
const { ui: ui4, bar: bar4 } = bootstrap([
    { device_id: 'c68a3cf2', model: 'OnePlus 9', status: 'online', offline_reason: '<img src=x>' },
]);
ui4.renderTopBar();
assert.ok(bar4().innerHTML.includes('🟢 1 台受控设备在线'));
assert.ok(!bar4().innerHTML.includes('<img'));

console.log('Phone apps 顶栏注入回归: 4 个场景通过');
