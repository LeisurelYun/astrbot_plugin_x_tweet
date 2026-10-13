# astrbot_plugin_x_tweet

AstrBot 插件：自动解析 **X / Twitter** 链接与个人主页，并注册 LLM 工具，让 AI 能总结、翻译推文或检索 X 上的最新内容。

> **不依赖 X 官方 API。数据源为 [FxTwitter](https://docs.fxembed.com/) 公开接口，并以 Syndication CDN、Twitter oEmbed 作为备用源。**

> 遍地罗绮者，不是养蚕人。

**注意！由于平台的特殊性质，建议你配置好黑名单（关键词和作者），由你使用本插件造成的一切后果由你本人负责。**

**默认屏蔽色情、赌博、广告拉群类内容，可按需增删。**

## 功能

- **自动解析**：群聊 / 私聊里发 `x.com/…/status/…` 链接，自动回正文 + 图片 + 视频
- **引用 / 回复贴**：被引用、被回复的主贴会**单独发一条消息**并完整解析（含译文与媒体）
- **楼中楼**：一条推文往上追溯主贴链，可交付为合并转发（每层一个节点）/ 逐层引用气泡 / X 风格卡片长图
- **X 风格卡片**：推文可渲染成还原 X 观感的卡片图（头像 / 昵称 / 三色认证徽章 / 日期 / 正文 /
  媒体占位提示 / 引用区域 / 互动栏），支持浅色与深色主题；卡片不内嵌媒体，图片进合并转发、视频单独发送
- **个人主页**：发 `https://x.com/<账号>` 即出资料卡，可交付为 X 风格主页卡片（横幅 / 大头像 /
  简介 / 位置网站加入时间 / 关注数 / 最近推文，只含本人发的）或纯文字资料卡
- **X 搜索**：`/xt 搜索 <关键词>` 或让 AI 调用 `search_x_posts`；账号列表为空时按关键词自动匹配相关账号；只返回账号本人发的推文（不含转帖）
- **翻译**：优先用 FxTwitter 免费翻译（25 种语言），偶发不给译文时先重试，仍拿不到才回退大模型
- **黑名单**：按作者或关键词跳过解析，关键词黑名单同时作用于搜索结果
- **图片尺寸四档**：`orig` / `large` / `medium` / `small`
- **视频**：优先 MP4 直链，无直链时异步调用本机 `yt-dlp`
- **贴表情**：解析成功后给原消息贴一个表情回应
- **缓存**：媒体与解析结果持久化在 `data/plugin_data/astrbot_plugin_x_tweet/cache`，可定时或定量清理

## 安装

把插件目录放到 AstrBot 的 `data/plugins/` 下，重启 AstrBot。首次加载会依据
`requirements.txt` 自动安装 `httpx` 与 `beautifulsoup4`（`beautifulsoup4` 缺失时退化为正则解析，不影响运行）。

**前置条件：**

- **网络**（建议）：FxTwitter 在国内需要**代理**。系统已设 `HTTPS_PROXY` 时插件自动沿用，也可在配置里填 `proxy`。
- **yt-dlp**（建议）：仅当接口没给 MP4 直链时才用。会自动探测 PATH，也可在配置里指定路径。

## 使用

| 场景           | 操作                                                                    |
| -------------- | ----------------------------------------------------------------------- |
| 自动解析       | 直接发链接（群聊 / 私聊均可，无需 @ 机器人）                            |
| 引用 / 回复贴  | 直接发链接；主贴会自动单独发一条                                        |
| 楼中楼         | 直接发最底下那条的链接                                                  |
| 个人主页       | 发 `https://x.com/elonmusk`，或用 `/xt 主页 elonmusk`                   |
| AI 总结 / 翻译 | 发链接后对机器人说「总结这条推文」「翻译一下」，AI 调用 `parse_x_tweet` |
| AI 搜 X        | 「X 上最近有什么关于 Grok 的消息」，AI 调用 `search_x_posts`            |
| 搜索           | `/xt 搜索 Grok`，或 `/xt 搜索 from:elonmusk 星舰 -火星 min_faves:5000`  |
| 搜索语法       | `/xt 搜索`（无参）或 `/xt help` —— 发送语法说明图片                     |
| 查看状态       | `/xt 状态`                                                              |
| 临时换翻译语言 | `/xt 翻译 ja`（只影响当前会话，发 `/xt 翻译 默认` 恢复）                |
| 清理缓存       | `/xt 清理`（仅管理员）                                                  |

支持链接形式：`x.com`、`twitter.com`、`mobile.twitter.com`、`/i/status/`、`/statuses/`、`nitter.*`、
`fxtwitter.com`、`vxtwitter.com`、`fixupx.com`，以及 `t.co` 短链。链接后的 `?s=20` 等参数会被剔除。

## 配置项（`_conf_schema.json`，57 项）

基础与解析源：

| 键                      | 默认                                     | 说明                                                        |
| ----------------------- | ---------------------------------------- | ----------------------------------------------------------- |
| `enabled`               | `true`                                   | 是否启用自动解析                                            |
| `proxy`                 | 空                                       | HTTP 代理，留空则跟随系统环境变量                           |
| `parse_source_order`    | `fxtwitter, mirror, syndication, oembed` | 解析源优先级（文本，逗号分隔）；未列出的源自动补到末尾      |
| `enable_mirror_sources` | `true`                                   | 是否轮询镜像实例                                            |
| `mirror_fx_instances`   | `[]`                                     | 镜像实例列表，一行一个；兼容 FxTwitter 同构与 vx 系两种格式 |

媒体：

| 键                                  | 默认        | 说明                                  |
| ----------------------------------- | ----------- | ------------------------------------- |
| `send_images` / `send_videos`       | `true`      | 是否发送图片 / 视频                   |
| `image_size`                        | `medium`    | `orig` / `large` / `medium` / `small` |
| `max_images`                        | `4`         | 单条推文最多发几张图                  |
| `max_image_size` / `max_video_size` | `10` / `50` | 大小上限（MB），超限降级为发链接      |
| `yt_dlp_path` / `cookie_file`       | 空          | 自定义 yt-dlp 路径 / cookies 文件     |
| `download_timeout`                  | `180`       | 视频下载超时（秒）                    |

正文与翻译：

| 键                        | 默认    | 说明                                                        |
| ------------------------- | ------- | ----------------------------------------------------------- |
| `text_content_mode`       | `both`  | 正文展示：`both` / `original` / `translation`               |
| `translate_mode`          | `auto`  | `off` / `auto` / `fxtwitter` / `llm`                        |
| `translate_target_lang`   | `zh-cn` | 目标语言，支持 25 种，也可写「中文」「日语」                |
| `translate_provider_id`   | 留空    | 回退翻译用的大模型（下拉选择），留空 = 自动用第一个可用模型 |
| `translate_timeout`       | `30`    | 大模型翻译超时（秒）                                        |
| `translate_llm_fallback`  | `true`  | 免费翻译没给译文时是否回退大模型                            |
| `translate_retry`         | `2`     | 免费译文缺失时的重试次数（0–3）                             |
| `translate_llm_min_chars` | `4`     | 有效字符少于该值不调用大模型（0 = 不限制）                  |

交付与卡片：

| 键                                            | 默认       | 说明                                                                |
| --------------------------------------------- | ---------- | ------------------------------------------------------------------- |
| `tweet_delivery`                              | `media`    | `media`（文字 + 媒体混排）/ `card`（X 风格卡片，媒体另发）          |
| `card_theme`                                  | `auto`     | 卡片配色：`auto` / `light` / `dark`                                 |
| `card_dark_start_hour` / `card_dark_end_hour` | `19` / `7` | `auto` 模式的深色时段（可跨夜）                                     |
| `thread_split`                                | `true`     | 引用 / 回复的主贴单独发一条（`card` 模式下不生效）                  |
| `thread_chain_depth`                          | `4`        | 楼中楼最多向上解析几层（1–10）                                      |
| `thread_delivery`                             | `forward`  | `forward`（合并转发）/ `bubble`（逐层引用气泡）/ `card`（一张长图） |
| `thread_merge_min_nodes`                      | `3`        | 楼层数达到几条用合并转发（2–10）                                    |
| `thread_merge_media`                          | `true`     | 合并转发每层是否带上图片                                            |
| `thread_forward_quote`                        | `true`     | 合并转发节点内是否显示「↩ 回复 @上一层」引用块                      |

搜索：

| 键                             | 默认      | 说明                                                         |
| ------------------------------ | --------- | ------------------------------------------------------------ |
| `search_enabled`               | `true`    | 启用 X 搜索                                                  |
| `search_accounts`              | `[]`      | 要检索的账号列表；**留空即按关键词自动匹配相关账号**         |
| `search_account_limit`         | `5`       | 单次搜索最多查询几个账号（1–10）                             |
| `search_count`                 | `3`       | 单次最多返回条数（上限 5）                                   |
| `search_delivery`              | `forward` | `forward`（合并转发）/ `text`（纯文本）/ `image`（卡片图片） |
| `search_translate`             | `false`   | 搜索结果是否补译文                                           |
| `search_translate_concurrency` | `3`       | 补译文的并发上限（1–10）                                     |
| `search_max_age_hours`         | `0`       | 只取最近 N 小时内的推文，`0` = 不限                          |
| `search_with_replies`          | `true`    | 是否包含回复                                                 |
| `search_include_media`         | `true`    | 结果里是否附带媒体链接                                       |
| `search_required_keywords`     | `[]`      | 白名单：必须命中其一                                         |

主页与黑名单：

| 键                   | 默认   | 说明                                                           |
| -------------------- | ------ | -------------------------------------------------------------- |
| `profile_enabled`    | `true` | 解析 X 个人主页链接                                            |
| `profile_delivery`   | `card` | 主页发送方式：`card` = X 风格主页卡片，`text` = 纯文字资料卡   |
| `profile_post_count` | `3`    | 主页资料卡附带最近推文条数（0–10）                             |
| `blocked_keywords`   | 见下   | 黑名单关键词，解析与搜索共用；默认屏蔽色情 / 赌博 / 广告拉群类 |
| `blocked_authors`    | `[]`   | 作者黑名单，如 `elonmusk`、`@OpenAI`                           |

交互与缓存：

| 键                         | 默认              | 说明                                                                 |
| -------------------------- | ----------------- | -------------------------------------------------------------------- |
| `emoji_reply` / `emoji_id` | `true` / `128077` | 贴表情开关与表情 ID（3 位编码可填 `144`）                            |
| `show_stats_footer`        | `true`            | 显示点赞 / 转发 / 回复 / 浏览量                                      |
| `send_error_hint`          | `true`            | 解析失败时在群里发简短提示                                           |
| `cooldown_seconds`         | `10`              | 同群同链接冷却秒数                                                   |
| `request_timeout`          | `20`              | 单次请求超时（秒）                                                   |
| `cache_dir`                | 空                | 自定义缓存目录，默认 `data/plugin_data/astrbot_plugin_x_tweet/cache` |
| `cache_max_files`          | `300`             | 缓存文件数上限                                                       |
| `cache_cleanup_interval`   | `0`               | 定时清理间隔（小时），`0` = 关闭                                     |

搜索语法（`/xt 搜索`，也可让 AI 直接自然语言检索）：

| 写法             | 作用                                                         |
| ---------------- | ------------------------------------------------------------ |
| `from:elonmusk`  | 只检索该账号（可多个，跳过自动匹配）；`@elonmusk` 旧写法等价 |
| `星舰 火箭`      | 关键词，任一命中即返回（大小写无关）                         |
| `-火星`          | 排除词，命中即丢弃（可多个）                                 |
| `min_faves:5000` | 只要点赞数 ≥ 5000 的推文                                     |

说明：只统计账号**本人发布**的推文——FxTwitter 的时间线接口会把转帖一并返回
（转帖的作者是原账号），这类内容会被过滤掉，避免结果的「作者」张冠李戴。

## LLM 工具

| 工具             | 参数                                                                    | 返回                                                                |
| ---------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `parse_x_tweet`  | `tweet_url`(string) —— 推文或主页链接，留空则回溯本会话最近解析过的推文 | 结构化 JSON：正文 / 作者 / 译文 / 媒体 / 引用 / 父子关系 / 互动数据 |
| `search_x_posts` | `keyword`(string)、`accounts`(string，逗号分隔)、`limit`(int)           | 搜索结果 JSON（作者、正文、时间、链接、互动数据）                   |

- 工具只返回 JSON 字符串，不自己发消息。
- 命中解析黑名单时返回 `blocked: true` 且不含正文。

## 已知限制

- **私有账号**、**已删除推文**、**仅登录可见**的内容**无法解析**，插件会给出简短中文提示。
- 解析按**可配置的源顺序**取数，各源的数据完整度不同：
  - FxTwitter 数据最全（含转发数、浏览量、自带翻译）；
  - Syndication CDN 不返回转发数与浏览量，超长推文只返回截断后的正文，也没有自带翻译。
- 搜索基于账号时间线本地过滤，不是全站搜索：FxTwitter 的 `/2/search` 不对普通调用者开放。
- 楼中楼靠推文自身的引用 / 回复关系向上追溯，追溯不到时只发已取到的楼层。
- 合并转发（`Node` / `Nodes`）只有**部分适配器**支持，不支持时自动退回分条发送；
  合成的转发节点没有真实消息 ID，层间用节点顺序与 `📌 第 k 层` 标签表示，视频放不进节点、始终单独发送。
- 卡片渲染依赖 AstrBot 文转图服务，失败时按「卡片 → 本地文转图 → 图文混排」逐级回退。

## 文件说明

| 文件                                     | 说明                                                          |
| ---------------------------------------- | ------------------------------------------------------------- |
| `main.py`                                | 插件主体 `XTweetPlugin`：事件处理、消息渲染、缓存与指令       |
| `x_tweet_core.py`                        | 常量、正则、纯工具函数、`TweetData` / `ProfileData`、卡片模板 |
| `x_tweet_parser.py`                      | `XTweetParser`（多源解析）与 `Translator`（大模型翻译）       |
| `x_tweet_media.py`                       | `MediaDownloader`：图片 / 视频下载、yt-dlp 调用、缓存清理     |
| `_conf_schema.json`                      | 面板配置项定义（57 项）                                       |
| `metadata.yaml`                          | 插件元信息（名称、版本、帮助文本）                            |
| `assets/search_help.png`                 | `/xt 搜索` 语法说明图片（静态资源）                           |
| `requirements.txt`                       | 依赖：`httpx`、`beautifulsoup4`                               |
| `logo.png`                               | 插件图标（256×256）                                           |
| `ruff.toml`                              | 代码风格约定（`line-length = 120`）                           |
| `pyrightconfig.json`                     | 编辑器（Pylance）静态分析配置，仅影响 IDE 提示，与运行无关    |
| `README.md` / `CHANGELOG.md` / `LICENSE` | 文档、版本记录、MIT 许可                                      |

## 开发约定

- 不引入 `requests`，全部走 `httpx.AsyncClient`（超时、代理统一在 `__init__` 配置）。
- 可调参数一律进 `_conf_schema.json`，代码里用 `_cfg_str` / `_cfg_int` / `_cfg_bool` 读取并带默认值。
- 面向用户的异常都在插件内兜住并转成中文提示，不把堆栈抛回 AstrBot。
- 缓存与媒体一律写在 AstrBot `data/` 目录下。
- 代码分四个模块（`main.py` / `x_tweet_core.py` / `x_tweet_parser.py` / `x_tweet_media.py`），
  子模块用「包内相对导入 + 平铺导入」双模式写法，AstrBot 与测试脚本两种加载方式都能跑。
- 单行不超过 120 字符（见 `ruff.toml`）。

## 许可

[MIT](LICENSE)。使用到的第三方服务（`FxTwitter`、`pbs.twimg.com`、`yt-dlp`）各自遵循其自身条款。

## 其他

> 本项目使用了`Vibe Coding` · 参与的模型有：`Qwen 3.8 MAX` | `DeepSeek V4.1 Flash` | `Step 5 Preview `|` Hy4 Preview`

感谢这些模型的参与和付出。

感谢您看到这里。
