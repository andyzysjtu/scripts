# WikipediaFetcher

把**单个维基百科页面**尽量完整、按 wiki 自身「章节 / 块」结构抓取为结构化 JSON（可选附带 HTML / wikitext），供下游 agent 作为资料继续使用。

设计原则：

- 只按 wiki 字面的章节与信息块组织，**不注入业务逻辑**（不做关系抽取、时间线、实体分类等）。
- 尽量多拿、尽量原样；语义理解留给使用方（agent）。
- 保留原文兜底（正文 HTML、wikitext），结构化字段丢信息时可回退。

## 依赖与运行

```bash
python3 -m pip install -r requirements.txt   # 依赖 Wikipedia-API
python3 WikipediaFetcher/wikipedia_fetcher.py --lang ja --keyword "豊臣秀長" --out toyotomi_hidenaga.json
```

未指定 `--out` 时，JSON 打印到 stdout。

## CLI 参数

| 参数 | 说明 |
|---|---|
| `--keyword` | 页面标题或搜索关键词。默认 `豊臣秀長` |
| `--lang` | 语言代码。默认 `ja` |
| `--out` | 写 JSON 到文件。给定时会**自动**同目录写 `.html`（完整正文内容）与 `.wiki`（wikitext 侧车） |
| `--html-out` | 正文 HTML 输出路径（默认 `OUT` 同名的 `.html`） |
| `--source-html-out` | 额外抓取**整页服务器渲染 HTML**（`page.canonicalurl`）并写入该路径 |
| `--wikitext-out` | wikitext 输出路径。默认 `OUT` 同名的 `.wiki`；输出到 stdout 时 wikitext 内嵌进 JSON |
| `--user-agent` | Wikimedia 合规 UA，建议含项目/联系方式 |
| `--link-limit` | 出链保留上限，`0`（默认）= 不限 |
| `--category-limit` | 分类上限，默认 500，`0` = 不限 |
| `--langlink-limit` | 跨语言链接上限，默认 300，`0` = 不限 |
| `--image-limit` | 图片上限，默认 100，`0` = 不限 |
| `--search-limit` | 搜索候选条数，默认 5 |
| `--proxy` | HTTP 代理；默认读环境变量 `https_proxy/HTTPS_PROXY/http_proxy/all_proxy` |
| `--strict` | 任何步骤失败都直接抛出（不再把可选步骤降级为 `warnings`），用于排查 bug |

## 输出文件

- `<out>.json`：主结构化结果（见下）。
- `<out>.html`：`action=parse&prop=text` 得到的**完整文章正文内容**（`mw-parser-output`：信息框 + 全部章节 + 脚注 + 导航框等）。注意它是**完整文章内容**，但不是完整网页文档——不含 `<head>`、站点皮肤、侧栏、顶部导航。
- `<out>.wiki`：当前版本 **wikitext 原文**（侧车文件；此时 JSON 里只有 `content.wikitext_file`）。
- `--source-html-out`：整页渲染 HTML（此时 JSON 有 `content.source_html_file`）。

> 写到文件时 wikitext 走侧车；只输出到 stdout 时 wikitext 内嵌为 `content.wikitext`，避免丢内容。

所有输出路径（包括自动生成的侧车路径）必须互不冲突；路径重名时会在抓取前报错并退出，返回码为 `2`。

## 返回码

| 码 | 含义 |
|---|---|
| `0` | 抓取成功且页面存在（`exists=true`） |
| `1` | 页面不存在（`exists=false` 且无 `error`） |
| `2` | 抓取过程出错（顶层有 `error`） |

## JSON 结构总览

```jsonc
{
  "query": {...},
  "exists": true,
  "page": {...},
  "page_meta": {...},
  "content": {...},
  "sections": [...],
  "links": {"items": [...], "by_namespace": {...}},
  "categories": {...},
  "category_info": {...},
  "langlinks": {...},
  "images": {...},
  "image_info": {...},
  "external_links": [...],
  "references": [...],
  "tables": [...],
  "infobox": {...},
  "search_results": [...],
  "fetched_at": "...",
  "warnings": [...]          // 仅当有可选步骤失败时出现
}
```

页面不存在时（`exists=false`）只返回 `query / exists / search_results / warnings? / fetched_at`；抓取出错时额外有 `error`。

---

## 字段详解

### `query`
本次请求参数回显。

| 字段 | 含义 |
|---|---|
| `keyword` | 用户输入的原始关键词 |
| `requested_title` | 实际请求的标题（可能经搜索修正） |
| `language` | 语言代码 |
| `proxy` | 使用的代理地址，已移除认证信息（无则 `null`；无法解析时为 `<redacted>`） |

### `exists`
页面是否存在。注意区分：

- `exists=false` 且**无** `error` → 页面确实不存在（返回码 1）。
- 有顶层 `error` → 抓取**失败**（网络/API 异常，返回码 2）。

### `error`（仅在抓取抛异常时出现）
```jsonc
{"type": "...", "message": "...", "step": "page.construct", "causes": [{"type":"...","message":"..."}]}
```
- `step`：出错的步骤名（如 `page.construct`、`links`、`content.html.parse`）。
- `causes`：底层异常链（`__cause__` 逐层）。

### `page`
页面身份信息。

| 字段 | 含义 |
|---|---|
| `title` | 页面标题 |
| `displaytitle` | 显示用标题（可能含 HTML） |
| `pageid` | MediaWiki 页面 ID |
| `namespace` | 命名空间编号（0=主条目） |
| `language` | 语言代码 |
| `fullurl` / `canonicalurl` | 完整 URL / 规范 URL |

### `page_meta`
页面级元数据（pageprops / pageimages / info / revisions）。

| 字段 | 含义 |
|---|---|
| `wikibase_item` | Wikidata QID（如 `Q1191183`），可用于关联结构化数据 |
| `defaultsort` | 默认排序键 |
| `page_image_free` | 页面代表图文件名（自由版权） |
| `disambiguation` | **本页本身**是否为消歧义页（布尔） |
| `contentmodel` | 内容模型（通常 `wikitext`） |
| `pagelanguage` | 页面语言代码 |
| `lastrevid` | 最近修订 revid |
| `touched` | 缓存刷新时间 |
| `length` | 页面字节数 |
| `redirect` | 本页作为重定向的信息标记（通常为 `null`，因抓取已跟随重定向） |
| `protection` | 保护级别列表 |
| `displaytitle` | 显示标题 |
| `page_image` | 代表图信息：`{name, original:{source,width,height}, thumbnail:{...}}` |
| `revision` | 末次修订：`{revid,parentid,timestamp,size,user,comment}` |
| `redirected_from` | 若 `--keyword` 是重定向，则为**源标题**，否则 `null` |
| `redirect_target` | 重定向**目标标题**，否则 `null` |

> ⚠️ `disambiguation` 只描述**本次抓取的页面本身**，不含其出链目标是否为消歧义页。

### `content`
| 字段 | 含义 |
|---|---|
| `summary` | 导语/摘要（`page.summary`） |
| `plaintext` | **整篇纯文本**（TextExtracts，覆盖全部章节） |
| `html_file` | 完整正文 HTML 文件路径（若已写出；含全部章节，不含站点外壳） |
| `wikitext_file` | wikitext 侧车文件路径（写文件时） |
| `wikitext` | wikitext 原文（仅输出到 stdout 时内嵌） |
| `source_html_file` | 整页渲染 HTML（含 head/皮肤/导航）路径（仅 `--source-html-out`） |

> 库用法注意：直接调用 `WikipediaFetcher.fetch()`（而非 CLI）时，结果还会额外包含 `_html`（正文 HTML 字符串）。CLI 的 `main()` 会把它 `pop` 出去写文件，所以**写出的 JSON 文件里没有 `_html`**。

### `sections`
按 wiki 标题（h2–h6）构建的**嵌套章节树**。每节：

| 字段 | 含义 |
|---|---|
| `title` | 标题纯文本 |
| `id` | 标题锚点（`<hN id="...">`，已解码 HTML 实体），可拼 `页面URL#id` |
| `level` | 层级：h2→`1`，h3→`2`……（即标签序号 − 1） |
| `text` | 本节正文纯文本（**不含**子节内容） |
| `sections` | 子节数组（同结构） |

### `links`
页面内链，两种视图：

- `links.items`：从正文 HTML 解析出的**按原文顺序**的链接出现（去重后）。
- `links.by_namespace`：来自 MediaWiki `prop=links` 的出链，按命名空间分组。

`items[]`：

| 字段 | 含义 |
|---|---|
| `anchor` | 正文里**显示的文字**（piped 链接时与 `title` 不同） |
| `title` | 链接的**精确目标标题**（可能带消歧义后缀，如 `藤原寛子 (藤原道長女)`） |
| `href` | 原始链接（已做 HTML/URL 解码） |
| `redlink` | **红链**标记，见下方“非直观语义” |
| `count` | 同一 `(title, anchor)` 在本页出现次数 |

`by_namespace[lang][ns]`：`{namespace, namespace_name, titles[], count}`；`titles` 按字母排序，受 `--link-limit` 约束（`0`=不限）。

> ⚠️ `--link-limit` 对两种视图含义不同：`items` 是“去重后按原文顺序截断”，`by_namespace` 是“API 出链按字母序截断”。需要严格按 limit 控制时以 `items` 为准。

### `categories` / `category_info`
- `categories`：分类按命名空间分组（结构同 `by_namespace`）。
- `category_info`：每个分类的补充信息，key 为分类标题：

| 字段 | 含义 |
|---|---|
| `sortkey` | 分类排序键。MediaWiki 原文是 **UTF-8 十六进制**，脚本已解码为文本；若为二进制/非 UTF-8，则**回退为原始十六进制串**（可能含换行，形如 `默认键\n頁面标题`） |
| `hidden` | 是否为**隐藏分类**（维护类） |
| `namespace` | 命名空间（通常 14） |

### `langlinks`
跨语言链接：`{ "<lang>": [ {"title", "fullurl"} ] }`。

### `images` / `image_info`
- `images`：图片按命名空间分组（结构同 `by_namespace`），只含文件标题。
- `image_info`：key 为文件标题，值为媒体详情（来自 `imageinfo`）：

| 字段 | 含义 |
|---|---|
| `url` | 原图 URL |
| `width` / `height` | 尺寸（像素） |
| `mime` | MIME 类型 |
| `byte_size` | 文件字节数 |
| `artist` / `credit` | 作者 / 来源 |
| `license` / `license_url` | 许可名称 / 链接 |
| `description` / `object_name` | 描述 / 对象名 |
| `date_original` | 原始日期 |

（`artist` 等来自 extmetadata，可能缺失。）

### `external_links`
页面的**外部链接 URL 列表**（API `extlinks`，去重）。与 `links`（站内 wiki 链）不同。

### `references`
脚注/引用（从渲染 HTML 的 `<ol class="references">` 解析）。

| 字段 | 含义 |
|---|---|
| `id` | 脚注定义锚点，如 `cite_note-2` |
| `group` | 脚注分组；`"注釈"` 表示注釈组，`null` 表示通常的“出典/引用”组 |
| `backlinks` | 正文中引用此脚注的 `cite_ref-...` 锚点（可多个） |
| `urls` | 此脚注内出现的外部 URL |
| `text` | 脚注渲染文本（纯文本） |

### `tables`
**正文表格**（已排除 infobox；infobox 见下节）。每表：

| 字段 | 含义 |
|---|---|
| `id` | `<table id>`（可为空） |
| `classes` | class 列表（如 `wikitable`、`navbox-inner`） |
| `caption` | 表标题（可空） |
| `rows` | 行数组；每行是单元格数组 |

单元格：

| 字段 | 含义 |
|---|---|
| `text` | 单元格纯文本 |
| `header` | 是否表头（`th`） |
| `colspan` / `rowspan` | 合并列/行（无则为 `null`） |
| `links` | 单元格内的 wiki 链接标题列表 |

### `infobox`
首个 `class` 含 `infobox` 的表格，结构化为：

| 字段 | 含义 |
|---|---|
| `caption` | 表格 caption（可空） |
| `rows` | 行数组，元素为下列之一 |

行元素形态：

- `{"header": "..."}`：跨列表头/分组标题。
- `{"label": "...", "value": "..."}`：键值对（如 `生誕` / `康保3年…`）。
- `{"value": "..."}`：无标签的行（如图片说明）。

> ja-wiki 的首个 `header` 常混入装饰字样（如“凡例”），属原样数据。

### `search_results`
搜索候选（`page 不存在` 的兜底也用它）。

| 字段 | 含义 |
|---|---|
| `title` | 结果标题 |
| `snippet` | 摘要，**已去除 HTML 标签** |
| `wordcount` / `size` / `timestamp` | 词数 / 字节数 / 时间戳 |

> 默认模式下，若搜索这一步失败，`search_results` 可能是 `[{"error": "..."}]`（而非标题列表）；使用 `--strict` 时会抛出异常。

### `warnings`
仅当**可选步骤**失败时出现，不影响主体。每项：`{step, type, message, causes}`。

HTTP 200 响应中的 MediaWiki `error` 对象也视为步骤失败：默认记录 `type: "MediaWikiAPIError"` 的警告，使用 `--strict` 时抛出异常；错误消息包含 API 错误码及说明。

### `fetched_at`
抓取时间（UTC，ISO 8601）。

---

## 非直观语义（重点）

1. **红链 `links.items[].redlink = true`**
   表示该链接在源页面是“红链”，即**目标页面不存在**（尚未创建）。其 `href` 形如
   `/w/index.php?title=...&action=edit&redlink=1`。
   → 顺着跳转前即可知道“此目标无正文可抓”。

2. **`links.items[].title` 是精确目标标题**
   源页面链接常带消歧义后缀（如 `藤原寛子 (藤原道長女)`），这类目标通常是**正常页面**；
   而裸标题（如 `藤原寛子`）本身可能是消歧义页。请优先使用 `title` 作为抓取目标。

3. **“目标是否为消歧义页”当前输出无法直接判断**
   `redlink` 只代表“目标不存在”，**不等于**消歧义。`page_meta.disambiguation` 只描述**本页**。
   若需判断某出链目标是否为消歧义页，需另行 `prop=pageprops`（`disambiguation`）查询。
   （这是当前实现的已知边界，详见“局限”。）

4. **`sections[].level`**：`h2→1, h3→2 …`，即 `标签层级 − 1`；并非原始 `hN` 数字。

5. **`references[].group`**：`"注釈"` 为注釈组，`null` 为普通出典/引用组；两者在 HTML 中是不同的 `<ol class="references">`。

6. **`category_info.sortkey` 的解码**：MediaWiki 以十六进制返回；脚本按 UTF-8 解码，失败则保留十六进制原文（二进制排序键）。因此值可能是可读文本，也可能是 hex。

7. **`tables` 不含 infobox**：infobox 单独在 `infobox`；正文表格里也可能含 `navbox` 等导航框（看 `classes`）。

8. **`images` 只有标题，`image_info` 才有 URL/许可**；两处 key 均为文件标题（如 `ファイル:...`）。

9. **`external_links` 是外部 URL；`links` 是站内 wiki 链**，二者不同。

10. **wikitext 在哪**：`--out` 写文件时在 `.wiki` 侧车（`content.wikitext_file`）；仅 stdout 时内嵌 `content.wikitext`。

11. **`html_file` 是完整文章正文**（`action=parse&prop=text`，含信息框/全部章节/脚注/导航框），但**不是完整网页文档**（无 `<head>`、无站点皮肤/导航）；要整页源码用 `--source-html-out`。

## 局限 / 已知边界

- **单页工具**：不构建关系图、不做多页递归、不判定出链目标的消歧义/存在性（红链除外，源页已标）。
- **不做业务语义**：关系（父母/子女等）是 `infobox` / `sections` 中的**原文串**，由使用方自行解析。
- **引用解析**以 `</ol>` 为边界，极少数引用内含嵌套列表时可能被截断。
- **消歧义页**会被正常抓取；此时 `page_meta.disambiguation=true`，但不会替使用方挑选具体义项。
