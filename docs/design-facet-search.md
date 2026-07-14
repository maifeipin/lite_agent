# Dashboard 多维标签过滤系统 — 设计方案 v1.1

> 修订记录：v1.0 → v1.1 采纳复核意见，修正 `sender` 字段名、`sortableAttributes` 配置错误、补充 `sort` 排序、Facet 消失问题、回填方案优化等 8 处。

## 总览

将 Dashboard 搜索从"纯关键词"升级为"关键词 + Facet 多维过滤"，让 4300+ RSS 文章和邮件可按来源、类型、时间等维度即时筛选。

---

## 一、改动范围（3 个文件）

| 文件 | 改动 | 代码量 |
|------|------|--:|
| `skills/ops_meili_sync.py` | 新增 `source`/`type`/`date` 字段提取 + 注册 filterableAttributes | ~40 行 |
| `web_dashboard/main.js` | 搜索函数支持 filter/sort + Facet 面板渲染/事件 + 非 Meili 源隐藏 | ~130 行 |
| `web_dashboard/style.css` | `.facet-group` / `.facet-item` 样式 | ~50 行 |

回填不另写脚本：重置 `meili_sync_state.json` 时间戳后直接调用 `/sync_meili`，利用现有 upsert 逻辑补全字段，代码复用率 100%。

---

## 二、数据结构变更

### 2.1 索引字段新增（两条索引统一）

| 字段 | 类型 | 提取规则 | 示例 |
|------|------|----------|------|
| `source` | string | RSS → URL 域名；邮件 → `sender` 头域名 | `v2ex.com`, `github.com`, `amazon.com` |
| `type` | string | 邮件：账单关键词匹配；RSS：`article` | `bill`, `dev`, `newsletter`, `article` |
| `date` | int64 | 统一为 Unix timestamp（见 2.5） | `1751884800` |
| `tags` | [string] | 用户自定义标签数组，默认 `[]`（Phase 3 启用） | `["重要"]` |

### 2.2 Meilisearch Settings 注册

> `score` 是系统内部排序规则，**不可**注册为 `sortableAttributes`，否则 Meilisearch 报错。

```json
POST /indexes/{uid}/settings
{
  "filterableAttributes": ["source", "type", "date", "tags"],
  "sortableAttributes": ["date"]
}
```

### 2.3 `source` 提取逻辑（修正：使用 `sender` 而非 `from`）

```python
from urllib.parse import urlparse

def extract_source(doc_type, raw_data):
    if doc_type == 'rss':
        return urlparse(raw_data.get('link', '')).netloc  # v2ex.com
    elif doc_type == 'email':
        # 注意：Meilisearch 中邮件发件人字段名是 'sender'，不是 'from'
        return raw_data.get('sender', '').split('@')[-1].rstrip('>')
```

### 2.4 `type` 自动分类逻辑（修正：去掉重复 "账单"）

```
邮件 type 判断:
  - 发件人 like '%@bank%' or '%unionpay%' or '%credit%'                  → "bill"
  - 标题 match '(invoice|receipt|payment|账单|消费|交易)'                  → "bill"
  - 发件人 like '%@github.com%' or '%@gitlab%' or '%@jira%'              → "dev"
  - 标题 match '(newsletter|digest|weekly|月刊|简报|周刊)'                 → "newsletter"
  - 其余                                                                 → "mail"

RSS type:
  - 固定 "article"
```

### 2.5 统一的 Date → Timestamp 转换（新增）

邮件用 RFC 2822 格式，RSS 用 ISO-8601，需要统一转换为 int64 时间戳：

```python
import email.utils
from datetime import datetime

def parse_to_timestamp(date_val) -> int:
    if not date_val:
        return 0
    if isinstance(date_val, (int, float)):
        return int(date_val)
    date_str = str(date_val).strip()
    # ISO-8601 (RSS): "2026-07-14T08:20:44Z"
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except ValueError:
        pass
    # RFC 2822 (Email): "Tue, 14 Jul 2026 16:20:39 +0800"
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt:
            return int(dt.timestamp())
    except Exception:
        pass
    return 0
```

---

## 三、Meilisearch 搜索调用变更（修正：补充 `sort`）

### 现状

```js
body: { q: query, offset, limit, attributesToHighlight: ['*'] }
```

### 改为

```js
body: {
    q: searchQuery,
    filter: activeFilters,
    sort: ["date:desc"],         // 必须显式排序，否则无关键词时结果乱序
    offset, limit,
    facets: ['source', 'type'],
    attributesToHighlight: ['*']
}
```

### Filter 构建逻辑

```js
function buildFilter() {
    const parts = [];
    for (const [field, values] of Object.entries(state.activeFilters)) {
        if (values.length === 0) continue;
        parts.push(values.map(v => `${field} = "${v}"`).join(' OR '));
    }
    return parts.length > 0 ? parts.map(p => `(${p})`).join(' AND ') : undefined;
}
```

---

## 四、前端 Facet 面板

### 4.1 渲染位置

搜索栏上方，横向排列 facet group（来源 / 类型），每个 group 内显示可点击的 checkbox 列表 + count。

### 4.2 交互规则

| 操作 | 行为 |
|------|------|
| 勾选/取消 checkbox | 更新 `state.activeFilters` → 触发 `performSearch()` |
| 输入关键词 | 全文搜索 + 当前 filters 同时生效 |
| 切换 tab | tab=`all`/`emails`/`rss` → 显示 Facet 面板；tab=`todos`/`sessions` → 隐藏 Facet 面板 |
| 多选同 group | OR 逻辑 |
| 跨 group 多选 | AND 逻辑 |

### 4.3 Facet 数据来源

Meilisearch 搜索结果自带 `facetDistribution`，零额外 API 调用。

### 4.4 防止 Facet 项消失（新增）

**问题**：勾选一个来源后，过滤后的 `facetDistribution` 中其他来源 count=0 被 Meilisearch 省略，未选中的 checkbox 从界面上消失，用户无法取消勾选。

**方案**：渲染时合并两个来源：
- Meilisearch 返回的 `facetDistribution`（当前结果集的分布）
- `state.activeFilters` 中用户已勾选的值（即使 count=0 也保留显示）

```js
function renderFacetPanel(facetDist) {
    for (const [group, serverVals] of Object.entries(facetDist)) {
        const merged = new Set([
            ...Object.keys(serverVals),
            ...(state.activeFilters[group] || [])
        ]);
        for (const val of merged) {
            const count = serverVals[val] || 0;
            const checked = (state.activeFilters[group] || []).includes(val);
            renderFacetItem(group, val, count, checked);
        }
    }
}
```

### 4.5 非 Meili 数据源隐藏面板（新增）

切换至 `todos` 或 `sessions` tab 时，Facet 面板通过 CSS `display: none` 完全隐藏——这些数据源不走 Meilisearch，无 facet 能力。

---

## 五、回填方案（修正：不另写脚本）

**原方案**：新建 `scripts/backfill_facets.py` 逐条 PUT 文档。

**修正方案**（代码复用率 100%）：
1. 升级 `ops_meili_sync.py` 的 `_doc_from_row()` 字段提取逻辑
2. 重置 `data/meili_sync_state.json` 的 `last_*_sync` 为 `1970-01-01T00:00:00Z`
3. VPS 上执行 `/sync_meili` 指令
4. Meilisearch 按主键 upsert 已有文档，字段原位补齐，耗时 ~10 秒

---

## 六、阶段划分

| 阶段 | 内容 | 风险 |
|:--:|------|:--:|
| **Phase 1** | 同步脚本加字段提取 + 注册 filterableAttributes + reset sync state 回填 | 低 |
| **Phase 2** | JS 搜索支持 filter/sort + Facet 面板 + 非 Meili 源隐藏 + 防消失合并 | 中 |
| **Phase 3** | （未来）用户自定义标签 API + 右键菜单 | 低 |

---

## 七、不做的事情

- 不做"或/且"逻辑切换
- 不做自定义时间范围 UI
- 不做 tag 颜色/图标
- 不做 URL 状态同步
- 不碰 sessions/todos 模块

---

## 八、改动量汇总

| 文件 | 行数 |
|------|--:|
| `ops_meili_sync.py` | +40 |
| `main.js` | +130 |
| `style.css` | +50 |
| **合计** | **~220** |

## 九、复核修正清单

| # | 问题 | 修正 |
|---|------|------|
| 1 | `from` 字段名错误（应为 `sender`） | 2.3 节已修正 |
| 2 | `sortableAttributes` 含非法字段 `score` | 2.2 节已移除 |
| 3 | 前端 payload 缺少 `sort` | 三节已补充 `sort: ["date:desc"]` |
| 4 | 正则重复 `账单` | 2.4 节已去重 |
| 5 | 缺 date 格式统一函数 | 2.5 节新增 `parse_to_timestamp()` |
| 6 | Facet 项勾选后消失 | 4.4 节新增合并渲染方案 |
| 7 | todos/sessions 不应显示 Facet | 4.5 节新增隐藏规则 |
| 8 | 另写回填脚本不必要 | 五节改为 reset sync state + `/sync_meili` |
