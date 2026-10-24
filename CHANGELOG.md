# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [v1.14.1] - 2026-10-25

### 修复

- 日志里的「空串报错」：httpx 0.28.1 在 Twitter CDN 直接掐断 TLS 连接时抛出的是
  消息为空串的 `ConnectError('')`，插件原来把 `{exc}` 直接拼进日志，于是刷出
  「图片下载失败（）」「FxTwitter 接口请求失败：」这种不带任何信息的残废报错，
  出了问题也没法判断是连接被断还是别的。新增 `exc_text()` 统一格式化异常
  （消息为空时回退为异常类型名），所有网络类报错点均已切换，日志现在能看出
  是 ConnectError 还是 Timeout。
- 插件重载竞态：原来 `terminate()` 会立刻关闭共享的 HTTP 客户端，事件总线里
  旧实例残留的在途任务随后发出的请求全部打到已关闭的客户端上，日志里表现为
  一连串 `Cannot send a request, as the client has been closed.`（重载插件后
  十来秒内集中出现）。现在 terminate 先置终止标志——在途任务不再发起新请求、
  媒体下载直接降级为发原链接——再有界等待在途任务收尾，最后才关。

### 变更

- 媒体下载与 FxTwitter 请求新增瞬时网络错误重试：`httpx.NetworkError` /
  `httpx.ProtocolError`（被掐断的 TLS 连接，即此前空串报错的真身）不再
  失败就降级——图片 / 视频直链最多重试 2 次（间隔 1s / 2s），FxTwitter 主
  请求重试 1 次。`TimeoutException` 不重试。
- 「未配置任何镜像实例」从「每次解析失败都刷一条 WARN」降为 DEBUG。

## [v1.14.0] - 2026-10-13

### 维护

- 遵循 AstrBot 插件商店审查意见：移除 Python 内置日志模块的残留（`x_tweet_core.py`
  里未使用的导入、`main.py` 末尾取日志器的死语句）。日志统一走
  `from astrbot.api import logger`，其余不变。
- 在 `cache_dir` 的读取处与配置项说明里补充注释：默认缓存到 AstrBot 规范的
  `data/plugin_data` 下；开放自定义路径是考虑到部分用户磁盘空间不足，
  属用户显式配置，不在本版改变该行为。

## [v1.13.1] - 2026-10-12

### 修复

- 楼中楼卡片交付时不再吞掉各层媒体：`thread_delivery=card` 现在会把 ladder 里每一层的
  图片按楼层顺序进一条合并转发、每一层的视频单独发送（一级主贴的图片此前整条丢失）。
  命中黑名单的楼层仍不发媒体；卡片渲染失败回退合并转发的路径不变。
- 个人主页的「最近推文」不再混入他人的转帖：FxTwitter 的 `/2/profile/{handle}/statuses`
  返回的转帖 author 是原作者，现在按 `reposted_by` 与 author 双重校验只保留本人发的推文。
- 搜索结果（`/xt 搜索` 与 `search_x_posts` 工具）同样过滤掉转帖：转帖的作者是原作者，
  混进来会让结果的「作者」张冠李戴；相应地把时间线请求条数放宽到 `search_count × 3`，
  保证过滤后仍凑得满目标条数。

### 变更

- 楼中楼卡片路径补齐了缓存写入、解析计数与「会话最近推文」记录（原先只有合并转发 /
  引用气泡两条路径会做）。
- 主页最近推文的请求条数放宽（转帖会被过滤掉，多要一些才够填满 `profile_post_count`）。

### 维护

- 删除未被调用的 `XTweetParser.ping()` 与其专用常量 `FXTWITTER_PING_API`。
- README 里的配置项数量同步为 57 项（`profile_delivery` 加入后漏改）。

## [v1.13.0] - 2026-10-12

### 新增

- 个人主页解析支持 X 风格卡片：横幅、大头像、昵称与认证徽章、简介、位置 / 网站 / 加入时间、关注数、标签页与最近推文列表，配色跟随 `card_theme`。
- `profile_delivery`：主页的发送方式，可选 X 风格卡片（默认）或纯文字资料卡。

### 变更

- 主页的「加入时间」由原始 ISO 时间串改为 X 样式的「2010年3月」。
- 主页卡片的推文条目不再带「（见下方）」提示（主页卡片不发送推文媒体）。
- 配置项共 57 项。

## [v1.12.0] - 2026-10-12

### 新增

- 卡片主题「自动」的深色时段可配置：`card_dark_start_hour` / `card_dark_end_hour`。

### 变更

- 配置项精简与重排：
  - `thread_reply_bubble` 并入 `thread_delivery`（三档：`forward` / `bubble` / `card`）；
  - 移除 `search_auto_accounts`，账号列表为空即自动匹配；
  - `search_max_accounts` 与 `search_auto_account_count` 合并为 `search_account_limit`；
  - `search_blocked_keywords` 与 `parse_blocked_keywords` 合并为 `blocked_keywords`；
  - `parse_blocked_authors` 改名为 `blocked_authors`。
- 重写默认黑名单词表（色情 / 赌博 / 广告拉群类）。
- 配置项按用途重新排序，共 56 项。

## [v1.11.0] - 2026-10-12

### 新增

- `card_theme`：卡片浅色 / 深色 / 自动（按时段切换）。
- 卡片每个节点右上角显示 X 标识。

### 变更

- 卡片按 2 倍分辨率渲染，文字更清晰。
- 互动栏四项始终显示，数据缺失时按 0。
- 裁白边按图片自身底色判断，深色主题同样生效。

## [v1.10.0] - 2026-10-12

### 新增

- 楼中楼卡片交付：`thread_delivery` 新增 `card`，整条楼渲染成一张长图，楼层之间用灰色竖线连接。
- 三色认证徽章：按 `author.verification.type` 区分蓝（个人）/ 金（商家组织）/ 灰（政府官方）。
- 互动栏改用 X 的 SVG 图标。

### 变更

- `thread_delivery` 移除 `both`，改为 `forward`（默认）/ `bubble` / `card`。
- 卡片模板合并为单个 `X_CARD_TMPL`，单推文 / 楼中楼 / 搜索结果共用。
- 默认缓存目录改为 AstrBot 规范位置 `data/plugin_data/astrbot_plugin_x_tweet/cache`。

## [v1.9.0] - 2026-10-12

### 新增

- `tweet_delivery`：推文可渲染成 X 风格卡片（头像 / 昵称 / 蓝V / 日期 / 正文 / 媒体占位提示 /
  引用区域 / 互动栏），卡片不内嵌媒体，图片进合并转发、视频单独发送。
- 搜索结果卡片改用 X 风格。

### 变更

- `parse_source_order` 改为文本类型（英文逗号分隔）。

## [v1.8.0] - 2026-10-12

### 新增

- `parse_source_order`：解析源优先级顺序可配置，未列出的源自动补到末尾。
- 镜像源兼容 vx 系格式（vxtwitter 等），请求统一带 `Referer`。

### 修复

- 「只发译文」模式下，原文已是目标语言时不再误报「翻译不可用」。

## [v1.7.0] - 2026-10-12

### 新增

- `search_delivery`：搜索结果可合并转发（默认）/ 图片卡片 / 纯文本。
- `search_translate`：搜索结果补译文（免费优先 + 大模型兜底，默认关闭）。

### 变更

- `search_count` 默认 10 → 3，上限 30 → 5。

## [v1.6.0] - 2026-10-12

### 新增

- Syndication CDN 备用源（X 官方嵌入端点，无需密钥）。
- 镜像实例轮询：`enable_mirror_sources` / `mirror_fx_instances`。
- typeahead 结果内存缓存 15 分钟。
- 搜索指令语法：`from:` / `-排除词` / `min_faves:N`，以及搜索语法帮助图。

### 变更

- 解析降级链：FxTwitter → 镜像轮询 → Syndication → oEmbed。
- 多账号搜索改为 `asyncio.gather` 并发拉取。

## [v1.5.2] - 2026-10-12

### 新增

- `thread_forward_quote`：合并转发节点内显示「↩ 回复 @上一层」引用块。
- 搜索账号自动匹配（`search_auto_accounts` / `search_auto_account_count`）。

## [v1.5.1] - 2026-10-12

### 修复

- 媒体超限异常未定义、缓存目录不可写导致插件加载失败、主页时间线请求异常未捕获、默认翻译模型硬编码。
- 缓存文件名改用 `stable_digest`（与进程无关）；缓存读写移出事件循环；下载锁改用弱引用；
  `/xt 清理` 限管理员。

### 变更

- `main.py` 拆分为 core / parser / media 四个模块。
- `/xt 翻译` 改为会话级（不再影响其它群）。

## [v1.5.0] - 2026-10-11

### 新增

- `translate_retry`：免费译文缺失时先重试。
- `translate_llm_min_chars`：短文本不调用大模型。
- `thread_delivery`：楼中楼交付方式可选（气泡 / 合并转发）。
- 命中黑名单的楼层改用占位说明，并继续向上追溯。

## [v1.4.0] - 2026-10-11

### 新增

- 楼中楼多层解析，支持「引用回复气泡」与「合并转发」两种交付。
- `/xt 状态` 与启动日志增加楼中楼信息。

## [v1.3.0] - 2026-10-11

### 新增

- `thread_split`：引用 / 回复的主贴单独发一条消息。
- X 个人主页解析（`profile_enabled` / `profile_post_count`）。
- 解析黑名单：作者与关键词。

## [v1.2.0] - 2026-10-11

### 新增

- 引用贴与回复贴解析（主贴正文、译文、媒体单独返回）。
- X 内容搜索：LLM 工具 `search_x_posts` 与 `/xt 搜索`。
- `text_content_mode`：正文三档展示（原文 + 译文 / 只发原文 / 只发译文）。
- `cache_cleanup_interval`：缓存定时清理。

### 变更

- 「翻译用大模型」改为服务商下拉选择（`select_provider`）。
- 搜索结果与解析结果统一用「万 / 亿」展示计数。

## [v1.1.0] - 2026-10-11

### 新增

- 贴表情：解析成功后给原消息加表情回应（默认 👍）。
- 图片尺寸四档：`orig` / `large` / `medium` / `small`。
- 推文翻译：非中文推文译成简体中文，译文附在原文下方并标注来源。
- 指令 `/xt 翻译 <语言>` 临时切换目标语言。

### 变更

- 解析接口升级到 FxTwitter v2。
- 自动解析支持私聊（装饰器改为 `ALL`）。

### 修复

- 修复「群聊 / 私聊发链接不解析」（handler 交付方式错误）。
- 修复 `/xt 翻译 ja` 不生效（参数解析顺序）。

## [v1.0.0] - 2026-10-10

### 新增

- 自动识别群内 X / Twitter 链接，多源降级解析（FxTwitter → oEmbed），不依赖 X 官方 API。
- 媒体发送：图片流式下载后发送，视频优先 MP4 直链、无直链时走本机 yt-dlp。
- 同群同链接冷却与内存缓存，正文 + 多图 + 视频合并为一条消息发送。
- LLM 工具 `parse_x_tweet`，指令 `/xt 状态` 与 `/xt 清理`。
