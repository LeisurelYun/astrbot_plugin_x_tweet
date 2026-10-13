# -*- coding: utf-8 -*-
"""
astrbot_plugin_x_tweet —— AstrBot 插件（入口）

功能：
    自动解析聊天中的 X / Twitter 推文链接，下载并发送正文、作者、图片与视频；
    同时注册 LLM 工具（Function Calling），支持让 AI 总结 / 翻译 / 解释推文。

说明：
    1. 不依赖 X 官方 API。数据源为 FxTwitter 公开 API，并以 Twitter oEmbed 作为纯文本兜底。
    2. 网络请求全部使用 httpx.AsyncClient，严禁使用 requests。
    3. 视频下载通过 asyncio.create_subprocess_exec 异步调用本地 yt-dlp。
    4. 缓存与媒体文件持久化在 AstrBot 的 data 目录下
       （默认 data/plugin_data/astrbot_plugin_x_tweet/cache，可在配置里改），
       插件更新不会丢失；缓存在内存中做一层加速。

代码结构（v1.5.1 起拆成四个模块，本文件只剩插件主体）：
    x_tweet_core.py    —— 常量与正则、纯工具函数、TweetData / ProfileData
    x_tweet_parser.py  —— XTweetParser（FxTwitter v2 → oEmbed → t.co 展开）、Translator
    x_tweet_media.py   —— MediaDownloader（图片 httpx 流式下载、视频走本地 yt-dlp）
    main.py            —— XTweetPlugin(Star)：配置、缓存、消息链、Handler、LLM 工具
    子模块通过 `from .x_tweet_core import *` 共享同一命名空间；以文件路径直接加载
    （测试脚本 / 手工调试）时自动退化为平铺导入。

设计约束（改代码前请先读）：
    * 一个 Handler 只能交付一个 MessageEventResult，因此正文 / 图片 / 视频必须合并进
      同一条消息链（详见 on_group_message 的注释与 README「开发避坑」章节）。
    * 任何网络失败都不向上抛异常，一律降级为可读的中文提示或原始链接。
    * 译文优先取 FxTwitter 自带的 ?lang= 结果（免费、无密钥），拿不到才调用大模型。

作者：Leisurelyun
版本：v1.13.1
"""

from __future__ import annotations

import base64
import os
import sys

if __package__:  # AstrBot 正常加载：data.plugins.astrbot_plugin_x_tweet.main
    from .x_tweet_core import *  # noqa: F401,F403
    from .x_tweet_parser import *  # noqa: F401,F403
    from .x_tweet_media import *  # noqa: F401,F403
else:  # 以文件路径直接加载（测试脚本 / 手工调试）时退化为平铺导入
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from x_tweet_core import *  # noqa: F401,F403
    from x_tweet_parser import *  # noqa: F401,F403
    from x_tweet_media import *  # noqa: F401,F403


# --------------------------------------------------------------------------- #
# 插件主体 XTweetPlugin
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #


class XTweetPlugin(Star):
    """X / Twitter 推文解析插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config

        # ------------------------- 读取配置 ------------------------- #
        self.proxy: str = self._cfg_str("proxy")
        self.enabled: bool = self._cfg_bool("enabled", True)
        self.cooldown_seconds: int = self._cfg_int("cooldown_seconds", 10)
        self.request_timeout: int = self._cfg_int("request_timeout", 20)
        self.download_timeout: int = self._cfg_int("download_timeout", 180)
        self.max_image_size: int = self._cfg_int("max_image_size", 10)
        self.max_video_size: int = self._cfg_int("max_video_size", 50)
        self.max_images: int = self._cfg_int("max_images", 4)
        self.send_images: bool = self._cfg_bool("send_images", True)
        self.send_videos: bool = self._cfg_bool("send_videos", True)
        self.send_error_hint: bool = self._cfg_bool("send_error_hint", True)
        self.show_stats_footer: bool = self._cfg_bool("show_stats_footer", True)
        self.yt_dlp_path: str = self._cfg_str("yt_dlp_path")
        self.cookie_file: str = self._cfg_str("cookie_file")

        # ---- 新增：图片尺寸 ----
        self.image_size: str = self._resolve_image_size(self._cfg_str("image_size"))

        # ---- 新增：表情回应（贴表情） ----
        self.emoji_reply: bool = self._cfg_bool("emoji_reply", True)
        self.emoji_id: str = self._cfg_str("emoji_id", DEFAULT_EMOJI_ID) or DEFAULT_EMOJI_ID

        # ---- 新增：翻译 ----
        self.translate_mode: str = self._cfg_str(
            "translate_mode", DEFAULT_TRANSLATE_MODE
        ).strip().lower() or DEFAULT_TRANSLATE_MODE
        if self.translate_mode not in TRANSLATE_MODE_CHOICES:
            logger.warning(
                f"[{PLUGIN_NAME}] 未知的翻译模式 {self.translate_mode}，已按 auto 处理"
            )
            self.translate_mode = DEFAULT_TRANSLATE_MODE
        self.translate_target_lang: str = normalize_translate_lang(
            self._cfg_str("translate_target_lang", DEFAULT_TRANSLATE_LANG)
        )
        self.translate_provider_id: str = self._cfg_str(
            "translate_provider_id", DEFAULT_TRANSLATE_PROVIDER_ID
        )
        self.translate_timeout: int = self._cfg_int("translate_timeout", 30)
        self.translate_llm_fallback: bool = self._cfg_bool("translate_llm_fallback", True)
        # FxTwitter 自带译文缺失时的重试次数（0 = 不重试，直接交大模型）
        self.translate_retry: int = max(
            0,
            min(
                MAX_TRANSLATE_RETRY,
                self._cfg_int("translate_retry", DEFAULT_TRANSLATE_RETRY),
            ),
        )
        # 去掉 @提及 / 链接 / 表情后有效字符少于该值就不再调用大模型（0 = 不限制）
        self.translate_llm_min_chars: int = max(
            0,
            min(
                200,
                self._cfg_int(
                    "translate_llm_min_chars", DEFAULT_TRANSLATE_LLM_MIN_CHARS
                ),
            ),
        )

        # ---- 新增：正文展示模式（原文 / 译文 / 两者都发） ----
        self.text_content_mode: str = self._resolve_text_content_mode(
            self._cfg_str("text_content_mode", DEFAULT_TEXT_CONTENT_MODE)
        )

        # ---- 新增：引用 / 回复贴的主贴单独成一条消息 ----
        # 打开后，主贴会被二次解析并发成独立的一条消息，译文与媒体都能正确区分
        # 推文交付方式：media=文字与媒体混排（默认）；card=X 风格卡片（媒体另发）
        tweet_delivery = str(
            self._cfg_str("tweet_delivery", DEFAULT_TWEET_DELIVERY) or ""
        ).strip().lower()
        self.tweet_delivery: str = (
            tweet_delivery if tweet_delivery in TWEET_DELIVERY_CHOICES else DEFAULT_TWEET_DELIVERY
        )
        # 卡片主题：light / dark / auto（auto 在固定时段内自动切深色）
        theme_mode = str(self._cfg_str("card_theme", DEFAULT_CARD_THEME) or "").strip().lower()
        self.card_theme: str = (
            theme_mode if theme_mode in CARD_THEME_CHOICES else DEFAULT_CARD_THEME
        )
        # 深色时段（auto 主题用，左闭右开）：起始小时之后、到结束小时之前算夜间
        self.card_dark_start_hour: int = max(
            0,
            min(
                23,
                self._cfg_int("card_dark_start_hour", DEFAULT_CARD_DARK_START_HOUR),
            ),
        )
        self.card_dark_end_hour: int = max(
            0,
            min(23, self._cfg_int("card_dark_end_hour", DEFAULT_CARD_DARK_END_HOUR)),
        )

        self.thread_split: bool = self._cfg_bool("thread_split", True)

        # ---- 新增：楼中楼（多层引用 / 回复链） ----
        # 最多向上追溯的楼层数（含链接本身）：1 = 只解析这一条
        self.thread_chain_depth: int = max(
            1,
            min(
                MAX_THREAD_CHAIN_DEPTH,
                self._cfg_int("thread_chain_depth", DEFAULT_THREAD_CHAIN_DEPTH),
            ),
        )
        # 楼层总数达到该值时改用「合并转发」（聊天记录）发送
        self.thread_merge_min_nodes: int = max(
            2,
            min(
                MAX_THREAD_CHAIN_DEPTH,
                self._cfg_int(
                    "thread_merge_min_nodes", DEFAULT_THREAD_MERGE_MIN_NODES
                ),
            ),
        )
        # 合并转发的节点里是否带上每层楼的图片
        self.thread_merge_media: bool = self._cfg_bool(
            "thread_merge_media", DEFAULT_THREAD_MERGE_MEDIA
        )
        # 楼中楼优先用「引用回复气泡」逐层发送（后端不支持时回退到合并转发）
        # 楼中楼的交付方式：forward = 一条合并转发（每层一个节点）；
        # bubble = 逐层引用气泡；card = 整条楼中楼渲染成一张 X 风格卡片图。
        delivery = (
            self._cfg_str("thread_delivery", DEFAULT_THREAD_DELIVERY).strip().lower()
        )
        if delivery not in THREAD_DELIVERY_CHOICES:
            if delivery:
                logger.warning(
                    f"[{PLUGIN_NAME}] 未知的楼中楼交付方式 {delivery}，已按 "
                    f"{DEFAULT_THREAD_DELIVERY} 处理"
                )
            delivery = DEFAULT_THREAD_DELIVERY
        self.thread_delivery: str = delivery
        # 合并转发节点里显示「↩ 回复 @上一层：上一层摘要」文字引用块
        self.thread_forward_quote: bool = self._cfg_bool(
            "thread_forward_quote", DEFAULT_THREAD_FORWARD_QUOTE
        )

        # ---- 新增：X 个人主页解析 ----
        self.profile_enabled: bool = self._cfg_bool("profile_enabled", True)
        self.profile_post_count: int = max(
            0, min(10, self._cfg_int("profile_post_count", DEFAULT_PROFILE_POST_COUNT))
        )
        # 主页交付方式：card = 渲染成 X 风格主页卡片（横幅 / 大头像 / 简介 / 元信息 /
        # 关注数 / 最近推文）；text = 旧的纯文字资料卡 + 头像图 + 推文清单
        profile_delivery = str(
            self._cfg_str("profile_delivery", DEFAULT_PROFILE_DELIVERY) or ""
        ).strip().lower()
        self.profile_delivery: str = (
            profile_delivery
            if profile_delivery in PROFILE_DELIVERY_CHOICES
            else DEFAULT_PROFILE_DELIVERY
        )

        # ---- 新增：解析黑名单（命中后不解析该推文） ----
        # 黑名单关键词：解析与搜索共用同一份（命中即跳过解析 / 不出现在搜索结果里）
        self.blocked_keywords: list[str] = self._parse_keyword_list(
            self._cfg_raw("blocked_keywords", DEFAULT_BLOCKED_KEYWORDS)
        )
        self.blocked_authors: list[str] = [
            item.lstrip("@").strip().lower()
            for item in self._parse_keyword_list(self._cfg_raw("blocked_authors", []))
            if item.lstrip("@").strip()
        ]

        # ---- 新增：定时清理缓存 ----
        # 单位小时，0 表示只在启动时清理一次，不开启定时任务
        self.cache_cleanup_interval: int = max(
            0, self._cfg_int("cache_cleanup_interval", 0)
        )
        self._cleanup_task: Optional[asyncio.Task[None]] = None

        # ---- 新增：X 搜索 ----
        self.search_enabled: bool = self._cfg_bool("search_enabled", True)
        self.search_accounts: list[str] = self._parse_account_list(
            self._cfg_raw("search_accounts", DEFAULT_SEARCH_ACCOUNTS)
        )
        self.search_account_limit: int = max(
            1,
            min(10, self._cfg_int("search_account_limit", DEFAULT_SEARCH_ACCOUNT_LIMIT)),
        )
        self.search_count: int = max(
            1,
            min(self._cfg_int("search_count", DEFAULT_SEARCH_COUNT), SEARCH_COUNT_MAX),
        )
        # 搜索结果的交付方式与翻译开关（v1.7.0）
        delivery = str(
            self._cfg_str("search_delivery", DEFAULT_SEARCH_DELIVERY) or ""
        ).strip().lower()
        self.search_delivery: str = (
            delivery if delivery in SEARCH_DELIVERY_CHOICES else DEFAULT_SEARCH_DELIVERY
        )
        self.search_translate: bool = self._cfg_bool(
            "search_translate", DEFAULT_SEARCH_TRANSLATE
        )
        self.search_translate_concurrency: int = max(
            1,
            min(
                10,
                self._cfg_int(
                    "search_translate_concurrency",
                    DEFAULT_SEARCH_TRANSLATE_CONCURRENCY,
                ),
            ),
        )
        self.search_max_age_hours: int = max(
            0, self._cfg_int("search_max_age_hours", 0)
        )
        self.search_with_replies: bool = self._cfg_bool("search_with_replies", True)
        self.search_include_media: bool = self._cfg_bool("search_include_media", True)
        self.search_required_keywords: list[str] = self._parse_keyword_list(
            self._cfg_raw("search_required_keywords", [])
        )
        # 账号列表为空时，按关键词向 FxTwitter /2/typeahead 自动匹配相关账号
        # 镜像实例轮询：FxTwitter 主站单点故障时的第二级保障。默认不轮询任何
        # 实例（实测常见公共镜像均不可达），用户可在面板里按需填写。
        self.enable_mirror_sources: bool = self._cfg_bool(
            "enable_mirror_sources", DEFAULT_ENABLE_MIRROR_SOURCES
        )
        self.mirror_fx_instances: list[str] = self._parse_instance_list(
            self._cfg_raw("mirror_fx_instances", DEFAULT_MIRROR_FX_INSTANCES)
        )
        # 解析源优先级顺序（未列出的源由 parser 自动追加到末尾）
        raw_order = self._cfg_raw("parse_source_order", DEFAULT_PARSE_SOURCE_ORDER)
        # 配置类型自检：schema 类型变更后，旧配置文件可能仍是旧类型（面板保存会报 400）。
        # 这里只给排查指引，不自动改用户配置。
        if isinstance(raw_order, (list, tuple)):
            logger.warning(
                f"[{PLUGIN_NAME}] 配置项 parse_source_order 仍是列表形式，面板保存会失败（400）；"
                f"请把配置文件里的该项改成逗号分隔文本，例如 "
                f'"fxtwitter, mirror, syndication, oembed"'
            )
        order = self._parse_source_order_list(raw_order)
        self.parse_source_order: list[str] = order or list(DEFAULT_PARSE_SOURCE_ORDER)

        # ------------------------- 缓存目录 ------------------------- #
        # 自定义目录可能非法（盘符不存在 / 无写权限 / 被写成文件）：不能让 mkdir 抛出
        # 异常把插件顶进「加载失败」，失败时回落到默认目录并继续工作。
        default_cache_dir = get_default_cache_dir()
        cache_dir_cfg = self._cfg_str("cache_dir")
        if cache_dir_cfg:
            self.cache_dir = Path(os.path.expanduser(cache_dir_cfg))
        else:
            self.cache_dir = default_cache_dir
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error(
                f"[{PLUGIN_NAME}] 配置的缓存目录不可用（{self.cache_dir}）：{exc}。"
                f"已回落到默认目录 {default_cache_dir}。"
            )
            self.cache_dir = default_cache_dir
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as fallback_exc:
                logger.error(
                    f"[{PLUGIN_NAME}] 默认缓存目录也不可用（{self.cache_dir}）：{fallback_exc}。"
                    f"图片、视频与解析缓存将无法落盘，解析结果会降级为原链接。"
                )
        logger.info(f"[{PLUGIN_NAME}] 媒体缓存目录：{self.cache_dir}")

        # ------------------------- HTTP 客户端 ------------------------- #
        # 优先使用插件配置的代理，其次自动读取环境变量中的代理设置
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.request_timeout, connect=min(10.0, self.request_timeout)),
            "follow_redirects": True,
            "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
        }
        if self.proxy:
            client_kwargs["proxy"] = self.proxy
            logger.info(f"[{PLUGIN_NAME}] 已启用配置代理：{self.proxy}")
        else:
            env_proxy = (
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("http_proxy")
                or os.environ.get("ALL_PROXY")
                or os.environ.get("all_proxy")
            )
            if env_proxy:
                logger.info(f"[{PLUGIN_NAME}] 已检测到环境变量代理：{env_proxy}")

        self.client = httpx.AsyncClient(**client_kwargs)

        # ------------------------- 服务与下载器 ------------------------- #
        self.parser = XTweetParser(
            client=self.client,
            timeout=float(self.request_timeout),
            translate_retry=self.translate_retry,
            translate_llm_min_chars=self.translate_llm_min_chars,
            enable_mirror_sources=self.enable_mirror_sources,
            mirror_fx_instances=self.mirror_fx_instances,
            source_order=self.parse_source_order,
        )
        self.downloader = MediaDownloader(
            client=self.client,
            cache_dir=self.cache_dir,
            max_image_size=self.max_image_size,
            max_video_size=self.max_video_size,
            timeout=float(self.request_timeout),
            download_timeout=float(self.download_timeout),
            yt_dlp_path=self.yt_dlp_path,
            cookie_file=self.cookie_file,
            proxy=self.proxy,
            cache_max_files=self._cfg_int("cache_max_files", 300),
        )
        self.translator = Translator(
            context=context,
            provider_id=self.translate_provider_id,
            timeout=float(self.translate_timeout),
        )

        # ------------------------- 运行时状态 ------------------------- #
        # 内存缓存：tweet_id -> TweetData
        self._data_cache: dict[str, TweetData] = {}
        self._data_cache_lock = asyncio.Lock()
        # 解析结果落盘后的 mtime，用于控制重写频率
        self._data_cache_mtime: dict[str, float] = {}
        # 冷却表：f"{session_id}:{tweet_id}" -> 上次解析时间
        self._cooldown: dict[str, float] = {}
        # 会话最近解析记录：session_id -> [(tweet_id, 时间戳)]
        self._session_recent: dict[str, list[tuple[str, float]]] = {}
        # 已发送媒体记录：f"{session_id}:{tweet_id}" -> 时间戳
        self._sent_media: dict[str, float] = {}
        # 已发送图片记录：与视频分开，避免「图片已发」把同一条推文的视频下载闸门也关掉
        self._sent_images: dict[str, float] = {}
        # 主页资料缓存：handle -> ProfileData（短时缓存，避免同一链接反复请求）
        self._profile_cache: dict[str, ProfileData] = {}
        self._profile_cache_ts: dict[str, float] = {}
        # 会话级目标语言：session_id -> 语言码（/xt 翻译 设置，只影响该会话）
        self._session_translate_lang: dict[str, str] = {}
        # 本轮消息里用「引用回复气泡」直接发出（不走 AstrBot 事件管道）的消息条数
        self._direct_deliveries: int = 0
        # 最近一次搜索实际使用的账号（含按关键词自动匹配到的），供来源展示与 payload 回显
        self._last_search_targets: list[str] = []
        # typeahead 结果内存缓存：关键词 -> (写入时间, handles, 错误)，防止 429 限速
        self._typeahead_cache: dict[str, tuple[float, list[str], str]] = {}
        # 搜索语法帮助图片（静态资源，生成一次后每次搜索直接复用）
        self.help_image_path: str = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "assets", "search_help.png"
        )

        self.run_count = 0
        self.last_error = ""

    # ------------------------------------------------------------------ #
    # 配置读取辅助
    # ------------------------------------------------------------------ #

    def _cfg_raw(self, key: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        try:
            value = self.config.get(key) if hasattr(self.config, "get") else self.config[key]
        except Exception:
            return default
        return default if value is None else value

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self._cfg_raw(key, default)
        return str(value).strip() if value is not None else default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg_raw(key, default))
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg_raw(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        try:
            return bool(int(value))
        except Exception:
            return default

    def _resolve_image_size(self, raw: str) -> str:
        """校验图片尺寸配置，非法值回退到默认档位。"""
        value = (raw or "").strip().lower()
        if value in IMAGE_SIZE_CHOICES:
            return value
        # 允许用户直接填写 twimg 的 name 参数（如 900x900），只要不是明显错误就保留
        if value and re.fullmatch(r"[0-9a-z]{2,20}x[0-9a-z]{2,20}|[a-z]{3,10}", value):
            if value not in IMAGE_SIZE_CHOICES:
                logger.info(
                    f"[{PLUGIN_NAME}] 图片尺寸使用自定义值 {value}（档位："
                    f"{'/'.join(IMAGE_SIZE_CHOICES)}）"
                )
            return value
        if value:
            logger.warning(
                f"[{PLUGIN_NAME}] 未知的图片尺寸 {value}，已回退到 {DEFAULT_IMAGE_SIZE}"
            )
        return DEFAULT_IMAGE_SIZE

    def _resolve_text_content_mode(self, raw: str) -> str:
        """校验正文展示模式，非法值回退到默认档位。"""
        value = (raw or "").strip().lower()
        aliases = {
            "all": "both",
            "全部": "both",
            "原文+译文": "both",
            "原文+翻译": "both",
            "only_original": "original",
            "origin": "original",
            "原文": "original",
            "只发原文": "original",
            "仅原文": "original",
            "only_translation": "translation",
            "translated": "translation",
            "译文": "translation",
            "翻译": "translation",
            "只发译文": "translation",
            "仅译文": "translation",
            "仅翻译": "translation",
        }
        value = aliases.get(value, value)
        if value in TEXT_CONTENT_MODE_CHOICES:
            return value
        if raw:
            logger.warning(
                f"[{PLUGIN_NAME}] 未知的正文展示模式 {raw}，已回退到 {DEFAULT_TEXT_CONTENT_MODE}"
            )
        return DEFAULT_TEXT_CONTENT_MODE

    @staticmethod
    def _split_list_setting(raw: Any, pattern: str) -> list[str]:
        """把配置里的列表型取值拆成字符串列表。

        兼容三种来源：真正的 list（配置面板的 list 类型）、逗号/分号分隔的字符串、
        以及被序列化成字符串的写法（`"['a', 'b']"`，手工编辑配置文件时可能出现）。
        """
        if raw is None:
            return []
        if isinstance(raw, (list, tuple, set)):
            return [str(item) for item in raw]
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        text = text.replace("'", "").replace('"', "")
        return re.split(pattern, text)

    @staticmethod
    def _parse_account_list(raw: Any) -> list[str]:
        """把配置里的账号列表归一化为不带 @ 的用户名列表。

        兼容三种写法：list（配置面板的 list 类型）、逗号/空格分隔的字符串、
        以及带 x.com / twitter.com 链接的写法。
        """
        items = XTweetPlugin._split_list_setting(raw, r"[,，;；\s]+")

        accounts: list[str] = []
        for item in items:
            name = (item or "").strip()
            if not name:
                continue
            # 支持直接粘贴主页链接：https://x.com/foo 或 x.com/foo
            match = re.search(
                r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", name, re.IGNORECASE
            )
            if match:
                name = match.group(1)
            name = name.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name):
                logger.debug(f"[{PLUGIN_NAME}] 忽略无法识别的搜索账号：{item!r}")
                continue
            if name.lower() not in {a.lower() for a in accounts}:
                accounts.append(name)
        return accounts

    @staticmethod
    def _parse_instance_list(raw: Any) -> list[str]:
        """把配置里的镜像实例列表归一化为去尾部斜杠的 URL 列表。

        只保留 http(s) 开头的条目；其余（空行、账号名等）按无效配置忽略。
        """
        items = XTweetPlugin._split_list_setting(raw, r"[,，;；\s]+")
        instances: list[str] = []
        for item in items:
            url = (item or "").strip().rstrip("/")
            if not url or not url.lower().startswith(("http://", "https://")):
                if url:
                    logger.debug(f"[{PLUGIN_NAME}] 忽略无效的镜像实例地址：{item!r}")
                continue
            if url not in instances:
                instances.append(url)
        return instances

    @staticmethod
    def _parse_source_order_list(raw: Any) -> list[str]:
        """把配置里的解析源顺序归一化：只保留已知源键、去重、保持顺序。"""
        items = XTweetPlugin._split_list_setting(raw, r"[,，;；\s]+")
        order: list[str] = []
        for item in items:
            key = (item or "").strip().lower()
            if not key:
                continue
            if key not in PARSE_SOURCE_CHOICES:
                logger.debug(f"[{PLUGIN_NAME}] 忽略未知的解析源：{item!r}")
                continue
            if key not in order:
                order.append(key)
        return order

    @staticmethod
    def _parse_keyword_list(raw: Any) -> list[str]:
        """把配置里的关键词列表归一化为小写关键词列表。

        关键词内部允许有空格（例如 `machine learning`），因此只按逗号/分号/换行拆分。
        """
        items = XTweetPlugin._split_list_setting(raw, r"[,，;；\n]+")
        keywords: list[str] = []
        for item in items:
            word = (item or "").strip().lower()
            if word and word not in keywords:
                keywords.append(word)
        return keywords

    @staticmethod
    def _match_keywords(text: str, keywords: list[str]) -> Optional[str]:
        """返回正文命中的第一个关键词，未命中返回 None。"""
        if not keywords:
            return None
        haystack = (text or "").lower()
        for word in keywords:
            if word in haystack:
                return word
        return None

    # ------------------------------------------------------------------ #
    # X 搜索
    # ------------------------------------------------------------------ #

    async def _fetch_profile_statuses(
        self,
        handle: str,
        translate_lang: str = "",
        since: Optional[int] = None,
        count: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """拉取某个账号的最新推文列表，返回 (status 列表, 错误信息)。

        count 为空时按搜索配置取条数；主页解析会传入自己的条数。
        """
        # 转帖与回复都会被调用方丢掉，按倍数多要一些，才凑得满目标条数
        # （实测该接口返回的条数比 count 多，count 只是下限）
        want = int(count) if count else max(10, min(100, self.search_count * 3))
        params: dict[str, str] = {"count": str(max(5, min(100, want)))}
        params["with_replies"] = "1" if self.search_with_replies else "0"
        if since:
            params["since"] = str(int(since))
        lang = normalize_translate_lang(translate_lang)
        if lang:
            params["lang"] = lang

        url = SEARCH_STATUSES_API.format(handle=urllib.parse.quote(handle))
        try:
            response = await self.client.get(
                url, headers=self.parser.headers(), params=params
            )
        except httpx.TimeoutException:
            return [], "请求超时"
        except httpx.HTTPError as exc:
            return [], f"网络异常：{exc}"
        except Exception as exc:  # 兜底：任何异常都降级为「没有数据 + 原因」
            return [], f"网络异常：{exc}"
        if response.status_code == 204:
            return [], ""  # 没有比 since 更新的推文
        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        try:
            payload = response.json()
        except Exception as exc:
            return [], f"响应不是合法 JSON：{exc}"

        results = payload.get("results")
        if not isinstance(results, list):
            return [], str(payload.get("message") or "接口未返回 results")
        return [item for item in results if isinstance(item, dict)], ""

    async def _fetch_typeahead_users(
        self, keyword: str, limit: int
    ) -> tuple[list[str], str]:
        """用 FxTwitter /2/typeahead 按关键词查找相关账号，返回 (handle 列表, 错误信息)。

        该端点无需登录即可调用（X 站内搜索框同款），用于「搜索账号列表」为空时
        自动确定检索范围。任何失败都按「空列表 + 中文原因」返回，不向上抛异常。
        结果带 15 分钟内存缓存，防止短时间内重复搜索触发 429 限速。
        """
        query = (keyword or "").strip()
        if not query:
            return [], "关键词为空"

        cache_key = query.lower()
        now = time.time()
        cached = self._typeahead_cache.get(cache_key)
        if cached and now - cached[0] < TYPEAHEAD_CACHE_TTL_SECONDS:
            logger.debug(
                f"[{PLUGIN_NAME}] typeahead「{query}」命中内存缓存"
                f"（{len(cached[1])} 个账号）"
            )
            return list(cached[1]), cached[2]

        try:
            response = await self.client.get(
                TYPEAHEAD_API,
                headers=self.parser.headers(),
                params={"q": query},
            )
        except httpx.TimeoutException:
            return [], "请求超时"
        except httpx.HTTPError as exc:
            return [], f"网络异常：{exc}"
        except Exception as exc:  # 兜底：任何异常都降级为「没有数据 + 原因」
            return [], f"网络异常：{exc}"
        if response.status_code != 200:
            return [], f"HTTP {response.status_code}"

        try:
            payload = response.json()
        except Exception as exc:
            return [], f"响应不是合法 JSON：{exc}"

        users = payload.get("users")
        if not isinstance(users, list):
            return [], "接口未返回 users 列表"
        handles: list[str] = []
        for user in users:
            if not isinstance(user, dict):
                continue
            handle = str(user.get("screen_name") or "").strip()
            if not handle or handle in handles:
                continue
            handles.append(handle)
            if len(handles) >= max(1, int(limit)):
                break

        self._typeahead_cache[cache_key] = (now, list(handles), "")
        return handles, ""

    def _status_passes_filters(
        self,
        status: dict[str, Any],
        required: list[str],
        blocked: list[str],
        excluded: Optional[list[str]] = None,
        min_likes: int = 0,
    ) -> tuple[bool, str]:
        """关键词、排除词、点赞门槛与时间过滤，返回 (是否通过, 命中的关键词)。"""
        text = status.get("text") or ""
        if blocked and self._match_keywords(text, blocked):
            return False, ""
        if excluded and self._match_keywords(text, excluded):
            return False, ""
        if min_likes > 0:
            likes = _to_int(status.get("favorite_count"))
            if likes is not None and likes < min_likes:
                return False, ""
        if required:
            hit = self._match_keywords(text, required)
            if not hit:
                return False, ""
            return True, hit
        return True, ""

    def _status_to_tweet(self, status: dict[str, Any], handle: str = "") -> TweetData:
        """把时间线里的 status 对象转成 TweetData（只取展示与搜索需要的字段）。"""
        data = TweetData(tweet_id=str(status.get("id") or ""))
        self.parser.fill_from_tweet(data, status)
        # 时间线接口不保证带 author 字段，缺失时用被查询的账号补齐，避免显示成「未知作者」
        if handle and not data.author_screen_name:
            data.author_screen_name = handle
        if not data.url:
            data.url = build_tweet_url(data.author_screen_name or None, data.tweet_id)
        # 搜索场景不下载媒体、也不做二次翻译，避免拖慢响应
        data.translation_text = ""
        data.translation_lang = ""
        data.translation_provider = ""
        return data

    async def search_x_posts(
        self,
        accounts: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        limit: int = 0,
        include_replies: Optional[bool] = None,
        include_media: Optional[bool] = None,
        translate_lang: str = "",
        exclude_keywords: Optional[list[str]] = None,
        min_likes: int = 0,
    ) -> tuple[list[tuple[TweetData, str]], str]:
        """在 X 账号里按关键词检索最新推文。

        账号来源优先级：显式 accounts 参数 > 配置的「搜索账号列表」；两者都为空且
        账号列表为空时，按关键词向 /2/typeahead 自动匹配相关账号。
        多个账号的时间线并发拉取，单个账号失败不影响其余账号。
        exclude_keywords：命令行排除词（-词），命中任意一个即丢弃；
        min_likes：点赞数门槛，低于该值的推文丢弃。
        返回 ([(TweetData, 命中关键词)], 错误信息)。错误信息非空时列表可能为空。
        """
        required = [word.lower() for word in (keywords or []) if word and word.strip()]
        if not required:
            required = list(self.search_required_keywords)
        blocked = list(self.blocked_keywords)
        excluded = [
            word.lower() for word in (exclude_keywords or []) if word and word.strip()
        ]
        min_likes = max(0, int(min_likes or 0))

        # v1.6.0：账号列表为空时，用 /2/typeahead 按关键词自动匹配相关账号再检索
        targets = list(accounts or self.search_accounts)
        auto_used = False
        if not targets and required:
            keyword_text = " ".join(required)
            handles, auto_error = await self._fetch_typeahead_users(
                keyword_text, self.search_account_limit
            )
            if handles:
                targets = handles
                auto_used = True
                logger.info(
                    f"[{PLUGIN_NAME}] 未配置搜索账号，已按关键词「{keyword_text}」"
                    f"自动匹配 {len(handles)} 个账号：{'、'.join('@' + h for h in handles)}"
                )
            else:
                return [], (
                    "没有配置要检索的 X 账号，按关键词自动匹配账号也失败了"
                    f"（{auto_error or '未返回相关账号'}）。"
                    "可前往插件配置面板添加「搜索账号列表」。"
                )
        self._last_search_targets = list(targets)
        if not targets:
            return [], "没有配置要检索的 X 账号（配置项：搜索账号列表）"
        # 单次查询账号数统一受 search_account_limit 限制
        if not auto_used:
            targets = targets[: self.search_account_limit]

        want_media = self.search_include_media if include_media is None else include_media
        max_results = max(1, min(int(limit or self.search_count), SEARCH_COUNT_MAX))

        if include_replies is None:
            include_replies = self.search_with_replies

        since: Optional[int] = None
        if self.search_max_age_hours > 0:
            since = int(time.time()) - self.search_max_age_hours * 3600

        # v1.6.0：多个账号并发拉取时间线；单个账号失败只记错误，不中断其它账号
        async def _fetch(handle: str) -> tuple[list[dict[str, Any]], str]:
            try:
                return await self._fetch_profile_statuses(
                    handle, translate_lang=translate_lang, since=since
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 搜索 @{handle} 失败: {exc}")
                return [], f"@{handle}: {exc}"

        fetched = await asyncio.gather(*[_fetch(handle) for handle in targets])

        collected: list[tuple[TweetData, str]] = []
        seen: set[str] = set()
        errors: list[str] = []

        for (statuses, error), handle in zip(fetched, targets):
            if error:
                errors.append(error)
                continue

            for status in statuses:
                tweet_id = str(status.get("id") or "")
                if not tweet_id or tweet_id in seen:
                    continue
                # 转帖的作者是原作者（reposted_by 才是被检索的账号），混进来会让
                # 结果的「作者」张冠李戴，这里和回复一样直接跳过
                if status_is_repost(status, handle):
                    continue
                # 过滤回复（正文以 @ 开头的基本都是回复）
                text = status.get("text") or ""
                if not include_replies and text.lstrip().startswith("@"):
                    continue
                passed, hit = self._status_passes_filters(
                    status, required, blocked, excluded, min_likes
                )
                if not passed:
                    continue
                data = self._status_to_tweet(status, handle)
                if not data.tweet_id or not data.text:
                    continue
                seen.add(tweet_id)
                collected.append((data, hit or ""))

        # 按发布时间倒序，最新的排在前面
        collected.sort(key=lambda item: item[0].created_timestamp or 0, reverse=True)
        return collected[:max_results], "；".join(errors)

    def _search_display_lines(self, data: TweetData) -> list[str]:
        """搜索结果的正文展示行：按 text_content_mode 决定译文 / 原文的组合。

        返回 0-2 行：both（有译文时）= [译文, 原文]；translation = [译文]；
        original 或没有译文时 = [原文]。两者都没有时返回空列表。
        """
        original = truncate_text((data.text or "").replace("\n", " ").strip(), 160)
        translated = truncate_text(
            (data.translation_text or "").replace("\n", " ").strip(), 160
        )
        mode = self.text_content_mode
        if mode == "original":
            return [original] if original else []
        if translated:
            if mode == "translation":
                return [translated]
            return [translated, original] if original else [translated]
        return [original] if original else []

    def build_search_text(
        self, results: list[tuple[TweetData, str]], keyword: str = ""
    ) -> str:
        """把搜索结果拼成一条纯文本消息体。"""
        if not results:
            return ""

        head = f"🔍 X 搜索：{keyword}" if keyword else "🔍 X 最新动态"
        lines = [head, ""]
        for index, (data, _hit) in enumerate(results, start=1):
            created = format_relative_time(data.created_timestamp) or "时间未知"
            lines.append(f"{index}. {data.author_display} · {created}")
            lines.extend(self._search_display_lines(data))
            link = data.url or build_tweet_url(data.author_screen_name, data.tweet_id)
            if link:
                lines.append(link)
            if index != len(results):
                lines.append("")
        return "\n".join(lines)

    async def _build_search_forward(
        self,
        event: AstrMessageEvent,
        results: list[tuple[TweetData, str]],
        keyword: str = "",
    ) -> list[Any]:
        """把搜索结果做成一条合并转发（聊天记录），每条结果一个节点。"""
        uin = self._node_uin(event)
        nodes: list[Any] = []
        for index, (data, _hit) in enumerate(results, start=1):
            created = format_relative_time(data.created_timestamp) or "时间未知"
            lines = [f"{index}. {data.author_display} · {created}"]
            lines.extend(self._search_display_lines(data))
            link = data.url or build_tweet_url(data.author_screen_name, data.tweet_id)
            if link:
                lines.append(link)
            nodes.append(
                Comp.Node(
                    content=[Comp.Plain("\n".join(lines))],
                    name=self._node_display_name(data),
                    uin=uin,
                )
            )
        return [Comp.Nodes(nodes)]

    async def _render_search_image(
        self,
        results: list[tuple[TweetData, str]],
        keyword: str,
        accounts: list[str],
    ) -> str:
        """把搜索结果渲染成图片，返回本地文件路径；失败返回空串。

        三级尝试：① AstrBot 文转图（自定义 HTML/Jinja2 模板，走官方云端渲染服务）
        → ② 本地文转图（text_to_image，PIL 渲染纯文本）→ ③ 返回空串由调用方回退文本发送。
        任何一级失败都只记日志，不影响搜索结果本身。
        """
        # 头像并发下载后 base64 内嵌（渲染服务器拉不到 twimg，直链会破图）
        avatars = await asyncio.gather(
            *[self._avatar_data_uri(data) for data, _hit in results]
        )
        items: list[dict[str, Any]] = []
        for (data, _hit), avatar in zip(results, avatars):
            item = await self._card_item(data, with_quote=False, avatar=avatar)
            # 搜索结果用更短的正文，避免卡片过长
            item["translation"] = self._card_text(item["translation"], 300)
            item["text"] = self._card_text(item["text"], 300)
            item["url"] = data.url or build_tweet_url(
                data.author_screen_name, data.tweet_id
            )
            items.append(item)
        sub = f"共 {len(results)} 条结果"
        if accounts:
            sub += " · 检索范围 " + "、".join("@" + a for a in accounts[:5])
        payload: dict[str, Any] = {
            "header": {"title": f"X 搜索：{keyword or '最新动态'}", "sub": sub},
            "show_line": False,
            "items": items,
            "footer": "astrbot_plugin_x_tweet · /xt help 查看搜索语法",
        }

        path = await self._render_card(payload)
        if path:
            return path

        try:
            path = await self.text_to_image(
                self.build_search_text(results, keyword=keyword), return_url=False
            )
            if path:
                logger.info(f"[{PLUGIN_NAME}] 搜索图片已回退到本地文转图渲染")
                return str(path)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 搜索图片（本地文转图）渲染失败：{exc}")
        return ""

    async def _translate_search_results(
        self, results: list[tuple[TweetData, str]], target: str
    ) -> None:
        """给搜索结果补译文（免费优先 + 大模型兜底），就地写回 TweetData。

        ① 先调 FxTwitter 单推文接口拿自带免费译文（时间线接口本身不返回译文）；
        ② 拿不到且允许回退大模型时，交给 Translator（受 translate_llm_min_chars 限制）。
        并发受 search_translate_concurrency 限制；单条失败只记日志，不影响其它结果。
        """
        if not target or not results:
            return
        semaphore = asyncio.Semaphore(max(1, self.search_translate_concurrency))
        allow_llm = self.translate_mode != "fxtwitter" and self.translate_mode != "off"
        allow_llm = allow_llm and self.translate_llm_fallback

        async def _one(data: TweetData) -> None:
            if not data.tweet_id or not data.text:
                return
            # 正文已经是目标语言（含 lang 缺失时的字符集兜底）就不必补译文
            if already_in_target_lang(data, target):
                return
            async with semaphore:
                try:
                    fresh = await self.parser.fetch_via_fxtwitter(
                        data.tweet_id, translate_lang=target
                    )
                except Exception as exc:
                    logger.debug(
                        f"[{PLUGIN_NAME}] 搜索补译文失败（{data.tweet_id}）：{exc}"
                    )
                    fresh = None
                if fresh is not None and fresh.translated:
                    data.translation_text = fresh.translation_text
                    data.translation_lang = fresh.translation_lang
                    data.translation_provider = fresh.translation_provider
                    data.translation_source_lang = fresh.translation_source_lang
                    return
                if not allow_llm:
                    return
                if (
                    self.translate_llm_min_chars > 0
                    and meaningful_text_length(data.text) < self.translate_llm_min_chars
                ):
                    return
                text = await self.translator.translate(data.text, target, data.lang)
                if text and text.strip() != (data.text or "").strip():
                    data.translation_text = text
                    data.translation_lang = target
                    data.translation_provider = self.translator.provider_id or "llm"

        await asyncio.gather(*[_one(data) for data, _hit in results])

    def build_search_payload(
        self,
        results: list[tuple[TweetData, str]],
        keyword: str = "",
        error: str = "",
        accounts: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """构造供 LLM 阅读的搜索结果 JSON。"""
        posts: list[dict[str, Any]] = []
        for data, hit in results:
            post: dict[str, Any] = {
                "url": data.url or build_tweet_url(data.author_screen_name, data.tweet_id),
                "author": data.author_display,
                "author_screen_name": data.author_screen_name,
                "text": data.text,
                "created_at": format_relative_time(data.created_timestamp),
                "language": data.lang,
            }
            if data.translation_text:
                post["translation"] = data.translation_text
            if hit:
                post["matched_keyword"] = hit
            if self.show_stats_footer:
                post["stats"] = {
                    "likes": data.likes,
                    "reposts": data.retweets,
                    "replies": data.replies,
                    "views": data.views,
                }
            if data.images:
                post["images_urls"] = data.images
            if data.videos:
                post["video_urls"] = data.videos
            posts.append(post)

        payload: dict[str, Any] = {
            "success": bool(posts),
            "type": "x_search",
            "keyword": keyword,
            "count": len(posts),
            "searched_accounts": (
                list(accounts) if accounts else list(self._last_search_targets)
            ),
            "blacklist": self.blocked_keywords,
            "posts": posts,
        }
        if error:
            payload["warning"] = error
        if not posts:
            payload["note"] = (
                "没有检索到符合条件的推文。可以提示用户换关键词，"
                "或在插件配置里增加「搜索账号列表」。"
            )
        return payload

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _search_account_text(self) -> str:
        """/xt 状态 里的账号来源说明。"""
        if self.search_accounts:
            return f"{len(self.search_accounts)} 个账号"
        return f"未配置账号，按关键词自动匹配 {self.search_account_limit} 个"

    def _source_order_brief(self) -> str:
        """解析源顺序的简写（键名形式，供启动日志使用）。"""
        return " → ".join(self.parse_source_order) or "默认"

    def _search_summary_text(self) -> str:
        """启动日志里的搜索摘要（含账号来源）。"""
        if not self.search_enabled:
            return "关"
        if self.search_accounts:
            return f"开({len(self.search_accounts)}个账号)"
        return f"开(0个账号,按关键词自动匹配{self.search_account_limit}个)"

    async def initialize(self) -> None:
        """插件激活时调用。"""
        logger.info(
            f"[{PLUGIN_NAME}] 已加载：自动解析={'开' if self.enabled else '关'}，"
            f"图片上限={self.max_image_size}MB，视频上限={self.max_video_size}MB，"
            f"图片尺寸={image_size_display(self.image_size)}，"
            f"贴表情={'开' if self.emoji_reply else '关'}，"
            f"翻译={self.translate_mode}->{self.translate_target_lang}，"
            f"正文={TEXT_CONTENT_MODE_LABELS.get(self.text_content_mode, self.text_content_mode)}，"
            f"推文交付={self.tweet_delivery}，"
            f"解析源={self._source_order_brief()}，"
            f"搜索={self._search_summary_text()}，"
            f"搜索交付={self.search_delivery}(翻译{'开' if self.search_translate else '关'})，"
            f"定时清理={'每' + str(self.cache_cleanup_interval) + '小时' if self.cache_cleanup_interval > 0 else '关'}，"
            f"楼中楼={self._thread_log_text()}"
        )
        if self.search_enabled and not self.search_accounts:
            logger.info(
                f"[{PLUGIN_NAME}] 「搜索账号列表」为空，搜索时将按关键词自动匹配 "
                f"{self.search_account_limit} 个相关账号（FxTwitter /2/typeahead）"
            )
        # 启动时做一次缓存清理，避免长期运行后目录膨胀
        try:
            await asyncio.to_thread(self.downloader.cleanup_cache)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 启动清理缓存失败: {exc}")
        self._start_cleanup_task()

    async def terminate(self) -> None:
        """插件卸载/重载时调用，释放网络资源。"""
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass  # 正常取消，不需要记录
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 清理任务退出时出错: {exc}")
            finally:
                self._cleanup_task = None
        await self.parser.close()
        await self.downloader.close()

    # ------------------------------------------------------------------ #
    # 定时清理缓存
    # ------------------------------------------------------------------ #

    def _start_cleanup_task(self) -> None:
        """按配置的间隔启动后台缓存清理任务（0 表示不启动）。"""
        if self.cache_cleanup_interval <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cleanup_task = loop.create_task(self._cleanup_loop())

    def _cleanup_interval_seconds(self) -> float:
        """把配置的小时间隔换算成秒（小于 1 小时按 1 小时计）。

        单独抽成一个方法，既方便阅读，也方便测试里替换成极小间隔来验证清理逻辑。
        """
        return max(1, self.cache_cleanup_interval) * 3600

    async def _cleanup_loop(self) -> None:
        """周期性清理媒体缓存与过期解析结果。"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval_seconds())
                removed_media = await asyncio.to_thread(self.downloader.cleanup_cache)
                removed_json = await asyncio.to_thread(self._purge_stale_data_files)
                if removed_media or removed_json:
                    logger.info(
                        f"[{PLUGIN_NAME}] 定时清理完成：媒体 {removed_media} 个、"
                        f"过期解析结果 {removed_json} 个"
                    )
                else:
                    logger.debug(f"[{PLUGIN_NAME}] 定时清理完成：没有需要清理的文件")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 定时清理失败：{exc}")

    def _purge_stale_data_files(self) -> int:
        """删除超过 7 天未使用的 data/<tweet_id>.json 解析缓存。"""
        ttl_seconds = DATA_CACHE_TTL_DAYS * 86400
        now = time.time()
        removed = 0
        try:
            for path in self.cache_dir.glob("*.json"):
                try:
                    if now - path.stat().st_mtime <= ttl_seconds:
                        continue
                    path.unlink()
                    self._data_cache.pop(path.stem, None)
                    self._data_cache_mtime.pop(path.stem, None)
                    removed += 1
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 清理过期解析结果失败: {exc}")
        return removed

    # ------------------------------------------------------------------ #
    # 内存缓存
    # ------------------------------------------------------------------ #

    async def _clear_all_caches(self) -> tuple[int, int, int]:
        """清空媒体缓存与推文解析缓存，返回 (媒体文件数, 解析文件数, 总字节)。"""
        media_count, media_size = await asyncio.to_thread(self.downloader.clear_cache)
        json_count, json_size = await asyncio.to_thread(self.downloader.clear_data_cache)
        self._data_cache.clear()
        self._data_cache_mtime.clear()
        self._session_recent.clear()
        self._sent_media.clear()
        self._sent_images.clear()
        self._profile_cache.clear()
        self._profile_cache_ts.clear()
        return media_count, json_count, media_size + json_size

    async def _cache_get(self, tweet_id: str) -> Optional[TweetData]:
        data = self._data_cache.get(tweet_id)
        if data is not None:
            return data
        # 尝试从磁盘恢复（放在锁里，避免同一条推文被并发读盘 / 写盘交叉）
        async with self._data_cache_lock:
            data = self._data_cache.get(tweet_id)
            if data is not None:
                return data
            path = self._data_file(tweet_id)
            if not path.exists():
                return None
            try:
                raw = json.loads(
                    await asyncio.to_thread(path.read_text, encoding="utf-8")
                )
                data = TweetData.from_dict(raw)
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 读取缓存 {tweet_id} 失败: {exc}")
                return None
            self._data_cache[tweet_id] = data
            try:
                self._data_cache_mtime[tweet_id] = (await asyncio.to_thread(path.stat)).st_mtime
            except Exception:
                pass
            return data

    async def _cache_put(self, data: TweetData) -> None:
        """写入内存缓存，并把解析结果持久化到 data 目录。"""
        if not data or not data.tweet_id:
            return
        self._data_cache[data.tweet_id] = data

        path = self._data_file(data.tweet_id)
        try:
            payload = json.dumps(data.to_dict(), ensure_ascii=False, indent=2)
            async with self._data_cache_lock:
                await asyncio.to_thread(path.write_text, payload, encoding="utf-8")
                self._data_cache_mtime[data.tweet_id] = (
                    await asyncio.to_thread(path.stat)
                ).st_mtime
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 写入缓存 {data.tweet_id} 失败: {exc}")

    def _data_file(self, tweet_id: str) -> Path:
        safe_id = re.sub(r"[^0-9A-Za-z_-]", "", tweet_id) or "unknown"
        return self.cache_dir / f"{safe_id}.json"

    def _record_session_tweet(self, session_id: str, tweet_id: str) -> None:
        """记录会话最近解析的推文，供无参数调用 LLM 工具时回溯。"""
        if not session_id:
            return
        records = self._session_recent.setdefault(session_id, [])
        records = [(tid, ts) for tid, ts in records if tid != tweet_id]
        records.append((tweet_id, time.time()))
        self._session_recent[session_id] = records[-10:]

    # ------------------------------------------------------------------ #
    # 会话级目标语言（/xt 翻译）
    # ------------------------------------------------------------------ #

    @property
    def active_translate_lang(self) -> str:
        """当前真正生效的目标语言：会话级覆盖优先，否则用插件配置值。"""
        return _ACTIVE_TRANSLATE_LANG.get() or self.translate_target_lang

    def session_translate_lang(self, session_id: str) -> str:
        """该会话通过 /xt 翻译 设置的语言（没有则为空字符串）。"""
        return self._session_translate_lang.get(session_id or "", "")

    def _apply_session_lang(self, event: AstrMessageEvent) -> str:
        """进入处理器时把当前会话的目标语言写进 ContextVar，返回生效值。

        每次处理消息都会重新写一遍（而不是只在 /xt 翻译 时写），因此即使框架
        复用同一个任务顺序处理不同会话，也不会读到上一个会话的语言。
        """
        session_id = getattr(event, "unified_msg_origin", "") or ""
        lang = self.session_translate_lang(session_id)
        _ACTIVE_TRANSLATE_LANG.set(lang)
        return lang or self.translate_target_lang

    def _resolve_session_tweet(self, session_id: str) -> Optional[str]:
        """取该会话最近一条仍在有效期内的推文 ID。"""
        records = self._session_recent.get(session_id) or []
        now = time.time()
        for tweet_id, ts in reversed(records):
            if now - ts <= 1800:
                return tweet_id
        return None

    # ------------------------------------------------------------------ #
    # 冷却与防刷屏
    # ------------------------------------------------------------------ #

    def check_and_mark_cooldown(self, session_id: str, tweet_id: str) -> bool:
        if self.cooldown_seconds <= 0:
            return False
        key = f"{session_id}:{tweet_id}"
        now = time.time()
        last = self._cooldown.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return True
        self._cooldown[key] = now

        # 定期清理过期条目，避免字典无限增长
        if len(self._cooldown) > 2000:
            expire_before = now - max(self.cooldown_seconds, 60) * 2
            self._cooldown = {
                k: v for k, v in self._cooldown.items() if v >= expire_before
            }
        return False

    def _mark_media_sent(self, session_id: str, tweet_id: str) -> None:
        self._sent_media[f"{session_id}:{tweet_id}"] = time.time()
        if len(self._sent_media) > 2000:
            cutoff = time.time() - 3600
            self._sent_media = {k: v for k, v in self._sent_media.items() if v >= cutoff}

    def _media_already_sent(self, session_id: str, tweet_id: str) -> bool:
        ts = self._sent_media.get(f"{session_id}:{tweet_id}")
        return ts is not None and time.time() - ts < 3600

    def _mark_images_sent(self, session_id: str, tweet_id: str) -> None:
        self._sent_images[f"{session_id}:{tweet_id}"] = time.time()
        if len(self._sent_images) > 2000:
            cutoff = time.time() - 3600
            self._sent_images = {
                k: v for k, v in self._sent_images.items() if v >= cutoff
            }

    def _images_already_sent(self, session_id: str, tweet_id: str) -> bool:
        ts = self._sent_images.get(f"{session_id}:{tweet_id}")
        return ts is not None and time.time() - ts < 3600

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #

    def _build_video_chain(self, data: TweetData, path: Optional[Path], url: str) -> list[Any]:
        """构建视频消息段，优先发送本地文件。

        视频前面必须带上作者、发布时间与正文等上下文，否则单独一个视频
        看不出是谁发的、内容是什么。
        """
        caption = self._build_video_caption(data)
        if path is not None and path.exists():
            return [
                Comp.Plain(escape_zero_width(caption)),
                Comp.Video.fromFileSystem(path=str(path)),
            ]
        return [Comp.Plain(escape_zero_width(f"{caption}\n{url}"))]

    def _build_caption_lines(
        self, data: TweetData, title: str, empty_note: str = ""
    ) -> list[str]:
        """所有「作者 + 时间 + 正文」消息头（主消息 / 视频说明）共用的骨架。

        P3-5：原先 `_build_header` 与 `_build_video_caption` 各写了一遍同样的
        首行、时间行与正文拼接，这里抽成一处，`title` 只决定首行前缀（🐦 / 🎬）。
        """
        lines = [f"{title}{data.author_display}"]
        created = format_relative_time(data.created_timestamp)
        if created:
            lines.append(f"🕒 {created}")

        body_lines = self._build_body_lines(data)
        if body_lines:
            lines.append("")
            lines.extend(body_lines)
        elif empty_note:
            # 正文为空时退化为一句话，至少说明这条媒体属于谁
            lines.append("")
            lines.append(empty_note)
        return lines

    def _build_video_caption(self, data: TweetData) -> str:
        """视频消息的说明文字：作者 + 发布时间 + 正文（与主消息保持同样的展示模式）。"""
        return "\n".join(
            self._build_caption_lines(data, "🎬 ", "（本条推文没有文字内容）")
        )

    def _build_header(self, data: TweetData) -> str:
        lines = self._build_caption_lines(data, "🐦 ")

        if data.quote_text or data.quote_translation_text:
            # 引用部分同样遵守「正文展示」三档设置：开启 thread_split 时主贴会
            # 单独成条（带完整译文），这里保留摘要作为上下文；关闭时至少补上译文。
            want_original = self.text_content_mode in ("both", "original")
            want_translation = self.text_content_mode in ("both", "translation")
            lines.append("")
            if data.quote_text and (want_original or not data.quote_translation_text):
                label = "🔁 引用：" if want_original else "🔁 引用（⚠️ 翻译不可用）："
                lines.append(label + truncate_text(data.quote_text, 300))
            if data.quote_translation_text and want_translation:
                label = "🔁 引用译文：" if data.quote_text and want_original else "🔁 引用："
                lines.append(label + truncate_text(data.quote_translation_text, 300))

        if self.show_stats_footer:
            stats = []
            if data.likes is not None:
                stats.append(f"❤️ {format_count(data.likes)}")
            if data.retweets is not None:
                stats.append(f"🔁 {format_count(data.retweets)}")
            if data.replies is not None:
                stats.append(f"💬 {format_count(data.replies)}")
            if data.views is not None:
                stats.append(f"👁 {format_count(data.views)}")
            if stats:
                lines.append("")
                lines.append(" · ".join(stats))

        if data.source == "oembed":
            lines.append("")
            lines.append("⚠️ 内容来自 oEmbed 兜底解析，可能不含媒体")

        return escape_zero_width("\n".join(lines))

    def _build_body_lines(self, data: TweetData) -> list[str]:
        """按配置决定发送原文、译文还是两者都发。

        - `both`：原文 + 译文（译文单独成段）
        - `original`：只发原文
        - `translation`：只发译文；这次没拿到译文（翻译失败、原文已是目标语言）
          时退回原文，总比什么都不发好
        """
        original = data.text or ""
        translation = data.translation_text or ""
        mode = self.text_content_mode
        want_original = mode in ("both", "original")
        want_translation = mode in ("both", "translation")

        lines: list[str] = []
        if want_original and original:
            lines.append(truncate_text(original))
        if want_translation and translation:
            translation_lines = self._build_translation_lines(data)
            if not lines:
                # 前面没有原文时去掉译文块开头的空行，避免出现连续空行
                translation_lines = [line for line in translation_lines if line]
            lines.extend(translation_lines)

        if lines:
            return lines
        if original:
            if mode == "translation":
                # 原文已经是目标语言（接口 lang 字段判定，或 lang 缺失时按字符集兜底）时
                # 只是「不需要翻译」，不是「翻译不可用」，不该加提示
                if already_in_target_lang(data, self.active_translate_lang):
                    return [truncate_text(original)]
                # 只发译文却真的没拿到译文：退回原文并说明原因，免得用户以为插件坏了
                return [TRANSLATION_UNAVAILABLE_HINT, truncate_text(original)]
            return [truncate_text(original)]
        return ["（本条推文没有文字内容）"]

    def _build_translation_lines(self, data: TweetData) -> list[str]:
        """生成译文段落；没有译文时返回空列表。"""
        if not data.translation_text:
            return []
        lang = data.translation_lang or self.active_translate_lang
        provider = data.translation_provider or ""
        label = f"🌐 译文（{lang}）" if lang else "🌐 译文"
        if provider:
            label += f" · {provider}"
        # 译文里可能出现与原文相同的 @ 与链接，同样需要打断以免被 QQ 误识别成 at
        return ["", label, truncate_text(data.translation_text)]

    @staticmethod
    def _translation_source_text(data: TweetData) -> str:
        """一条推文的译文来源（日志用，方便核对到底用的免费译文还是大模型）。"""
        if data.translation_text:
            return data.translation_provider or "未知来源"
        if not data.text:
            return "无正文"
        return "无译文"

    def _log_translation_source(self, data: TweetData, prefix: str = "") -> None:
        """把「正文语言 + 译文来源」写进日志（INFO）。

        群里看到的只是消息正文里的标签，出问题时翻日志看不到来源，
        所以每条推文（以及楼中楼每一层）都单独记一行。
        """
        if not data or not data.ok:
            return
        logger.info(
            f"[{PLUGIN_NAME}] {prefix}推文 {data.tweet_id} 正文语言="
            f"{data.lang or '未知'}，译文来源={self._translation_source_text(data)}"
        )

    async def _react_emoji(self, event: AstrMessageEvent) -> bool:
        """给用户发来的消息贴一个表情回应（OneBot 扩展 action）。

        说明：
            - AstrBot 只为 Telegram / Discord / 飞书提供了表情回应接口，
              QQ（aiocqhttp）需要直接调用 OneBot 扩展 action `set_msg_emoji_like`；
            - 不同 QQ 后端对 emoji_id 的编码不同（Lagrange 用 6 位 QQ 表情 ID，
              NapCat 等用 3 位系统表情 ID），因此失败时按 EMOJI_ID_FALLBACKS 重试；
            - 后端不支持该 action 时静默跳过，不影响推文发送。
        """
        if not self.emoji_reply:
            return False

        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if call_action is None:
            logger.debug(f"[{PLUGIN_NAME}] 当前平台不支持表情回应，已跳过贴表情")
            return False

        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if not message_id:
            logger.debug(f"[{PLUGIN_NAME}] 拿不到消息 ID，已跳过贴表情")
            return False

        candidates = [self.emoji_id]
        fallback = EMOJI_ID_FALLBACKS.get(self.emoji_id)
        if fallback and fallback not in candidates:
            candidates.append(fallback)

        for index, emoji_id in enumerate(candidates):
            try:
                await call_action(
                    "set_msg_emoji_like",
                    message_id=str(message_id),
                    emoji_id=str(emoji_id),
                )
                return True
            except Exception as exc:
                still_have_next = index + 1 < len(candidates)
                level = logger.debug if still_have_next else logger.info
                level(
                    f"[{PLUGIN_NAME}] 贴表情失败（emoji_id={emoji_id}）："
                    f"{type(exc).__name__}: {exc}"
                )
        return False

    # ------------------------------------------------------------------ #
    # 自动解析 Handler
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.regex(_TRIGGER_RE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听消息，自动解析其中的 X / Twitter 推文链接或个人主页（群聊与私聊均生效）。

        两个 filter 是 AND 关系：消息类型不限，但正文必须含推文链接、t.co 短链或主页链接。
        引用 / 回复贴的主贴会被二次解析后**单独成一条消息先发**（见 thread_split 配置），
        这样主贴的译文与媒体都能与当前推文区分开。
        """
        if not self.enabled:
            return

        # 本会话的目标语言（/xt 翻译 设置过的会在这里生效）
        self._apply_session_lang(event)

        message = event.message_str or ""
        if not message:
            return
        # 跳过指令消息，避免干扰其它插件与内置指令
        if message.lstrip().startswith(("/", "／", "#")):
            return

        try:
            tweet_ids = extract_tweet_ids(message)
            short_links = _TCO_RE.findall(message)

            # 只有 t.co 短链时，先展开再判断是否指向推文
            if not tweet_ids and short_links:
                for link in short_links[:3]:
                    try:
                        target = await self.parser.expand_tco(link)
                    except Exception as exc:
                        logger.debug(f"[{PLUGIN_NAME}] 展开短链失败 {link}: {exc}")
                        continue
                    for tid in extract_tweet_ids(target):
                        if tid not in tweet_ids:
                            tweet_ids.append(tid)
                    if tweet_ids:
                        break

            profile_handles = (
                extract_profile_handles(message) if self.profile_enabled else []
            )
            if not tweet_ids and not profile_handles:
                return

            # 先把待发送内容收集成「若干条独立消息」，再统一交付给 AstrBot
            messages: list[list[Any]] = []
            # 本轮里用「引用回复气泡」直接发出的条数（这些不走事件管道）
            self._direct_deliveries = 0

            # 主页链接（一条消息里最多解析 1 个，避免刷屏）
            for handle in profile_handles[:1]:
                messages.extend(await self._collect_profile_messages(event, handle))

            # 一条消息里最多处理 2 条推文，避免刷屏
            for tweet_id in tweet_ids[:2]:
                if self.check_and_mark_cooldown(event.unified_msg_origin, tweet_id):
                    logger.debug(f"[{PLUGIN_NAME}] 推文 {tweet_id} 处于冷却期，跳过自动解析")
                    continue
                messages.extend(await self._collect_tweet_messages(event, tweet_id))

            messages = [parts for parts in messages if parts]
            if not messages:
                # 内容可能已经用「引用回复气泡」直接发出去了，同样要终止事件传播，
                # 免得机器人再对链接发起一次 LLM 请求
                if self._direct_deliveries > 0:
                    event.stop_event()
                return

            # 消息条数超过上限时（比如一条消息里带了好几个链接），
            # 把多出来的部分合并到同一条里
            if len(messages) > MAX_AUTO_MESSAGES:
                head = messages[: MAX_AUTO_MESSAGES - 1]
                tail = self._merge_chains(messages[MAX_AUTO_MESSAGES - 1 :])
                messages = [*head, tail]

            # AstrBot 事件管道一个 handler 只能交付一个 MessageEventResult
            # （StarRequestSubStage 会 clear_result 后再进入发送阶段），
            # 因此前面几条用 event.send() 即时发出，最后一条用 chain_result 交付。
            pending = list(messages)
            while len(pending) > 1:
                if not await self._send_now(event, pending[0]):
                    break
                pending.pop(0)
            if len(pending) > 1:
                # 无法分条发送（平台或版本不支持）时降级为一条消息
                pending = [self._merge_chains(pending)]

            yield event.chain_result(pending[0])
            # 自动解析完成后终止事件传播，避免机器人再对链接发起一次 LLM 请求
            event.stop_event()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[{PLUGIN_NAME}] 自动解析消息时出错: {exc}", exc_info=True)
            if self.send_error_hint:
                yield event.plain_result("推文解析出错了，请稍后再试。")

    async def _resolve_tweet(self, tweet_ref: str) -> TweetData:
        """取推文数据：优先用缓存；缓存里的译文不符合当前目标语言时重新解析。"""
        tweet_id = normalize_tweet_ref(tweet_ref) or tweet_ref
        data = await self._cache_get(tweet_id)
        if data is not None and self._translation_needed(data):
            fresh = await self._parse_for_send(tweet_id)
            if fresh.ok:
                data = fresh
        if data is None:
            data = await self._parse_for_send(tweet_id)
        return data

    def _parse_block_reason(self, data: TweetData) -> str:
        """命中解析黑名单时返回原因（作者 / 关键词），否则返回空字符串。"""
        if not data:
            return ""
        author = (data.author_screen_name or "").lstrip("@").strip().lower()
        if author and author in self.blocked_authors:
            return f"作者黑名单 @{author}"
        text = "\n".join(part for part in (data.text, data.translation_text) if part)
        hit = self._match_keywords(text, self.blocked_keywords)
        if hit:
            return f"关键词黑名单「{hit}」"
        return ""

    @staticmethod
    def _merge_chains(chains: list[list[Any]]) -> list[Any]:
        """把多条消息链合并成一条（中间插入分隔线）。"""
        merged: list[Any] = []
        for chain in chains:
            if not chain:
                continue
            if merged:
                merged.append(Comp.Plain("\n————————————\n"))
            merged.extend(chain)
        return merged

    async def _send_now(self, event: AstrMessageEvent, parts: list[Any]) -> bool:
        """立刻单独发送一条消息链。

        平台 / AstrBot 版本不支持 event.send() 时返回 False，
        由调用方降级为「全部合并成一条」。
        """
        if not parts or MessageChain is None:
            return False
        sender = getattr(event, "send", None)
        if sender is None:
            return False
        try:
            await sender(MessageChain(parts))
            return True
        except Exception as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] 分条发送失败（{exc}），改为合并成一条消息发送"
            )
            return False

    async def _collect_tweet_messages(
        self, event: AstrMessageEvent, tweet_id: str
    ) -> list[list[Any]]:
        """解析一条推文并返回要发送的消息链列表。

        返回两条链表示「主贴单独一条 + 本推文一条」；返回一条表示合并发送。
        楼中楼（多层引用 / 回复链）按 thread_delivery 交付：
        - both（默认）：先按「引用回复气泡」逐层发一遍，再补一条合并转发汇总；
        - bubble：只发逐层气泡（需要后端支持 reply 段）；
        - forward：只发合并转发（楼层数达到 thread_merge_min_nodes 时）。
        顺序为 一级主贴 → … → 本条链接；命中黑名单的楼层只留占位说明，
        并且会继续往上追溯（不会因为中间一层被拦就把整条楼中楼截断）。
        命中黑名单、解析失败等情形都在这里降级成可读的中文提示。
        """
        data = await self._resolve_tweet(tweet_id)

        if not data.ok:
            chain = await self._build_chain(event, tweet_id, data=data, react=False)
            return [chain] if chain else []

        reason = self._parse_block_reason(data)
        if reason:
            logger.info(f"[{PLUGIN_NAME}] 推文 {tweet_id} 命中{reason}，跳过解析")
            if not self.send_error_hint:
                return []
            return [[Comp.Plain(f"🚫 这条推文命中{reason}，已跳过解析。")]]

        self._log_translation_source(data)

        # ---- 楼中楼：沿引用 / 回复关系一层层往上找 ----
        # ladder 顺序为 一级主贴 → … → 本条链接；direct_parent 是直接主贴
        # （命中黑名单的楼层也会留在 ladder 里，用 skip_reason 标记成占位）
        ladder: list[tuple[str, TweetData]] = []
        direct_parent: Optional[TweetData] = None
        if self.thread_split and self.thread_chain_depth > 1 and data.parent_tweet_id:
            ladder, direct_parent = await self._resolve_thread_ladder(tweet_id, data)
            if len(ladder) >= 2:
                bubble_ok = False
                bubble_tried = False
                want_bubble = self.thread_delivery == "bubble"
                want_forward = self.thread_delivery == "forward"
                want_card = self.thread_delivery == "card"
                # X 风格卡片：整条楼中楼渲染成一张长图（楼层之间画灰色连接线）
                if want_card:
                    card_path = await self._render_thread_card(ladder)
                    if card_path:
                        card_messages: list[list[Any]] = [
                            [Comp.Image.fromFileSystem(card_path)]
                        ]
                        # 卡片不内嵌媒体，各层的图片 / 视频必须另外发，
                        # 否则「一级主贴」等楼层的媒体会被整条吞掉
                        card_messages.extend(
                            await self._build_thread_card_media_messages(event, ladder)
                        )
                        # 与合并转发 / 逐层气泡保持一致：各层都进缓存、计入统计，
                        # 并记录「会话最近推文」供无参数的 LLM 工具回溯
                        for tid, layer in ladder:
                            if layer.skip_reason:
                                continue
                            await self._cache_put(layer)
                            self.run_count += 1
                        self._record_session_tweet(
                            event.unified_msg_origin, ladder[-1][0]
                        )
                        return card_messages
                    logger.warning(
                        f"[{PLUGIN_NAME}] 楼中楼卡片渲染不可用，回退为合并转发"
                    )
                    want_forward = True
                # 引用回复气泡：第 1 层引用用户那条消息，之后每层引用上一层
                if want_bubble and self._supports_reply_bubble(event):
                    bubble_tried = True
                    bubble_ok = await self._send_thread_as_replies(event, ladder)
                    if bubble_ok and not want_forward:
                        last_id, last_data = ladder[-1]
                        return await self._thread_video_messages(
                            event, last_id, last_data
                        )
                # 合并转发：楼层数够时才做（只发气泡但后端不支持 / 气泡失败时也回退到这里）
                if (
                    (want_forward or (bubble_tried and not bubble_ok))
                    and len(ladder) >= max(2, self.thread_merge_min_nodes)
                    and self._supports_forward(event)
                ):
                    return await self._build_thread_forward(
                        event, ladder, react=not bubble_ok
                    )
                if bubble_ok:
                    last_id, last_data = ladder[-1]
                    return await self._thread_video_messages(event, last_id, last_data)

        chains: list[list[Any]] = []

        # 引用 / 回复的主贴：二次解析后单独发一条（带译文与自己的媒体）
        # card 模式下不单独发主贴：引用信息已经渲染在卡片的引用区域里（更接近 X 的观感）
        parent_id = data.parent_tweet_id
        if self.thread_split and parent_id and self.tweet_delivery != "card":
            parent = (
                direct_parent
                if direct_parent is not None
                else await self._resolve_tweet(parent_id)
            )
            parent_reason = self._parse_block_reason(parent) if parent.ok else ""
            if parent.ok and not parent_reason:
                label = (
                    "🔁 引用的主贴"
                    if data.parent_kind == "quote"
                    else f"↩ 被回复的主贴"
                )
                parent_parts = await self._build_chain(
                    event, parent_id, data=parent, react=False, tag=label
                )
                if parent_parts:
                    chains.append(parent_parts)
            elif parent_reason:
                logger.info(
                    f"[{PLUGIN_NAME}] 主贴 {parent_id} 命中{parent_reason}，不单独发送"
                )
            else:
                logger.debug(f"[{PLUGIN_NAME}] 主贴 {parent_id} 解析失败：{parent.error}")

        chain = await self._build_chain(event, tweet_id, data=data)
        if chain:
            chains.append(chain)
        # card 模式：卡片只负责文字，媒体另行发送（图片进合并转发、视频单独发）
        if self.tweet_delivery == "card":
            chains.extend(await self._build_card_media_messages(event, data))
        return chains

    # ------------------------------------------------------------------ #
    # 楼中楼：多层引用 / 回复链
    # ------------------------------------------------------------------ #

    async def _resolve_thread_ladder(
        self, tweet_id: str, data: TweetData
    ) -> tuple[list[tuple[str, TweetData]], Optional[TweetData]]:
        """沿「引用 / 回复」关系向上追溯楼中楼。

        返回 (ladder, direct_parent)：
            - ladder：按 `一级主贴 → … → 本条链接` 排好序的楼层列表；
            - direct_parent：直接主贴的解析结果（即使它命中黑名单 / 属于别的
              分支也返回，供调用方复用，避免重复请求）。

        最多追溯 `thread_chain_depth` 层；遇到解析失败或成环就停止。中间某层命中
        解析黑名单时**不会截断整条楼中楼**：该层会以占位节点（skip_reason）留在
        ladder 里，只发一句「已跳过」的说明，然后继续往上追溯更上面的楼层。
        """
        ladder: list[tuple[str, TweetData]] = [(tweet_id, data)]
        seen: set[str] = {tweet_id}
        direct_parent: Optional[TweetData] = None
        current = data

        while len(ladder) < self.thread_chain_depth:
            parent_id = current.parent_tweet_id
            if not parent_id or parent_id in seen:
                if parent_id and parent_id in seen:
                    logger.debug(
                        f"[{PLUGIN_NAME}] 楼中楼出现环（{parent_id} 已出现过），停止向上追溯"
                    )
                break

            parent = await self._resolve_tweet(parent_id)
            if direct_parent is None:
                direct_parent = parent

            if not parent.ok:
                logger.debug(
                    f"[{PLUGIN_NAME}] 楼中楼上一层 {parent_id} 解析失败"
                    f"（{parent.error}），只发已取到的 {len(ladder)} 层"
                )
                break

            parent_reason = self._parse_block_reason(parent)
            if parent_reason:
                # 命中黑名单的楼层：正文与媒体都不发，只留占位说明，
                # 然后继续沿它的主贴往上找（否则用户就看不到更上面的楼层了）
                parent.skip_reason = parent_reason
                ladder.append((parent_id, parent))
                seen.add(parent_id)
                current = parent
                logger.info(
                    f"[{PLUGIN_NAME}] 楼中楼上一层 {parent_id} 命中{parent_reason}，"
                    f"该层改为占位并继续向上追溯"
                )
                continue

            self._log_translation_source(parent, prefix=f"楼中楼上一层 {parent_id}：")
            ladder.append((parent_id, parent))
            seen.add(parent_id)
            current = parent

        ladder.reverse()
        return ladder, direct_parent

    @staticmethod
    def _thread_level_tag(index: int, total: int) -> str:
        """楼中楼每一层的层号标签（第 1 层是最上面的一级主贴）。"""
        if total <= 1:
            return ""
        level = index + 1
        if index == 0:
            return "📌 第 1 层 · 一级主贴"
        if index == total - 1:
            return f"📌 第 {level} 层 · 本条链接"
        return f"📌 第 {level} 层"

    @staticmethod
    def _node_display_name(data: TweetData) -> str:
        """合并转发节点里显示的发送者名字（直接复用 TweetData.author_display）。"""
        name = data.author_display
        return "X" if name == "未知作者" else name

    @staticmethod
    def _supports_forward(event: AstrMessageEvent) -> bool:
        """当前平台是否支持合并转发（Node / Nodes）。

        只有 aiocqhttp 适配器实现了 Node/Nodes，其它平台发出去可能变成空气，
        所以这些平台上自动退回老逻辑（主贴单独一条 + 本条一条）。
        取不到平台名时按支持处理（本插件主要部署在 OneBot 上）。
        """
        getter = getattr(event, "get_platform_name", None)
        try:
            name = str(getter() or "").strip().lower() if callable(getter) else ""
        except Exception:
            name = ""
        return name in ("", "aiocqhttp")

    def _thread_state_text(self) -> str:
        """楼中楼当前状态（`/xt 状态` 用），关闭时返回空串。"""
        if not (self.thread_split and self.thread_chain_depth > 1):
            return ""
        return (
            f"最多 {self.thread_chain_depth} 层，"
            f"{self.thread_merge_min_nodes} 层起合并转发，"
            f"节点引用块={'开' if self.thread_forward_quote else '关'}，"
            f"交付={self._thread_delivery_text()}"
        )

    def _thread_delivery_text(self) -> str:
        """楼中楼交付方式的中文说明。"""
        return THREAD_DELIVERY_CHOICES.get(self.thread_delivery, self.thread_delivery)

    def _thread_log_text(self) -> str:
        """楼中楼启动日志摘要，关闭时返回「关」。"""
        if not (self.thread_split and self.thread_chain_depth > 1):
            return "关"
        return (
            f"最多 {self.thread_chain_depth} 层 / "
            f"{self.thread_merge_min_nodes} 层起合并转发 / "
            f"节点引用块={'开' if self.thread_forward_quote else '关'} / "
            f"交付={self._thread_delivery_text()}"
        )

    @staticmethod
    def _supports_reply_bubble(event: AstrMessageEvent) -> bool:
        """当前后端是否支持真正的「引用回复气泡」（reply 段）。

        需要平台是 aiocqhttp（或取不到平台名）并且拿得到 `bot.call_action` ——
        只有直接调用协议端 API 才能拿到每层楼的 message_id 供下一层引用。
        取不到时返回 False，调用方自动回退到合并转发 / 分条发送。
        """
        if not XTweetPlugin._supports_forward(event):
            return False
        bot = getattr(event, "bot", None)
        return callable(getattr(bot, "call_action", None))

    @staticmethod
    def _event_str(event: AstrMessageEvent, getter_name: str) -> str:
        """安全地取 event.get_xxx() 的字符串结果。"""
        getter = getattr(event, getter_name, None)
        try:
            return str(getter() or "").strip() if callable(getter) else ""
        except Exception:
            return ""

    def _admin_ids(self) -> set[str]:
        """AstrBot 全局配置里的管理员 ID 列表（admins_id 可能是字符串或列表）。"""
        ids: set[str] = set()
        try:
            config = self.context.get_config()
            raw = config.get("admins_id") if hasattr(config, "get") else None
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 读取管理员列表失败：{exc}")
            return ids
        if isinstance(raw, (list, tuple, set)):
            items = list(raw)
        elif isinstance(raw, str):
            items = re.split(r"[,\s]+", raw)
        else:
            items = []
        for item in items:
            text = str(item).strip()
            if text:
                ids.add(text)
        return ids

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        """消息是否来自管理员。

        AstrBot 有两个「管理员」概念：平台角色（event.is_admin()，例如群主 /
        频道管理员）和全局配置 admins_id。任一命中都算管理员。
        """
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        sender_id = self._event_str(event, "get_sender_id")
        return bool(sender_id) and sender_id in self._admin_ids()

    @classmethod
    def _event_message_id(cls, event: AstrMessageEvent) -> str:
        """用户那条消息的 message_id（引用回复气泡第 1 层要引用它）。"""
        value = getattr(getattr(event, "message_obj", None), "message_id", None)
        text = str(value).strip() if value is not None else ""
        return "" if text in ("", "0") else text

    @staticmethod
    def _node_uin(event: AstrMessageEvent) -> str:
        """合并转发节点使用的 QQ 号：优先用机器人自己的号，取不到就用 0。"""
        getter = getattr(event, "get_self_id", None)
        try:
            self_id = str(getter() or "").strip() if callable(getter) else ""
        except Exception:
            self_id = ""
        return self_id or "0"

    async def _render_image(self, tweet_id: str, url: str, label: str) -> Any:
        """下载一张图片并渲染成消息组件。

        成功返回 `Comp.Image`，失败（下载错误 / 超过大小上限）降级为
        `{label}{链接}` 纯文本。主消息与合并转发节点共用这一条分支，
        避免「成功 → Image、失败 → Plain」的写法散落在多处（P3-5）。
        """
        sized_url = apply_image_size(url, self.image_size)
        local = await self.downloader.download_image(
            tweet_id, sized_url, variant=self.image_size
        )
        if local is not None:
            return Comp.Image.fromFileSystem(path=str(local))
        return Comp.Plain(f"{label}{sized_url}")

    async def _download_node_images(self, data: TweetData) -> list[Any]:
        """下载一条推文的图片，供合并转发节点使用；失败降级成图片直链。"""
        images: list[Any] = []
        for url in data.images[: self.max_images]:
            images.append(await self._render_image(data.tweet_id, url, "🖼 图片："))
        extra = len(data.images) - self.max_images
        if extra > 0:
            images.append(Comp.Plain(f"（另有 {extra} 张图片未发送）"))
        if images:
            images.insert(0, Comp.Plain("\n"))
        return images

    # ------------------------------------------------------------------ #
    # X 风格卡片（tweet_delivery=card）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _card_date(timestamp: Optional[int]) -> str:
        """X 风格日期：当年显示「10月10日」，跨年显示「2024年12月1日」。"""
        if not timestamp:
            return ""
        try:
            created = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone()
        except Exception:
            return ""
        now = datetime.now().astimezone()
        if created.year == now.year:
            return f"{created.month}月{created.day}日"
        return f"{created.year}年{created.month}月{created.day}日"

    async def _image_data_uri(
        self, url: str, cache_key: str, variant: str, limit: int = 512 * 1024
    ) -> str:
        """下载图片并转成 data URI（失败返回空串，模板会退化成占位样式）。

        实测云端渲染服务器拉不到 pbs.twimg.com（直链会显示破图），所以卡片里的
        头像与横幅都必须 base64 内嵌；这两张图体积都很小，对请求体没有压力。
        """
        if not url:
            return ""
        try:
            local = await self.downloader.download_image(
                cache_key, url, variant=variant
            )
            if local is None:
                return ""
            path = Path(local)
            blob = await asyncio.to_thread(path.read_bytes)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 图片内嵌失败：{exc}")
            return ""
        if not blob or len(blob) > limit:
            return ""
        mime = {
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "image/jpeg")
        return f"data:{mime};base64," + base64.b64encode(blob).decode()

    async def _avatar_data_uri(self, data: TweetData) -> str:
        """推文作者头像的 data URI。"""
        return await self._image_data_uri(
            _profile_avatar_url(data.author_avatar),
            f"{data.tweet_id}_avatar",
            "avatar",
        )

    async def _profile_banner_data_uri(self, profile: ProfileData) -> str:
        """主页横幅的 data URI（横幅比头像大得多，单独放宽体积上限）。"""
        return await self._image_data_uri(
            profile.banner,
            f"{PROFILE_CACHE_PREFIX}{profile.handle}_banner",
            "banner",
            limit=1024 * 1024,
        )

    def _card_theme(self) -> dict[str, str]:
        """当前卡片主题配色。

        auto 模式按配置的深色时段切换：card_dark_start_hour 点之后、到次日
        card_dark_end_hour 点之前用深色，其余时间用浅色。
        """
        mode = self.card_theme
        if mode == "auto":
            hour = datetime.now().hour
            dark = hour >= self.card_dark_start_hour or hour < self.card_dark_end_hour
            mode = "dark" if dark else "light"
        return dict(X_THEME_DARK if mode == "dark" else X_THEME_LIGHT)

    def _card_stats(self, data: TweetData) -> list[dict[str, str]]:
        """卡片互动栏（结构化：kind + 已格式化数值），模板按 kind 选对应 SVG 图标。

        四项始终都给（X 就是这么显示的，哪怕是 0 也会画个图标），缺失的数据按 0 处理。
        顺序与 X 一致：回复 / 转发 / 点赞 / 浏览。
        """
        if not self.show_stats_footer:
            return []
        return [
            {"kind": "reply", "value": format_count(data.replies or 0)},
            {"kind": "retweet", "value": format_count(data.retweets or 0)},
            {"kind": "like", "value": format_count(data.likes or 0)},
            {"kind": "views", "value": format_count(data.views or 0)},
        ]

    @staticmethod
    def _card_text(text: str, limit: int = 1200) -> str:
        """卡片正文：保留换行、放宽长度上限，超长时用省略号（不加「已截断」提示）。

        与文本交付不同：卡片有足够空间，X 本身也会显示长文，所以这里只做极端长度保护。
        """
        body = (text or "").strip()
        if len(body) <= limit:
            return body
        return body[:limit].rstrip() + "…"

    async def _card_item(
        self,
        data: TweetData,
        *,
        with_quote: bool = True,
        avatar: Optional[str] = None,
        media_suffix: str = "（见下方）",
    ) -> dict[str, Any]:
        """把一条推文转成卡片模板里的一个 item（单推文 / 楼中楼层 / 搜索结果共用）。

        media_suffix 会拼在媒体提示后面：推文卡片后面确实跟着媒体，所以默认写
        「（见下方）」；主页卡片不发推文媒体，调用方传空串。
        """
        translation = ""
        text = ""
        if self.text_content_mode != "original" and data.translation_text:
            translation = self._card_text(data.translation_text)
            if self.text_content_mode == "both":
                text = self._card_text(data.text)
        else:
            text = self._card_text(data.text)

        images = len(data.images)
        videos = len(data.videos)
        media_hint = ""
        if images or videos:
            chunks: list[str] = []
            if images:
                chunks.append(f"{images} 张图片")
            if videos:
                chunks.append(f"{videos} 个视频")
            media_hint = " · ".join(chunks) + media_suffix

        quote = None
        if with_quote and (data.quote_text or data.quote_url):
            handle = ""
            if data.quote_url:
                match = re.search(
                    r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/", data.quote_url
                )
                if match:
                    handle = match.group(1)
            quote_body = (data.quote_text or "").strip()
            prefix = f"@{handle}: " if handle else ""
            if prefix and quote_body.startswith(prefix):
                quote_body = quote_body[len(prefix) :]
            quote = {
                "name": handle or "引用推文",
                "handle": handle,
                "date": "",
                "text": self._card_text(quote_body, 400),
            }

        name = data.author_name or data.author_screen_name or "未知作者"
        return {
            "name": name,
            "initial": (name[:1] or "X").upper(),
            "handle": data.author_screen_name,
            "avatar": avatar if avatar is not None else await self._avatar_data_uri(data),
            "verified": bool(data.author_verified),
            "badge_type": data.author_verified_type or "individual",
            "date": self._card_date(data.created_timestamp),
            "translation": translation,
            "text": text,
            "media_hint": media_hint,
            "quote": quote,
            "url": "",
            "stats": self._card_stats(data),
        }

    @staticmethod
    def _trim_card_whitespace(path: str) -> None:
        """裁掉卡片图片四周多余的纯色背景（就地覆盖）。

        背景色取图片左上角像素，因此浅色主题（白底）与深色主题（黑底）都适用。
        实测云端渲染服务按固定视口高度出图，内容较短时底部会留一大片空白。
        Pillow 是 AstrBot 的既有依赖，缺失时静默跳过。
        """
        try:
            from PIL import Image, ImageChops

            with Image.open(path) as raw:
                image = raw.convert("RGB")
            background = Image.new("RGB", image.size, image.getpixel((0, 0)))
            diff = ImageChops.difference(image, background).convert("L")
            bbox = diff.point(lambda p: 255 if p > 12 else 0).getbbox()
            if not bbox:
                return
            pad = 20
            left = max(0, bbox[0] - pad)
            top = max(0, bbox[1] - pad)
            right = min(image.size[0], bbox[2] + pad)
            bottom = min(image.size[1], bbox[3] + pad)
            if (right - left, bottom - top) == image.size:
                return
            image.crop((left, top, right, bottom)).save(path, "PNG", optimize=True)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 卡片裁白边失败（忽略）：{exc}")

    async def _render_template(self, tmpl: str, payload: dict[str, Any]) -> str:
        """渲染任意卡片模板（统一注入主题配色），返回本地路径；失败返回空串。"""
        payload = {"theme": self._card_theme(), **payload}
        try:
            path = await self.html_render(
                tmpl,
                payload,
                return_url=False,
                options={"full_page": True, "type": "png"},
            )
            if path:
                await asyncio.to_thread(self._trim_card_whitespace, str(path))
                return str(path)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 卡片渲染失败：{exc}")
        return ""

    async def _render_card(self, payload: dict[str, Any]) -> str:
        """渲染 X 风格卡片（推文统一模板），返回本地路径；失败返回空串。"""
        return await self._render_template(X_CARD_TMPL, payload)

    async def _render_tweet_card(self, data: TweetData) -> str:
        """单条推文的卡片；模板失败时回退本地文转图（纯文本），再失败返回空串。

        调用方拿到空串会回退成图文混排发送，保证结果一定发得出去。
        """
        try:
            item = await self._card_item(data)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 卡片数据组装失败：{exc}")
            return ""
        path = await self._render_card({"show_line": False, "items": [item]})
        if path:
            return path
        try:
            path = await self.text_to_image(self._build_header(data), return_url=False)
            if path:
                logger.info(f"[{PLUGIN_NAME}] 推文卡片已回退到本地文转图渲染")
                return str(path)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 推文卡片本地渲染失败：{exc}")
        return ""

    async def _render_thread_card(self, ladder: list[tuple[str, TweetData]]) -> str:
        """整条楼中楼渲染成一张 X 风格卡片图（楼层之间画灰色连接线）。

        与 X 的 thread 视图一致：左侧头像列、头像之间用竖线连接；命中黑名单的
        占位楼层用「已跳过」说明替代正文与媒体。
        """
        items: list[dict[str, Any]] = []
        for _tid, data in ladder:
            try:
                item = await self._card_item(data, with_quote=False)
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] 楼层卡片数据组装失败：{exc}")
                continue
            if data.skip_reason:
                item["translation"] = ""
                item["text"] = f"该层命中{data.skip_reason}，已跳过"
                item["media_hint"] = ""
                item["stats"] = []
            items.append(item)
        if not items:
            return ""
        return await self._render_card({"show_line": True, "items": items})

    @staticmethod
    def _profile_meta(profile: ProfileData) -> list[dict[str, str]]:
        """主页卡片的元信息行（位置 / 网站 / 加入时间），kind 决定用哪个图标。

        X 的顺序就是 位置 → 网站 → 加入时间；网站要去掉协议头只显示域名。
        """
        meta: list[dict[str, str]] = []
        location = _clean_str(profile.location)
        if location:
            meta.append({"kind": "location", "text": location})
        website = _clean_str(profile.website)
        if website:
            site = re.sub(r"^https?://", "", website).rstrip("/")
            if site:
                meta.append({"kind": "link", "text": site})
        joined = format_join_date(profile.joined)
        if joined:
            meta.append({"kind": "joined", "text": f"加入于 {joined}"})
        return meta

    async def _render_profile_card(
        self, profile: ProfileData, posts: list[TweetData]
    ) -> str:
        """把主页渲染成一张 X 风格主页卡片；失败返回空串（调用方回退文字资料卡）。

        横幅与头像都必须 base64 内嵌：云端渲染服务器拉不到 pbs.twimg.com。
        """
        try:
            name = profile.name or profile.handle
            # 主页里的推文都来自同一个账号，头像直接复用主页头像：
            # 时间线接口不一定带 author，而且这样也不用为每条推文重复下载一次头像。
            avatar_uri = await self._image_data_uri(
                _profile_avatar_url(profile.avatar),
                f"{PROFILE_CACHE_PREFIX}{profile.handle}",
                "profile",
            )
            payload: dict[str, Any] = {
                "banner": await self._profile_banner_data_uri(profile),
                "avatar": avatar_uri,
                "initial": (name[:1] or "X").upper(),
                "name": name,
                "handle": profile.handle,
                "verified": bool(profile.verified),
                "description": self._card_text(profile.description, 400),
                "meta": self._profile_meta(profile),
                "following": format_count(profile.following or 0),
                "followers": format_count(profile.followers or 0),
                "posts": [],
            }
            for post in posts:
                item = await self._card_item(
                    post, with_quote=False, avatar=avatar_uri, media_suffix=""
                )
                payload["posts"].append(
                    {
                        "name": item["name"],
                        "initial": item["initial"],
                        "handle": item["handle"],
                        "avatar": item["avatar"],
                        "date": item["date"],
                        "translation": item["translation"],
                        "text": item["text"],
                        "media_hint": item["media_hint"],
                        "stats": item["stats"],
                    }
                )
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 主页卡片数据组装失败：{exc}")
            return ""
        return await self._render_template(PROFILE_CARD_TMPL, payload)

    async def _build_card_media_messages(
        self, event: AstrMessageEvent, data: TweetData
    ) -> list[list[Any]]:
        """card 模式的媒体消息：图片按顺序进合并转发（不支持时逐条发），视频单独发。

        视频无法放进 OneBot 的合并转发节点（协议限制），因此始终单独发送。
        """
        messages: list[list[Any]] = []
        session_id = event.unified_msg_origin
        images = data.images[: self.max_images] if self.send_images else []
        if images:
            rendered: list[Any] = []
            for url in images:
                rendered.append(await self._render_image(data.tweet_id, url, "🖼 图片："))
            if self._supports_forward(event):
                uin = self._node_uin(event)
                nodes = [
                    Comp.Node(
                        content=[item],
                        name=f"{self._node_display_name(data)} 的图片 {index}/{len(rendered)}",
                        uin=uin,
                    )
                    for index, item in enumerate(rendered, start=1)
                ]
                messages.append([Comp.Nodes(nodes)])
            else:
                messages.extend([[item] for item in rendered])
            self._mark_images_sent(session_id, data.tweet_id)

        if self.send_videos and data.videos:
            local_video: Optional[Path] = None
            if not self._media_already_sent(session_id, data.tweet_id):
                try:
                    local_video = await self.downloader.download_video(
                        data.tweet_id, data.videos[0]
                    )
                except Exception as exc:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 视频下载异常（{exc}），降级为发送链接"
                    )
                    local_video = None
                self._mark_media_sent(session_id, data.tweet_id)
            messages.append(self._build_video_chain(data, local_video, data.videos[0]))
        return messages

    def _forward_quote_line(self, quote_from: TweetData) -> str:
        """合并转发节点里的文字引用块行：「↩ 回复 @上一层：上一层摘要」。

        摘要按 text_content_mode 取译文 / 原文，压掉换行并截断到约 60 字；
        上一层只是占位（skip_reason）时，说明它命中了什么、已跳过。
        """
        if quote_from.skip_reason:
            return f"↩ 回复 @上一层：该层命中{quote_from.skip_reason}，已跳过"
        translation = quote_from.translation_text or ""
        original = (quote_from.text or "").strip()
        if self.text_content_mode in ("both", "translation") and translation:
            summary = translation
        else:
            summary = original or translation
        summary = truncate_text(summary.replace("\n", " ").strip(), 60)
        who = (
            f"@{quote_from.author_screen_name}"
            if quote_from.author_screen_name
            else "上一层"
        )
        if not summary:
            return f"↩ 回复 {who}：（该层没有文字内容）"
        return f"↩ 回复 {who}：{summary}"

    async def _build_thread_layer_content(
        self,
        event: AstrMessageEvent,
        index: int,
        total: int,
        tid: str,
        data: TweetData,
        mark_images: bool = True,
        quote_from: Optional[TweetData] = None,
    ) -> list[Any]:
        """构建楼中楼某一层的内容（合并转发节点与引用回复气泡共用）。

        内容 = 层号标签 + 作者 / 时间 / 正文（或译文）+ 图片（可选）+ 视频直链 + 推文链接。
        `mark_images=False` 时不在构建阶段登记「图片已发」，由调用方在真正发出后再登记。
        命中解析黑名单的楼层只留一句占位说明（正文与媒体都不发）。
        `quote_from` 非空且 thread_forward_quote 开启时，层号标签后渲染一行文字引用块
        （合并转发节点是本地合成的、没有 message_id，只能用文字表达回复关系；
        引用回复气泡本身自带引用，气泡路径不传该参数）。
        """
        session_id = event.unified_msg_origin
        content: list[Any] = []
        tag = self._thread_level_tag(index, total)
        if tag:
            content.append(Comp.Plain(f"{tag}\n"))

        if quote_from is not None and self.thread_forward_quote:
            quote_line = self._forward_quote_line(quote_from)
            if quote_line:
                content.append(Comp.Plain(f"{quote_line}\n"))

        if data.skip_reason:
            who = f"@{data.author_screen_name} " if data.author_screen_name else ""
            content.append(
                Comp.Plain(f"🚫 {who}该层内容命中{data.skip_reason}，已跳过\n")
            )
            link = data.url or build_tweet_url(data.author_screen_name, tid)
            if link:
                content.append(Comp.Plain(escape_zero_width(f"🔗 {link}")))
            return content

        content.append(Comp.Plain(self._build_header(data)))
        if self.thread_merge_media and self.send_images and data.images:
            content.extend(await self._download_node_images(data))
            if mark_images:
                self._mark_images_sent(session_id, tid)
        if data.videos:
            content.append(
                Comp.Plain(
                    "\n" + escape_zero_width("🎬 视频：" + "\n".join(data.videos[:2]))
                )
            )
        link = data.url or build_tweet_url(data.author_screen_name, tid)
        if link:
            content.append(Comp.Plain(escape_zero_width(f"\n🔗 {link}")))
        return content

    async def _thread_video_messages(
        self, event: AstrMessageEvent, last_id: str, last_data: TweetData
    ) -> list[list[Any]]:
        """链接那层的视频：合并转发 / 引用气泡里都放不了视频，另外真发一条。"""
        session_id = event.unified_msg_origin
        if not (self.send_videos and last_data.videos):
            return []

        local_video: Optional[Path] = None
        if not self._media_already_sent(session_id, last_id):
            try:
                local_video = await self.downloader.download_video(
                    last_data.tweet_id, last_data.videos[0]
                )
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 视频下载异常（{exc}），降级为发送链接")
                local_video = None
            self._mark_media_sent(session_id, last_id)
        if local_video is None:
            return []
        video_chain = self._build_video_chain(
            last_data, local_video, last_data.videos[0]
        )
        return [video_chain] if video_chain else []

    async def _build_thread_card_media_messages(
        self, event: AstrMessageEvent, ladder: list[tuple[str, TweetData]]
    ) -> list[list[Any]]:
        """楼中楼卡片的媒体消息：各层图片按楼层顺序进一条合并转发，视频单独发。

        卡片本身不内嵌媒体（与单推文的 card 模式规则一致），所以每一层的媒体都要
        另外发出去。命中黑名单的楼层不发媒体（正文都跳过了）。
        """
        messages: list[list[Any]] = []
        session_id = event.unified_msg_origin
        uin = self._node_uin(event)

        rendered: list[tuple[str, Any]] = []
        if self.send_images:
            for tid, data in ladder:
                if data.skip_reason or not data.images:
                    continue
                for url in data.images[: self.max_images]:
                    rendered.append(
                        (
                            self._node_display_name(data),
                            await self._render_image(tid, url, "🖼 图片："),
                        )
                    )
                self._mark_images_sent(session_id, tid)

        if rendered:
            if self._supports_forward(event):
                total = len(rendered)
                messages.append(
                    [
                        Comp.Nodes(
                            [
                                Comp.Node(
                                    content=[item],
                                    name=f"{who} 的图片 {index}/{total}",
                                    uin=uin,
                                )
                                for index, (who, item) in enumerate(rendered, start=1)
                            ]
                        )
                    ]
                )
            else:
                messages.extend([[item] for _who, item in rendered])

        # 视频没法放进合并转发节点（协议限制），逐层各发一条
        if self.send_videos:
            for tid, data in ladder:
                if data.skip_reason or not data.videos:
                    continue
                if self._media_already_sent(session_id, tid):
                    continue
                local_video: Optional[Path] = None
                try:
                    local_video = await self.downloader.download_video(
                        tid, data.videos[0]
                    )
                except Exception as exc:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 视频下载异常（{exc}），降级为发送链接"
                    )
                    local_video = None
                self._mark_media_sent(session_id, tid)
                messages.append(
                    self._build_video_chain(data, local_video, data.videos[0])
                )

        return messages

    async def _build_thread_forward(
        self,
        event: AstrMessageEvent,
        ladder: list[tuple[str, TweetData]],
        react: bool = True,
    ) -> list[list[Any]]:
        """把整条楼中楼做成一条「合并转发」（聊天记录）。

        节点顺序：一级主贴 → 二级回复 → … → 本条链接。
        每个节点里是该层的作者、时间、正文 / 译文，以及（可选的）图片与链接。
        视频没法在合并转发里播放，所以节点里只给直链；链接那层如果真有视频，
        另外再真发一条视频消息，免得用户看不到。
        `react=False` 表示贴表情已经做过了（thread_delivery=both 时逐层气泡先发），
        不再重复贴一次。
        """
        session_id = event.unified_msg_origin
        total = len(ladder)
        uin = self._node_uin(event)
        nodes: list[Any] = []

        for index, (tid, data) in enumerate(ladder):
            if not data.skip_reason:
                await self._cache_put(data)
                self.run_count += 1
            content = await self._build_thread_layer_content(
                event,
                index,
                total,
                tid,
                data,
                quote_from=ladder[index - 1][1] if index else None,
            )
            nodes.append(
                Comp.Node(
                    content=content,
                    name=self._node_display_name(data),
                    uin=uin,
                )
            )

        last_id, last_data = ladder[-1]
        self._record_session_tweet(session_id, last_id)
        if react:
            await self._react_emoji(event)

        messages: list[list[Any]] = [[Comp.Nodes(nodes)]]

        # 链接那条推文的视频：合并转发里放不了视频，另外真发一条
        messages.extend(await self._thread_video_messages(event, last_id, last_data))

        logger.info(
            f"[{PLUGIN_NAME}] 推文 {last_id} 处于 {total} 层楼中楼，"
            f"已按「一级主贴 → 本条」顺序合并转发"
        )
        return messages

    async def _parts_to_segments(self, parts: list[Any]) -> list[dict]:
        """把消息段转成 OneBot 段落（优先复用 aiocqhttp 适配器自己的转换器）。

        适配器会把图片转成 base64、丢掉纯空白文本，与 AstrBot 正常发消息的行为一致；
        拿不到适配器（或转换失败）时用内置降级转换。
        """
        if not parts:
            return []
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (  # noqa: E501
                AiocqhttpMessageEvent,
            )

            segments = await AiocqhttpMessageEvent._parse_onebot_json(
                MessageChain(parts)
            )
            if segments:
                return list(segments)
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 复用适配器消息转换失败，改用内置转换：{exc}")

        segments: list[dict] = []
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and not text.strip():
                continue
            segment: Any = None
            to_dict = getattr(part, "toDict", None)
            if callable(to_dict):
                try:
                    segment = to_dict()
                except Exception:
                    segment = None
            if not (isinstance(segment, dict) and segment.get("type")):
                segment = self._component_to_dict(part, text)
            if segment:
                segments.append(segment)
        return segments

    @staticmethod
    def _component_to_dict(part: Any, text: Any = None) -> Optional[dict]:
        """内置降级转换：把一个消息段转成 OneBot 段落。"""
        if text is None:
            text = getattr(part, "text", None)
        kind = str(getattr(part, "type", "") or "").strip().lower()
        if kind in ("plain", "text") or (not kind and isinstance(text, str)):
            return {"type": "text", "data": {"text": text or ""}}
        if not kind:
            return None
        data: dict[str, Any] = {}
        for key in ("file", "url", "path"):
            value = getattr(part, key, None)
            if isinstance(value, str) and value:
                data[key] = value
        if not data:
            return None
        return {"type": kind, "data": data}

    async def _send_with_reply(
        self,
        event: AstrMessageEvent,
        parts: list[Any],
        reply_to: str,
    ) -> tuple[bool, str]:
        """直接调用协议端 API 发一条消息，并返回 (是否发出, 新消息的 message_id)。

        `reply_to` 非空时在消息开头插入 reply 段（真正的「引用回复气泡」）。
        只有走 `bot.call_action` 才能拿到协议端返回的 message_id —— AstrBot 的
        `event.send()` 会把返回值丢掉，所以这里不能用事件管道发送。
        """
        bot = getattr(event, "bot", None)
        action = getattr(bot, "call_action", None)
        if not callable(action) or not parts:
            return False, ""

        group_id = self._event_str(event, "get_group_id")
        sender_id = self._event_str(event, "get_sender_id")
        payload: dict[str, Any] = {}
        if group_id.isdigit():
            api = "send_group_msg"
            payload["group_id"] = int(group_id)
        elif sender_id.isdigit():
            api = "send_private_msg"
            payload["user_id"] = int(sender_id)
        else:
            logger.debug(
                f"[{PLUGIN_NAME}] 会话 ID 不是纯数字（群 {group_id} / 用户 {sender_id}），"
                f"无法用引用回复气泡发送"
            )
            return False, ""

        segments = await self._parts_to_segments(parts)
        if not segments:
            return False, ""
        if reply_to:
            segments.insert(0, {"type": "reply", "data": {"id": str(reply_to)}})

        self_id = self._event_str(event, "get_self_id")
        if self_id.isdigit():
            payload["self_id"] = int(self_id)
        payload["message"] = segments

        try:
            result = await action(api, **payload)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 引用回复气泡发送失败（{api}）：{exc}")
            return False, ""

        message_id = ""
        if isinstance(result, dict):
            value = result.get("message_id")
            message_id = str(value).strip() if value is not None else ""
        return True, message_id

    async def _send_thread_as_replies(
        self,
        event: AstrMessageEvent,
        ladder: list[tuple[str, TweetData]],
    ) -> bool:
        """用真正的「引用回复气泡」把楼中楼一层层发出去。

        第 1 层引用用户发的那条消息，之后每层引用上一层刚发出的消息，形成嵌套的
        引用气泡。返回 True 表示内容已经直接发出（调用方不要再走事件管道）：

        - 第 1 层就失败 → 一条都没发出去，返回 False，调用方回退到合并转发 / 分条；
        - 中途失败 → 该层改用不带 reply 段的普通消息补发一次，成功就继续发完剩余层
          （后面的层不再带引用气泡）；补发也失败才停下，已发出的层保留。
        """
        total = len(ladder)
        if total < 2 or not self._supports_reply_bubble(event):
            return False

        session_id = event.unified_msg_origin
        reply_to = self._event_message_id(event)
        sent = 0
        degraded = False
        for index, (tid, data) in enumerate(ladder):
            content = await self._build_thread_layer_content(
                event, index, total, tid, data, mark_images=False
            )
            ok, message_id = await self._send_with_reply(event, content, reply_to)
            if not ok and index > 0:
                logger.warning(
                    f"[{PLUGIN_NAME}] 楼中楼第 {index + 1} 层引用失败，"
                    f"改用普通消息补发剩余楼层"
                )
                degraded = True
                ok, message_id = await self._send_with_reply(event, content, "")
            if not ok:
                if index == 0:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 楼中楼第 1 层发送失败，"
                        f"回退到合并转发 / 分条发送"
                    )
                    return False
                logger.warning(
                    f"[{PLUGIN_NAME}] 楼中楼在第 {index + 1} 层中断，"
                    f"已发出 {sent} 层"
                )
                break

            sent += 1
            if not data.skip_reason:
                await self._cache_put(data)
                self.run_count += 1
                if self.thread_merge_media and self.send_images and data.images:
                    self._mark_images_sent(session_id, tid)
            # 一旦降级（某层引用失败改用普通消息补发），后面不再带 reply 段
            reply_to = "" if degraded else (message_id or "")

        if sent == 0:
            return False

        self._direct_deliveries += sent
        last_id, last_data = ladder[-1]
        self._record_session_tweet(session_id, last_id)
        await self._react_emoji(event)
        logger.info(
            f"[{PLUGIN_NAME}] 推文 {last_id} 处于 {total} 层楼中楼，"
            f"已用「引用回复气泡」逐层发送 {sent} 条"
        )
        return True

    async def _build_chain(
        self,
        event: AstrMessageEvent,
        tweet_id: str,
        data: Optional[TweetData] = None,
        react: bool = True,
        tag: str = "",
    ) -> list[Any]:
        """解析一条推文，返回要发送的消息段列表（正文 + 图片 + 视频/链接）。

        与 LLM 工具的差别：这里会真正下载媒体并返回 Comp.Image / Comp.Video，
        因此调用方必须把全部消息段放进**同一个** MessageEventResult 里。
        data 为空时自动取缓存或重新解析；tag 非空时在链首插入「引用的主贴」这类小标题。
        """
        session_id = event.unified_msg_origin
        try:
            if data is None:
                data = await self._resolve_tweet(tweet_id)

            if not data.ok:
                logger.warning(f"[{PLUGIN_NAME}] 推文 {tweet_id} 解析失败: {data.error}")
                if not self.send_error_hint:
                    return []
                detail = data.error or "未知原因"
                text = f"⚠️ 这条推文没能解析出来（推文 ID：{tweet_id}）。"
                if "私有" in detail or "权限" in detail:
                    text = "⚠️ 这条推文无法访问，可能来自私有账号或已被删除。"
                elif "不存在" in detail or "删除" in detail:
                    text = "⚠️ 这条推文不存在或已被删除。"
                elif "超时" in detail or "网络" in detail or "失败" in detail:
                    text = "⚠️ 解析超时或网络异常，请检查代理设置后重试。"
                return [Comp.Plain(text)]

            await self._cache_put(data)
            self._record_session_tweet(session_id, tweet_id)
            self.run_count += 1
            # 首次发送后做一次表情回应，失败不影响正文
            if react:
                await self._react_emoji(event)

            parts: list[Any] = []
            if tag:
                parts.append(Comp.Plain(f"{tag}\n"))

            # card 模式：把整条推文渲染成 X 风格卡片（媒体由调用方另行发送）
            if self.tweet_delivery == "card":
                card_path = await self._render_tweet_card(data)
                if card_path:
                    parts.append(Comp.Image.fromFileSystem(card_path))
                    return parts
                logger.warning(
                    f"[{PLUGIN_NAME}] 推文 {tweet_id} 卡片渲染不可用，回退图文混排"
                )

            parts.append(Comp.Plain(self._build_header(data)))

            # 引用主贴的媒体默认跟随主贴自己那条消息发送；
            # 关闭 thread_split 时（主贴不单独发）才并入本条消息，保持旧行为
            extra_images = [] if self.thread_split else list(data.quote_images)

            # ---------------- 图片 ---------------- #
            if self.send_images and (data.images or extra_images):
                images: list[Any] = []
                for url in data.images[: self.max_images]:
                    images.append(await self._render_image(data.tweet_id, url, "🖼 图片："))
                for url in extra_images[: max(0, self.max_images - len(data.images))]:
                    images.append(
                        await self._render_image(
                            f"{data.tweet_id}_quote", url, "🖼 引用图片："
                        )
                    )
                extra = len(data.images) + len(extra_images) - self.max_images
                if extra > 0:
                    images.append(Comp.Plain(f"（另有 {extra} 张图片未发送）"))
                parts.extend(images)
                # 图片已随本条消息发出，记下来供 LLM 工具判断「媒体已发」
                # 注意：与 _sent_media（视频下载闸门）分开记，否则同一条推文的视频会被跳过
                self._mark_images_sent(session_id, tweet_id)

            # ---------------- 视频 ---------------- #
            if self.send_videos and data.videos:
                local_video: Optional[Path] = None
                if not self._media_already_sent(session_id, tweet_id):
                    try:
                        local_video = await self.downloader.download_video(
                            data.tweet_id, data.videos[0]
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[{PLUGIN_NAME}] 视频下载异常（{exc}），降级为发送链接"
                        )
                        local_video = None
                    self._mark_media_sent(session_id, tweet_id)
                parts.extend(self._build_video_chain(data, local_video, data.videos[0]))
            elif data.videos:
                parts.append(
                    Comp.Plain(
                        escape_zero_width("🎬 视频：" + "\n".join(data.videos[:2]))
                    )
                )
            elif not self.thread_split and data.quote_videos:
                # 主贴视频不单独发送时，附上直链
                parts.append(
                    Comp.Plain(
                        escape_zero_width(
                            "🎬 引用视频：" + "\n".join(data.quote_videos[:2])
                        )
                    )
                )

            if data.quote_url:
                parts.append(Comp.Plain(escape_zero_width(f"🔁 原推文：{data.quote_url}")))
            if data.reply_to_url:
                author = f"@{data.reply_to_author} " if data.reply_to_author else ""
                parts.append(
                    Comp.Plain(escape_zero_width(f"↩ 回复：{author}{data.reply_to_url}"))
                )

            return parts
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"[{PLUGIN_NAME}] 处理推文 {tweet_id} 时出现异常: {exc}",
                exc_info=True,
            )
            if self.send_error_hint:
                return [Comp.Plain("推文解析过程中出错了，请稍后再试。")]
            return []

    # ------------------------------------------------------------------ #
    # X 个人主页
    # ------------------------------------------------------------------ #

    def _build_profile_header(self, profile: ProfileData) -> str:
        """生成主页资料卡文本（昵称、简介、统计、加入时间）。"""
        title = f"👤 {profile.display_name}"
        if profile.verified:
            title += " ✅"
        if profile.protected:
            title += "（受保护账号）"
        lines = [title]

        if profile.description:
            lines.append("")
            lines.append(truncate_text(profile.description, 300))

        stats: list[str] = []
        if profile.followers is not None:
            stats.append(f"👥 {format_count(profile.followers)} 粉丝")
        if profile.following is not None:
            stats.append(f"➡️ {format_count(profile.following)} 关注")
        if profile.statuses is not None:
            stats.append(f"🐦 {format_count(profile.statuses)} 推文")
        if stats:
            lines.append("")
            lines.append(" · ".join(stats))

        extra_stats: list[str] = []
        if profile.likes is not None:
            extra_stats.append(f"❤️ {format_count(profile.likes)} 喜欢")
        if profile.media_count is not None:
            extra_stats.append(f"🖼 {format_count(profile.media_count)} 媒体")
        if extra_stats:
            lines.append(" · ".join(extra_stats))

        if profile.location:
            lines.append(f"📍 {profile.location}")
        if profile.website:
            lines.append(f"🔗 {profile.website}")
        joined = format_join_date(profile.joined)
        if joined:
            lines.append(f"📅 加入于 {joined}")

        lines.append("")
        lines.append(f"🔗 {profile.url or f'https://x.com/{profile.handle}'}")
        return escape_zero_width("\n".join(lines))

    async def _fetch_profile_cached(self, handle: str) -> ProfileData:
        """带短时内存缓存的主页解析，避免同一链接被反复请求。"""
        key = (normalize_handle(handle) or handle).lower()
        now = time.time()
        cached = self._profile_cache.get(key)
        if (
            cached is not None
            and now - self._profile_cache_ts.get(key, 0.0) <= PROFILE_CACHE_TTL
        ):
            return cached

        profile = await self.parser.fetch_profile(key)
        if profile.ok:
            self._profile_cache[key] = profile
            self._profile_cache_ts[key] = now
        return profile

    async def _collect_profile_messages(
        self, event: AstrMessageEvent, handle: str
    ) -> list[list[Any]]:
        """解析一个 X 个人主页，返回要发送的消息链。

        card 模式（默认）把主页渲染成一张 X 风格主页卡片；配置成 text 或卡片渲染
        失败时，回退成「文字资料卡 + 头像图 + 最近推文清单」的图文混排。
        """
        if not self.profile_enabled:
            return []

        try:
            profile = await self._fetch_profile_cached(handle)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 解析主页 {handle} 异常：{exc}")
            if not self.send_error_hint:
                return []
            return [[Comp.Plain(f"⚠️ 主页解析出错了：{truncate_text(str(exc), 120)}")]]

        if not profile.ok:
            logger.warning(f"[{PLUGIN_NAME}] 主页 {handle} 解析失败：{profile.error}")
            if not self.send_error_hint:
                return []
            return [[Comp.Plain(f"⚠️ 没能解析主页 {handle}：{profile.error}")]]

        self.run_count += 1

        # 最近推文：只取文字与时间，不下载媒体（卡片与文字清单共用这一份数据）
        posts: list[TweetData] = []
        if self.profile_post_count > 0:
            statuses, error = await self._fetch_profile_statuses(
                profile.handle,
                # 时间线接口会把转帖一起返回，转帖下面会被丢掉，所以多要一些
                count=max(10, min(100, self.profile_post_count * 3)),
            )
            if error:
                logger.debug(f"[{PLUGIN_NAME}] 拉取主页时间线失败：{error}")
            for status in statuses:
                # 只要本人发的推文：转帖的 author 是原作者，显示出来就成了「他人的帖子」
                if status_is_repost(status, profile.handle):
                    continue
                post = self._status_to_tweet(status, profile.handle)
                if post.ok:
                    posts.append(post)
                if len(posts) >= self.profile_post_count:
                    break

        if self.profile_delivery == "card":
            card_path = await self._render_profile_card(profile, posts)
            if card_path:
                return [[Comp.Image.fromFileSystem(path=card_path)]]
            logger.info(f"[{PLUGIN_NAME}] 主页卡片渲染失败，回退文字资料卡")

        parts: list[Any] = [Comp.Plain(self._build_profile_header(profile))]

        avatar = _profile_avatar_url(profile.avatar)
        if self.send_images and avatar:
            local = await self.downloader.download_image(
                f"{PROFILE_CACHE_PREFIX}{profile.handle}",
                avatar,
                variant="profile",
            )
            if local is not None:
                parts.append(Comp.Image.fromFileSystem(path=str(local)))
            else:
                parts.append(Comp.Plain(f"🖼 头像：{avatar}"))

        if posts:
            lines = ["", f"📌 最近推文（{len(posts)} 条）"]
            for index, post in enumerate(posts, 1):
                when = format_relative_time(post.created_timestamp)
                lines.append(f"{index}. {when}".rstrip())
                summary = truncate_text(post.text.replace("\n", " "), 180)
                if summary:
                    lines.append(f"   {summary}")
                link = post.url or build_tweet_url(
                    post.author_screen_name, post.tweet_id
                )
                lines.append(f"   {link}")
            parts.append(Comp.Plain(escape_zero_width("\n".join(lines))))

        return [parts]

    async def _parse_for_send(self, tweet_ref: str) -> TweetData:
        """按当前翻译配置解析推文（自动解析与 /xt 重解析共用）。"""
        mode = self.translate_mode
        if mode == "off":
            return await self.parser.parse(tweet_ref)
        if mode == "fxtwitter":
            # 只用 FxTwitter 自带翻译，不调用大模型
            return await self.parser.parse_with_translation(
                tweet_ref,
                translate_lang=self.active_translate_lang,
                translator=None,
                allow_llm=False,
            )
        allow_llm = mode == "llm" or self.translate_llm_fallback
        return await self.parser.parse_with_translation(
            tweet_ref,
            translate_lang=self.active_translate_lang,
            translator=self.translator if allow_llm else None,
            allow_llm=allow_llm,
        )

    def _translation_needed(self, data: TweetData) -> bool:
        """判断缓存里的推文是否还需要按当前配置补一次翻译。"""
        if self.translate_mode == "off":
            return False
        if data.translation_lang == self.active_translate_lang:
            return False
        return data.needs_translation(self.active_translate_lang)

    # ------------------------------------------------------------------ #
    # LLM 工具（Function Calling）
    # ------------------------------------------------------------------ #

    @filter.llm_tool(name="search_x_posts")
    async def search_x_posts_tool(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        accounts: str = "",
        limit: int = 0,
    ) -> str:
        """在 X（Twitter）上检索最新推文，返回结构化的搜索结果 JSON
        （作者、正文、发布时间、链接、互动数据）。
        当用户想了解某个话题、人物或事件在 X 上的最新动态，例如「X 上最近有什么关于 XX 的消息」
        「帮我看看 XX 最近发了什么」「搜一下 X 上关于 XX 的讨论」时，调用本工具。
        只返回 JSON 字符串，不直接发消息，请根据返回内容用中文总结给用户。
        注意：受接口限制，本工具无法进行全站全文检索。它的工作原理是根据【核心实体】
        （人名 / 公司名 / 产品名）寻找相关账号并检索其近期推文。请务必从用户输入中
        提取核心实体作为 keyword，不要使用长句或事件描述。

        Args:
            keyword(string): 检索关键词，请使用核心实体词（如 Grok、OpenAI、特斯拉），不要长句；留空表示只要这些账号的最新推文
            accounts(string): 要检索的 X 账号，多个用逗号分隔（如 elonmusk,OpenAI）；留空表示使用插件配置里的「搜索账号列表」，配置为空时按关键词自动匹配相关账号
            limit(int): 最多返回多少条结果，0 表示使用插件配置的默认值
        """
        self._apply_session_lang(event)
        try:
            if not self.search_enabled:
                return json.dumps(
                    {
                        "success": False,
                        "error": "插件配置里已关闭 X 搜索功能（search_enabled=false）。",
                    },
                    ensure_ascii=False,
                )

            account_list = self._parse_account_list(accounts) if accounts else []
            keyword_list = self._parse_keyword_list(keyword) if keyword else []
            results, error = await self.search_x_posts(
                accounts=account_list,
                keywords=keyword_list,
                limit=limit,
                translate_lang=self.active_translate_lang,
            )
            payload = self.build_search_payload(
                results, keyword=keyword, error=error, accounts=account_list or None
            )
            if not results and error:
                payload.setdefault(
                    "note",
                    "检索失败，请如实告知用户当前无法访问 X 的搜索接口，不要编造内容。",
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[{PLUGIN_NAME}] 搜索工具执行失败: {exc}", exc_info=True)
            return json.dumps(
                {"success": False, "type": "x_search", "error": f"工具内部错误：{exc}"},
                ensure_ascii=False,
            )

    async def _build_profile_payload(self, handle: str) -> dict[str, Any]:
        """构造主页解析的载荷（LLM 工具与指令共用）。"""
        profile = await self._fetch_profile_cached(handle)
        if not profile.ok:
            return {
                "success": False,
                "type": "profile",
                "handle": normalize_handle(handle) or handle,
                "error": profile.error or "主页解析失败",
                "note": "请如实告知用户无法读取该账号主页，不要编造资料。",
            }

        payload: dict[str, Any] = {
            "success": True,
            "type": "profile",
            "handle": profile.handle,
            "url": profile.url,
            "name": profile.name,
            "description": profile.description,
            "location": profile.location,
            "website": profile.website,
            "joined": profile.joined,
            "verified": profile.verified,
            "protected": profile.protected,
            "avatar_url": _profile_avatar_url(profile.avatar),
            "stats": {
                "followers": profile.followers,
                "following": profile.following,
                "statuses": profile.statuses,
                "likes": profile.likes,
                "media_count": profile.media_count,
            },
            "note": "账号资料已在上面的对话里发送过，请直接根据本 JSON 回答，不要编造数据。",
        }

        if self.profile_post_count > 0:
            statuses, error = await self._fetch_profile_statuses(
                profile.handle,
                count=max(5, min(100, self.profile_post_count * 2)),
            )
            posts: list[dict[str, Any]] = []
            for status in statuses:
                post = self._status_to_tweet(status, profile.handle)
                if not post.ok:
                    continue
                posts.append(
                    {
                        "url": post.url
                        or build_tweet_url(post.author_screen_name, post.tweet_id),
                        "text": truncate_text(post.text, 400),
                        "language": post.lang,
                        "created_timestamp": post.created_timestamp,
                    }
                )
                if len(posts) >= self.profile_post_count:
                    break
            payload["recent_posts"] = posts
            if error:
                payload["recent_posts_error"] = error
        return payload

    @filter.llm_tool(name="parse_x_tweet")
    async def parse_x_tweet(
        self,
        event: AstrMessageEvent,
        tweet_url: str = "",
    ) -> str:
        """解析 X（Twitter）推文的内容或 X 账号主页资料，返回结构化 JSON。
        当用户发来 x.com 或 twitter.com 的推文链接并要求总结、翻译、解释、提取或分析其内容时，
        调用本工具获取推文原文；当用户发来 X 个人主页链接（如 https://x.com/elonmusk）
        或询问某个 X 账号的资料、简介、粉丝数、最近发了什么时，同样调用本工具获取账号资料与最近推文。
        tweet_url 可以留空，此时会自动使用当前会话中最近解析过的一条推文。

        Args:
            tweet_url(string): 推文链接、个人主页链接、@账号名或推文数字 ID；留空表示使用本会话最近解析过的推文
        """
        try:
            self._apply_session_lang(event)
            tweet_id = normalize_tweet_ref(tweet_url) if tweet_url else None
            profile_handle = ""
            if not tweet_id and tweet_url:
                profile_handle = normalize_handle(tweet_url)

            # 主页链接 / 裸 @账号：返回账号资料与最近推文
            if not tweet_id and profile_handle:
                payload = await self._build_profile_payload(profile_handle)
                return json.dumps(payload, ensure_ascii=False)

            if not tweet_id:
                tweet_id = self._resolve_session_tweet(event.unified_msg_origin)

            if not tweet_id:
                return json.dumps(
                    {
                        "success": False,
                        "error": "未提供推文链接，且当前会话中没有最近解析过的推文。请让用户提供 x.com 或 twitter.com 的推文链接。",
                    },
                    ensure_ascii=False,
                )

            cached = await self._cache_get(tweet_id)
            data: Optional[TweetData] = cached if (cached and cached.ok) else None
            if data is None:
                data = await self._parse_for_send(tweet_id)
                if data.ok:
                    await self._cache_put(data)
                    self._record_session_tweet(event.unified_msg_origin, tweet_id)

            if not data.ok:
                return json.dumps(
                    {
                        "success": False,
                        "tweet_id": tweet_id,
                        "error": data.error or "推文解析失败",
                        "note": "请如实告知用户无法读取该推文，不要编造推文内容。",
                    },
                    ensure_ascii=False,
                )

            # 解析黑名单：命中作者或关键词时不把内容交给大模型
            block_reason = self._parse_block_reason(data)
            if block_reason:
                return json.dumps(
                    {
                        "success": False,
                        "blocked": True,
                        "tweet_id": tweet_id,
                        "reason": block_reason,
                        "error": f"该推文命中{block_reason}，已按插件配置跳过解析。",
                        "note": "请告知用户这条推文被插件的解析黑名单拦截了，不要编造内容。",
                    },
                    ensure_ascii=False,
                )

            session_id = event.unified_msg_origin
            media_sent = self._media_already_sent(
                session_id, tweet_id
            ) or self._images_already_sent(session_id, tweet_id)

            payload = {
                "success": True,
                "tweet_id": data.tweet_id,
                "url": data.url or build_tweet_url(data.author_screen_name, data.tweet_id),
                "author_name": data.author_name,
                "author_screen_name": data.author_screen_name,
                "text": truncate_text(data.text),
                "language": data.lang,
                "translation_text": data.translation_text,
                "translation_lang": data.translation_lang,
                "translation_provider": data.translation_provider,
                "created_at": (
                    datetime.fromtimestamp(
                        int(data.created_timestamp), tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC")
                    if data.created_timestamp
                    else ""
                ),
                "images_urls": [
                    apply_image_size(url, self.image_size) for url in data.images
                ],
                "image_size": self.image_size,
                "video_urls": data.videos,
                "media_urls": data.media_urls,
                "quote_text": data.quote_text,
                "quote_url": data.quote_url,
                "quote_translation_text": data.quote_translation_text,
                "parent_tweet_id": data.parent_tweet_id,
                "parent_kind": data.parent_kind,
                "reply_to_author": data.reply_to_author,
                "reply_to_url": data.reply_to_url,
                "stats": {
                    "likes": data.likes,
                    "retweets": data.retweets,
                    "replies": data.replies,
                    "views": data.views,
                },
                "source": data.source,
                "from_cache": cached is not None,
                "media_already_sent_to_group": media_sent,
                "note": (
                    "推文的正文、图片和视频已经在上面的对话里发送过了，"
                    "请直接根据 text 字段作答，不要再输出图片或视频链接。"
                    if media_sent
                    else "请根据 text 字段回答用户；若用户需要图片或视频，"
                    "可以给出 images_urls / video_urls 中的链接。"
                ),
            }
            if data.translation_text:
                payload["note"] += (
                    " 若用户要求翻译，可直接使用 translation_text 字段"
                    "（已翻译完成），无需自己再翻译一遍。"
                )
            if data.parent_tweet_id:
                kind = "引用的主贴" if data.parent_kind == "quote" else "被回复的主贴"
                payload["note"] += (
                    f" 本条推文是{kind}（parent_tweet_id 字段），"
                    "若用户想了解完整语境，可用本工具再次解析该 ID 获取主贴内容。"
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[{PLUGIN_NAME}] LLM 工具执行失败: {exc}", exc_info=True)
            return json.dumps(
                {"success": False, "error": f"工具内部错误：{exc}"}, ensure_ascii=False
            )

    # ------------------------------------------------------------------ #
    # 管理指令
    # ------------------------------------------------------------------ #

    @filter.command("xt")
    async def xt_command(self, event: AstrMessageEvent):
        """查看 X 推文解析插件的状态与缓存情况。用法：/xt [状态|清理|翻译 <语言>|搜索 <关键词>|主页 <账号>]"""
        self._apply_session_lang(event)
        raw = event.message_str or ""
        parts = raw.split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        # 去掉可能残留的唤醒前缀 / 指令名（不同平台、不同 AstrBot 版本差异较大）
        for prefix in ("/xt", "／xt", "xt"):
            if argument.lower().startswith(prefix):
                argument = argument[len(prefix) :].strip()
                break
        keyword = argument.split(maxsplit=1)[0].lower() if argument else ""

        if keyword in ("清理", "clear", "clean"):
            if not self._is_admin_event(event):
                yield event.plain_result(
                    "⛔ 只有管理员可以清理缓存（/xt 清理）。"
                )
                return
            media_count, json_count, total_size = await self._clear_all_caches()
            total = media_count + json_count
            yield event.plain_result(
                f"🧹 已清理缓存：{total} 个文件"
                f"（媒体 {media_count} / 解析 {json_count}），"
                f"释放 {total_size / 1024 / 1024:.2f} MB"
            )
            return

        if keyword in ("翻译", "translate"):
            # 只切换「当前会话」的目标语言（不写配置、不影响其他群，重启后恢复）
            session_id = getattr(event, "unified_msg_origin", "") or ""
            raw_lang = argument[len(keyword) :].strip()
            if not raw_lang:
                override = self.session_translate_lang(session_id)
                yield event.plain_result(
                    f"当前翻译：{self.translate_mode} → {self.active_translate_lang}"
                    f"（本会话{'自定义 ' + override if override else '跟随配置'}；"
                    f"配置默认 {self.translate_target_lang}）"
                )
                return
            if raw_lang in ("默认", "重置", "清除", "reset", "default", "off"):
                self._session_translate_lang.pop(session_id, None)
                _ACTIVE_TRANSLATE_LANG.set("")
                yield event.plain_result(
                    f"🌐 已恢复使用配置里的目标语言 {self.translate_target_lang}（仅本会话）"
                )
                return
            lang = normalize_translate_lang(raw_lang)
            self._session_translate_lang[session_id] = lang
            _ACTIVE_TRANSLATE_LANG.set(lang)
            yield event.plain_result(
                f"🌐 本会话目标语言已切换为 {lang}（只影响当前会话；"
                "发「/xt 翻译 默认」可恢复，永久修改请到插件配置面板调整 translate_target_lang）"
            )
            return

        if keyword in ("搜索", "search", "搜"):
            async for result in self._handle_search_command(event, argument[len(keyword) :].strip()):
                yield result
            return

        if keyword in ("帮助", "help", "?"):
            yield self._search_help_result(event)
            return

        if keyword in ("主页", "profile", "资料"):
            async for result in self._handle_profile_command(
                event, argument[len(keyword) :].strip()
            ):
                yield result
            return

        file_count, size = await asyncio.to_thread(self.downloader.cache_stats)
        if self.cache_cleanup_interval > 0:
            cleanup_text = f"每 {self.cache_cleanup_interval} 小时"
        else:
            cleanup_text = "未开启（仅启动时清理一次）"
        lines = [
            "🐦 X/Twitter 推文解析插件",
            f"状态：{'✅ 已启用' if self.enabled else '⛔ 已关闭'}",
            f"代理：{self.proxy or '跟随系统环境变量'}",
            f"冷却：同群同链接 {self.cooldown_seconds} 秒",
            f"上限：图片 {self.max_image_size}MB / 视频 {self.max_video_size}MB",
            f"图片尺寸：{image_size_display(self.image_size)}",
            f"正文展示：{TEXT_CONTENT_MODE_LABELS.get(self.text_content_mode, self.text_content_mode)}",
            f"推文交付：{TWEET_DELIVERY_CHOICES.get(self.tweet_delivery, self.tweet_delivery)}",
            f"主贴分开：{'✅ 已开启' if self.thread_split else '⛔ 已关闭'}",
            f"楼中楼：{self._thread_state_text() or '⛔ 已关闭'}",
            f"主页解析：{'✅ 已开启' if self.profile_enabled else '⛔ 已关闭'}"
            f"（{PROFILE_DELIVERY_CHOICES.get(self.profile_delivery, self.profile_delivery)}，"
            f"附带 {self.profile_post_count} 条最近推文）",
            f"解析黑名单：作者 {len(self.blocked_authors)} 个 / "
            f"关键词 {len(self.blocked_keywords)} 个",
            f"贴表情：{'✅ 已开启' if self.emoji_reply else '⛔ 已关闭'}"
            f"（emoji_id={self.emoji_id}）",
            f"翻译：{self.translate_mode} → {self.active_translate_lang}",
            f"翻译模型：{self.translate_provider_id or '未指定（自动用第一个可用模型）'}"
            f"（成功 {self.translator.success_count} / 失败 {self.translator.failure_count}）",
            f"搜索：{'✅ 已开启' if self.search_enabled else '⛔ 已关闭'}"
            f"（{self._search_account_text()} / 每次最多 {self.search_count} 条）",
            f"搜索交付：{SEARCH_DELIVERY_CHOICES.get(self.search_delivery, self.search_delivery)}"
            f"（翻译 {'开' if self.search_translate else '关'}，"
            f"并发上限 {self.search_translate_concurrency}）",
            f"解析源顺序：{self.parser.source_order_text()}",
            f"镜像实例：{len(self.mirror_fx_instances)} 个"
            f"（{'已启用' if self.enable_mirror_sources else '已关闭'}）",
            f"定时清理：{cleanup_text}",
            f"缓存：{file_count} 个文件，{size / 1024 / 1024:.2f} MB",
            f"内存缓存：{len(self._data_cache)} 条推文",
            f"解析次数：{self.run_count}",
            f"缓存目录：{self.cache_dir}",
        ]
        if self.translator.last_error:
            lines.append(f"翻译最近错误：{self.translator.last_error}")
        if self.last_error:
            lines.append(f"最近错误：{self.last_error}")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _parse_search_query(raw: str) -> tuple[list[str], list[str], list[str], int]:
        """解析 `/xt 搜索` 的查询语法，返回 (账号列表, 关键词, 排除词, 点赞门槛)。

        语法（以空格分隔，关键词大小写无关）：
            from:<账号>    限定检索账号，可写多个；带此语法跳过 typeahead 自动匹配
            -<词>          排除词，正文命中任意一个即丢弃，可写多个
            min_faves:<N>  点赞数门槛，低于 N 的推文丢弃
            其余词         关键词，任一命中即返回
        兼容旧写法 `@账号`（等价 from:<账号>）。
        """
        accounts: list[str] = []
        keywords: list[str] = []
        excludes: list[str] = []
        min_faves = 0
        for token in (raw or "").split():
            if not token:
                continue
            lowered = token.lower()
            if lowered.startswith("from:"):
                accounts.extend(XTweetPlugin._parse_account_list(token[5:]))
            elif lowered.startswith(("min_faves:", "minlikes:")):
                min_faves = _to_int(token.split(":", 1)[1]) or 0
            elif token.startswith("-") and len(token) > 1:
                excludes.append(token[1:].lower())
            elif token.startswith("@"):
                accounts.extend(XTweetPlugin._parse_account_list(token))
            else:
                keywords.append(token.lower())
        return accounts, keywords, excludes, min_faves

    def _search_help_result(self, event: AstrMessageEvent, hint: str = ""):
        """搜索帮助消息：一行提示 + 静态帮助图片（图片缺失时退化为纯文字）。"""
        parts: list[Any] = [Comp.Plain(hint or "🔍 X 搜索语法提示：\n")]
        if os.path.isfile(self.help_image_path):
            parts.append(Comp.Image.fromFileSystem(self.help_image_path))
        else:
            parts.append(
                Comp.Plain(
                    "用法：/xt 搜索 [from:账号] <关键词> [-排除词] [min_faves:N]\n"
                    "示例：/xt 搜索 from:elonmusk 星舰 -火星 min_faves:5000\n"
                    "多个关键词任一命中；未指定 from: 时按关键词自动匹配相关账号。"
                )
            )
        return event.chain_result(parts)

    async def _handle_search_command(self, event: AstrMessageEvent, argument: str):
        """处理 `/xt 搜索 [from:账号] <关键词> [-排除词] [min_faves:N]`。

        不带任何参数时发送搜索语法帮助图片；账号来源优先级：
        from:/@账号 > 配置的「搜索账号列表」> 按关键词自动匹配（auto 开启时）。
        """
        if not self.search_enabled:
            yield event.plain_result("⛔ X 搜索功能已在插件配置里关闭（search_enabled）。")
            return

        accounts, keywords, excludes, min_faves = self._parse_search_query(argument)

        # 完全没给查询条件：多半是不了解语法，直接发帮助图片
        if not accounts and not keywords and not min_faves:
            yield self._search_help_result(event)
            return

        # 账号来源：from:/@账号 前缀 > 配置的「搜索账号列表」> 按关键词自动匹配（auto 开启时）
        if not accounts and not self.search_accounts and not keywords:
            yield self._search_help_result(
                event,
                hint="⚠️ 还没有配置要检索的 X 账号。请在插件配置面板的「搜索账号列表」里"
                "填入账号（如 elonmusk, OpenAI），或者用 from: 语法指定账号：\n",
            )
            return

        keyword_text = " ".join(keywords)
        try:
            results, error = await self.search_x_posts(
                accounts=accounts or None,
                keywords=keywords or None,
                translate_lang=self.active_translate_lang,
                exclude_keywords=excludes,
                min_likes=min_faves,
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[{PLUGIN_NAME}] /xt 搜索 失败: {exc}", exc_info=True)
            yield event.plain_result("⚠️ 搜索出错了，请稍后再试。")
            return

        # 自动匹配到的账号只有在 search_x_posts 跑完才知道，来源展示放到调用之后
        used_accounts = accounts or self._last_search_targets
        used_text = "、".join("@" + item for item in used_accounts)

        if not results:
            hint = "没有检索到符合条件的推文。"
            if error:
                hint = f"检索失败（{truncate_text(error, 200)}）。"
            elif keyword_text:
                hint = f"没有在这些账号里找到含有「{keyword_text}」的近期推文。"
            yield event.plain_result(hint + f"\n检索范围：{used_text}")
            return

        # 补译文（默认关闭，需在面板开启 search_translate）
        if self.search_translate and self.active_translate_lang:
            await self._translate_search_results(results, self.active_translate_lang)

        # 交付方式：合并转发（默认）→ 图片 → 纯文本，逐级回退
        delivery = self.search_delivery
        if delivery == "forward" and not self._supports_forward(event):
            logger.debug(f"[{PLUGIN_NAME}] 当前平台不支持合并转发，搜索结果改用纯文本")
            delivery = "text"

        if delivery == "forward":
            yield event.chain_result(
                await self._build_search_forward(event, results, keyword_text)
            )
            return

        if delivery == "image":
            image_path = await self._render_search_image(
                results, keyword_text, used_accounts
            )
            if image_path:
                yield event.chain_result([Comp.Image.fromFileSystem(image_path)])
                return
            logger.warning(f"[{PLUGIN_NAME}] 搜索结果图片渲染不可用，回退纯文本发送")
            delivery = "text"

        text = self.build_search_text(results, keyword=keyword_text)
        note = f"\n\n（来源：{used_text}）"
        yield event.plain_result(escape_zero_width(truncate_text(text, 3500) + note))

    async def _handle_profile_command(self, event: AstrMessageEvent, argument: str):
        """处理 `/xt 主页 <账号名或链接>`。"""
        if not self.profile_enabled:
            yield event.plain_result(
                "⛔ X 个人主页解析已在插件配置里关闭（profile_enabled）。"
            )
            return

        handle = normalize_handle(argument)
        if not handle:
            yield event.plain_result(
                "用法：/xt 主页 <账号名>，例如 /xt 主页 elonmusk 或 "
                "/xt 主页 https://x.com/elonmusk"
            )
            return

        try:
            messages = await self._collect_profile_messages(event, handle)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error(f"[{PLUGIN_NAME}] /xt 主页 失败: {exc}", exc_info=True)
            yield event.plain_result("⚠️ 主页解析出错了，请稍后再试。")
            return

        if not messages:
            yield event.plain_result("⚠️ 没能解析这个主页，请稍后再试。")
            return

        for parts in messages[:-1]:
            await self._send_now(event, parts)
        yield event.chain_result(messages[-1])


# --------------------------------------------------------------------------- #
# 兼容入口：部分旧版本通过 register 装饰器或模块级类发现插件
# --------------------------------------------------------------------------- #

# AstrBot 通过扫描模块中的 Star 子类来加载插件，因此无需额外注册代码。
# 这里仅确保 logger 在模块级别被引用，避免静态检查误报未使用。
# MJ
_ = logging.getLogger(PLUGIN_NAME)
