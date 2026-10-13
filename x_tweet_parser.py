# -*- coding: utf-8 -*-
"""
astrbot_plugin_x_tweet —— 解析层

包含 XTweetParser（FxTwitter → 镜像轮询 → Syndication CDN → oEmbed → t.co 展开的
多源降级解析）与 Translator（大模型翻译兜底）。
依赖 x_tweet_core 中的常量与数据结构，不依赖插件主体，可单独实例化测试。

作者：Leisurelyun
版本：v1.13.1
"""

from __future__ import annotations

import os
import sys

if __package__:  # AstrBot 正常加载：data.plugins.astrbot_plugin_x_tweet.main 的包内相对导入
    from .x_tweet_core import *  # noqa: F401,F403
else:  # 以文件路径直接加载（测试脚本 / 手工调试）时退化为平铺导入
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from x_tweet_core import *  # noqa: F401,F403

# --------------------------------------------------------------------------- #
# 四、解析服务 XTweetParser
# --------------------------------------------------------------------------- #


class XTweetParser:
    """
    推文解析服务。

    按顺序尝试多个数据源，任意一个成功即返回：
        源1：FxTwitter v2  —— 含正文、作者、图片、视频直链，并支持 ?lang= 免费翻译
        源1'：FxTwitter v1 —— v2 不可用时的兜底（无翻译）
        源2：镜像实例轮询  —— FxTwitter 同构 API 的其它部署（enable_mirror_sources
              控制开关，列表为空则不轮询，见 mirror_fx_instances）
        源3：Syndication CDN —— X 官方嵌入组件的取数端点，无需登录与密钥；
              有结构化正文 / 作者 / 媒体 / 父子关系，但超长推文会被截断，
              且无转发数 / 浏览量、无自带翻译
        源4：Twitter oEmbed —— 纯文本兜底（有正文与作者，无媒体、无翻译）

    翻译说明：translate_lang 非空时会在 v2 请求里带上 ?lang=，接口返回的
    translation 字段即为译文（由 FxTwitter 侧调用 Grok 完成），无需任何密钥。
    备用源（2/3/4）都没有自带译文，需要翻译时会交给大模型兜底。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout: float = 20.0,
        user_agent: str = DEFAULT_USER_AGENT,
        translate_retry: Optional[int] = None,
        translate_llm_min_chars: Optional[int] = None,
        enable_mirror_sources: Optional[bool] = None,
        mirror_fx_instances: Optional[list[str]] = None,
        source_order: Optional[list[str]] = None,
    ) -> None:
        self.client = client
        self.timeout = timeout
        self.user_agent = user_agent
        # FxTwitter 自带译文缺失时的重试次数（插件会把面板配置传进来；
        # 单独使用本类时按默认值走）
        self.translate_retry: int = (
            DEFAULT_TRANSLATE_RETRY if translate_retry is None else int(translate_retry)
        )
        # 少于这么多有效字符就不调用大模型翻译（0 = 不限制）
        self.translate_llm_min_chars: int = (
            DEFAULT_TRANSLATE_LLM_MIN_CHARS
            if translate_llm_min_chars is None
            else int(translate_llm_min_chars)
        )
        # 记录 v2 是否可用，避免每次都白试一次（None=未知，False=不可用）
        self._v2_available: Optional[bool] = None
        # 镜像实例轮询：FxTwitter 主站单点故障时的第二级保障。默认不轮询任何
        # 实例（实测常见公共镜像均不可达），用户可在面板里按需填写。
        self.enable_mirror_sources: bool = (
            DEFAULT_ENABLE_MIRROR_SOURCES if enable_mirror_sources is None
            else bool(enable_mirror_sources)
        )
        instances: list[str] = []
        for raw in mirror_fx_instances or []:
            item = str(raw).strip().rstrip("/")
            if not item or not item.lower().startswith(("http://", "https://")):
                continue
            if item not in instances:
                instances.append(item)
        self.mirror_fx_instances: list[str] = instances
        # 解析源优先级：只保留已知的源键，未列出的按默认顺序追加到末尾（不丢源）
        order: list[str] = []
        for raw in source_order or []:
            key = str(raw).strip().lower()
            if key in PARSE_SOURCE_CHOICES and key not in order:
                order.append(key)
        for key in DEFAULT_PARSE_SOURCE_ORDER:
            if key not in order:
                order.append(key)
        self.source_order: list[str] = order

    def _source_fetchers(self) -> list[tuple[str, Any]]:
        """按配置的优先级返回 [(源键, 取数方法)]。"""
        mapping: dict[str, Any] = {
            "fxtwitter": self.fetch_via_fxtwitter,
            "mirror": self._fetch_mirror_fx,
            "syndication": self.fetch_via_syndication,
            "oembed": self.fetch_via_oembed,
        }
        return [(key, mapping[key]) for key in self.source_order if key in mapping]

    def source_order_text(self) -> str:
        """把解析源顺序拼成可读文本（供 /xt 状态 与启动日志使用）。"""
        return " → ".join(PARSE_SOURCE_CHOICES.get(key, key) for key in self.source_order)

    # ---------------------------- 内部工具 ---------------------------- #

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    async def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        try:
            await self.client.aclose()
        except Exception as exc:  # pragma: no cover
            logger.debug(f"[{PLUGIN_NAME}] 关闭 HTTP 客户端时出错: {exc}")

    # ---------------------------- 主入口 ---------------------------- #

    async def parse(self, tweet_ref: str) -> TweetData:
        """解析推文。tweet_ref 可以是推文链接或纯数字 ID。

        按 parse_source_order 配置的顺序尝试各数据源（默认 FxTwitter(v2/v1) →
        镜像实例 → Syndication CDN → oEmbed），第一个成功即返回。
        """
        tweet_id = normalize_tweet_ref(tweet_ref)
        if not tweet_id:
            return TweetData(tweet_id="", error="未能从输入中识别出推文链接或推文 ID")
        return await self._fetch_fallback_chain(tweet_id)

    async def _fetch_fallback_chain(
        self, tweet_id: str, translate_lang: str = ""
    ) -> TweetData:
        """按配置的源顺序依次尝试（默认：FxTwitter → 镜像 → Syndication → oEmbed）。

        首个成功即返回；全部失败时返回第一个源的错误（通常信息最准确），
        并在其后附上其余源的失败数量。translate_lang 只作用于 FxTwitter（自带翻译）。
        """
        last = TweetData(tweet_id=tweet_id, error="所有解析源均未取到推文")
        errors: list[str] = []
        for key, fetcher in self._source_fetchers():
            if key == "fxtwitter" and translate_lang:
                candidate = await self.fetch_via_fxtwitter(
                    tweet_id, translate_lang=translate_lang
                )
            else:
                candidate = await fetcher(tweet_id)
            if candidate.ok:
                return candidate
            last = candidate
            label = PARSE_SOURCE_CHOICES.get(key, key)
            if candidate.error:
                errors.append(f"{label}：{candidate.error}")
            logger.warning(
                f"[{PLUGIN_NAME}] 解析源「{label}」未取到推文 {tweet_id}：{candidate.error}"
            )
        if errors:
            # 措辞刻意避开「失败」二字：主链会按关键词（超时/网络/失败/不存在）决定群内提示，
            # 「失败」会把「推文不存在」误报成「网络异常，请检查代理」
            last.error = (
                errors[0]
                if len(errors) == 1
                else f"{errors[0]}（另有 {len(errors) - 1} 个源同样未取到）"
            )
        return last

    async def parse_with_translation(
        self,
        tweet_ref: str,
        translate_lang: str = "",
        translator: Optional["Translator"] = None,
        allow_llm: bool = True,
    ) -> TweetData:
        """解析推文，并在需要时补上译文。

        Args:
            translate_lang: 目标语言（空字符串表示不翻译）。
            translator: 负责调用大模型翻译的对象；为 None 时不尝试大模型。
            allow_llm: 是否允许在 FxTwitter 翻译不可用时回退到大模型。

        翻译策略：
            1. 解析时直接带 ?lang= 请求 FxTwitter v2，接口自带译文（免费、无密钥）；
            2. 目标语言不在白名单或 v2 不可用时，退化为 v1 解析 + 大模型翻译；
            3. 任何一步失败都只记日志，不影响推文本体。
        """
        target = normalize_translate_lang(translate_lang) if translate_lang else ""
        if not target or target not in TRANSLATE_LANG_CHOICES:
            if target:
                logger.warning(
                    f"[{PLUGIN_NAME}] 目标语言 {target} 不在 FxTwitter 支持列表内，"
                    "将只尝试大模型翻译"
                )
            data = await self.parse(tweet_ref)
            if data.ok and target:
                await self._translate_with_llm(data, target, translator)
            return data

        tweet_id = normalize_tweet_ref(tweet_ref) or tweet_ref
        data = await self._fetch_fallback_chain(tweet_id, translate_lang=target)
        if not data.ok:
            return data

        # FxTwitter 未返回译文（或本来就不需要翻译）时，才考虑重试与大模型
        needs = (
            not data.translated
            and data.ok
            and data.source != "oembed"
            and data.needs_translation(target)
        )
        if needs:
            # 自带译文是间歇性的：先重试几次，能白拿就白拿
            data = await self._retry_free_translation(tweet_ref, target, data)
            needs = (
                not data.translated
                and data.ok
                and data.source != "oembed"
                and data.needs_translation(target)
            )
        if needs and allow_llm:
            logger.info(
                f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 没拿到 FxTwitter 自带译文，"
                f"改用大模型翻译"
            )
            await self._translate_with_llm(data, target, translator)
        elif needs:
            logger.info(
                f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 没拿到 FxTwitter 自带译文，"
                f"且当前配置不允许大模型翻译，只发原文"
            )
        return data

    async def _retry_free_translation(
        self, tweet_ref: str, target: str, data: TweetData
    ) -> TweetData:
        """FxTwitter 自带译文偶发缺失时重试几次，再决定要不要动用大模型。

        只在「接口认得原文语言、且与目标语言不同」时重试 —— 纯表情 / 超短文本的
        language 为空，接口本来就不会给译文，重试纯属浪费请求。
        """
        retries = self.translate_retry
        if retries <= 0 or not data.lang or data.translated:
            return data

        for attempt in range(1, retries + 1):
            await asyncio.sleep(TRANSLATE_RETRY_DELAY_SECONDS * attempt)
            retry = await self.fetch_via_fxtwitter(tweet_ref, translate_lang=target)
            if not retry.ok or retry.source == "oembed":
                continue
            data = retry
            if retry.translated:
                logger.info(
                    f"[{PLUGIN_NAME}] 推文 {retry.tweet_id} 第 {attempt} 次重试拿到了 "
                    f"FxTwitter 自带译文（provider={retry.translation_provider}）"
                )
                break
        return data

    async def _translate_with_llm(
        self,
        data: TweetData,
        target: str,
        translator: Optional["Translator"],
    ) -> None:
        """用大模型补齐译文；失败只记日志，不改变 data.ok 状态。"""
        if translator is None or not data.text:
            return
        # 正文已经是目标语言时（接口 lang 判定，或 lang 缺失时按字符集兜底）不必翻译
        if already_in_target_lang(data, target):
            logger.info(
                f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 正文已经是目标语言（{target}），"
                f"不调用大模型翻译"
            )
            return
        # 去掉 @提及 / 链接 / 表情后没剩几个字的短文本不必劳烦大模型
        min_chars = self.translate_llm_min_chars
        if min_chars > 0:
            effective = meaningful_text_length(data.text)
            if effective < min_chars:
                logger.info(
                    f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 正文有效字符只有 "
                    f"{effective} 个（少于 {min_chars}），不调用大模型翻译"
                )
                return
        try:
            text = await translator.translate(data.text, target, data.lang)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 大模型翻译失败：{exc}")
            return
        if text:
            if text.strip() == (data.text or "").strip():
                # 模型把原文原样返回：这不算译文，存进去反而会多出一行重复正文
                logger.info(
                    f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 大模型译文与原文相同，视为无需翻译"
                )
                return
            data.translation_text = text
            data.translation_lang = target
            data.translation_provider = translator.provider_id or "llm"
            data.translation_source_lang = data.lang

    # ---------------------------- 源1：FxTwitter ---------------------------- #

    async def fetch_via_fxtwitter(
        self, tweet_id: str, translate_lang: str = ""
    ) -> TweetData:
        """通过 FxTwitter 公开 API 获取推文详情。

        translate_lang 非空且 v2 可用时，会带上 ?lang=<lang> 以获得接口自带译文。
        """
        if self._v2_available is not False:
            data = await self._fetch_fxtwitter_v2(tweet_id, translate_lang)
            if data.ok:
                self._v2_available = True
                return data
            # v2 明确不可用（404/5xx）时记住，后续直接走 v1，避免每次都白试
            if data.error and ("HTTP 404" in data.error or "HTTP 5" in data.error):
                self._v2_available = False
                logger.warning(
                    f"[{PLUGIN_NAME}] FxTwitter v2 接口不可用（{data.error}），"
                    "已回退到 v1（v1 不支持自带翻译）"
                )
                return await self._fetch_fxtwitter_v1(tweet_id)
            return data
        return await self._fetch_fxtwitter_v1(tweet_id)

    async def _fetch_fxtwitter_v2(self, tweet_id: str, translate_lang: str = "") -> TweetData:
        """调用 FxTwitter v2 接口。"""
        url = TWEET_API_V2.format(tweet_id=tweet_id)
        data, tweet = await self._get_tweet_payload(url, tweet_id, translate_lang)
        if not data.ok:
            return data
        self._apply_translation(data, tweet)
        return data

    async def _fetch_fxtwitter_v1(self, tweet_id: str) -> TweetData:
        """调用 FxTwitter v1 接口（兜底，无翻译）。"""
        url = TWEET_API_V1.format(tweet_id=tweet_id)
        data, _tweet = await self._get_tweet_payload(url, tweet_id, "")
        return data

    # ---------------------------- 源2：镜像实例轮询 ---------------------------- #

    async def _fetch_mirror_fx(self, tweet_id: str) -> TweetData:
        """轮询镜像实例，任一成功即返回。

        镜像列表为空（默认）时直接放弃，不做任何请求。每个实例按
        v2 → v1 两种路径尝试；响应同时兼容两种格式：
        ① FxTwitter 同构（顶层 tweet / status 键）；
        ② vx 系（vxtwitter 等，顶层直接是 user_screen_name / text / date_epoch 等）。
        请求统一带 Referer: https://x.com/（vx 系镜像不带会返回 403）。
        """
        empty = TweetData(tweet_id=tweet_id, error="未配置任何镜像实例")
        instances = list(self.mirror_fx_instances)
        if not self.enable_mirror_sources or not instances:
            return empty

        errors: list[str] = []
        for instance in instances:
            for pattern in ("/2/status/{tweet_id}", "/i/status/{tweet_id}"):
                url = instance + pattern.format(tweet_id=tweet_id)
                data = await self._fetch_mirror_once(url, tweet_id)
                if data.ok:
                    data.source = "mirror"
                    logger.info(
                        f"[{PLUGIN_NAME}] 推文 {tweet_id} 通过镜像 {instance} 解析成功"
                    )
                    return data
                errors.append(f"{instance}: {data.error}")
        return TweetData(
            tweet_id=tweet_id, error="；".join(errors[:4]) or "所有镜像实例均不可用"
        )

    async def _fetch_mirror_once(self, url: str, tweet_id: str) -> TweetData:
        """请求单个镜像地址，自动识别 FxTwitter 同构与 vx 系两种响应格式。"""
        data = TweetData(tweet_id=tweet_id, url=build_tweet_url(None, tweet_id))
        headers = self.headers()
        headers["Referer"] = VX_MIRROR_REFERER
        try:
            response = await self.client.get(url, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException:
            data.error = "镜像请求超时"
            return data
        except httpx.HTTPError as exc:
            data.error = f"镜像请求失败：{exc}"
            return data
        except Exception as exc:  # pragma: no cover
            data.error = f"镜像请求未知错误：{exc}"
            return data

        if response.status_code == 404:
            data.error = "推文不存在或已被删除"
            return data
        if response.status_code >= 400:
            data.error = f"镜像返回 HTTP {response.status_code}"
            return data
        try:
            payload = response.json()
        except Exception:
            data.error = "镜像返回了非 JSON 数据"
            return data
        if not isinstance(payload, dict):
            data.error = "镜像返回格式无法识别"
            return data

        # ① FxTwitter 同构：顶层 tweet / status
        tweet = payload.get("tweet") or payload.get("status")
        if isinstance(tweet, dict) and tweet:
            self.fill_from_tweet(data, tweet)
            return data
        # ② vx 系（vxtwitter 等）：顶层直接是推文字段
        if any(
            key in payload
            for key in ("user_screen_name", "media_extended", "date_epoch", "tweetURL")
        ):
            return self._parse_vx_payload(payload, data)

        message = payload.get("message") or "接口未返回推文数据"
        data.error = f"解析失败：{message}"
        return data

    def _parse_vx_payload(self, payload: dict[str, Any], data: TweetData) -> TweetData:
        """把 vx 系镜像（vxtwitter 等）的响应映射为 TweetData。

        与 FxTwitter 同构格式的差异：顶层直接给字段（user_screen_name / text /
        date_epoch / media_extended / qrt / replyingTo），引用推文是完整对象，
        部分实例还带 translation 字段。
        """
        data.text = html.unescape(payload.get("text") or "")
        data.author_name = _clean_str(payload.get("user_name"))
        data.author_screen_name = _clean_str(payload.get("user_screen_name"))
        data.author_avatar = _clean_str(payload.get("user_profile_image_url"))
        data.author_verified = _truthy(payload.get("user_verified")) or _truthy(
            payload.get("verified")
        )
        data.created_timestamp = _to_int(payload.get("date_epoch"))
        data.lang = _clean_str(payload.get("lang"))
        data.likes = _to_int(payload.get("likes"))
        data.retweets = _to_int(payload.get("retweets"))
        data.replies = _to_int(payload.get("replies"))
        data.views = None
        data.is_note_tweet = False

        for item in payload.get("media_extended") or []:
            if not isinstance(item, dict):
                continue
            kind = (item.get("type") or "").lower()
            url = item.get("url") or ""
            if kind in ("image", "photo"):
                if url and url not in data.images:
                    data.images.append(url)
            elif kind in ("video", "gif"):
                if url and url not in data.videos:
                    data.videos.append(url)
                thumb = item.get("thumbnail_url") or ""
                if thumb and thumb not in data.video_thumbnails:
                    data.video_thumbnails.append(thumb)

        # 引用推文：vx 系的 qrt 是完整对象，递归解析（含其媒体）
        qrt = payload.get("qrt")
        if isinstance(qrt, dict) and qrt:
            qrt_id = str(qrt.get("tweetID") or "").strip()
            quoted = self._parse_vx_payload(qrt, TweetData(tweet_id=qrt_id))
            if quoted.ok:
                data.quote_id = quoted.tweet_id
                data.quote_url = quoted.url
                prefix = (
                    f"@{quoted.author_screen_name}: "
                    if quoted.author_screen_name
                    else ""
                )
                data.quote_text = truncate_text(prefix + (quoted.text or ""), 500)
                data.quote_images = quoted.images
                data.quote_videos = quoted.videos
                data.quote_video_thumbnails = quoted.video_thumbnails
        if not data.quote_id:
            qrt_url = payload.get("qrtURL") or ""
            if qrt_url:
                data.quote_url = qrt_url
                data.quote_id = normalize_tweet_ref(qrt_url) or ""

        # 回复关系：只取 ID / 作者，正文交给主链按楼中楼逻辑二次解析
        reply_id = str(payload.get("replyingToID") or "").strip()
        if not reply_id:
            reply_url = payload.get("replyingTo") or ""
            if isinstance(reply_url, str) and reply_url.startswith("http"):
                reply_id = normalize_tweet_ref(reply_url) or ""
        if reply_id:
            data.reply_to_id = reply_id
            reply_author = ""
            reply_url = payload.get("replyingTo")
            if isinstance(reply_url, str):
                match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/", reply_url)
                if match:
                    reply_author = match.group(1)
            data.reply_to_author = reply_author
            data.reply_to_url = build_tweet_url(reply_author or None, reply_id)

        # 部分实例自带译文（结构与 FxTwitter 略有出入，做宽松兼容）
        translation = payload.get("translation")
        if isinstance(translation, dict):
            translated = (translation.get("text") or "").strip()
            if translated and translated != (data.text or "").strip():
                data.translation_text = translated
                data.translation_lang = translation.get("target_lang") or ""
                data.translation_provider = "mirror"
        elif isinstance(translation, str) and translation.strip():
            if translation.strip() != (data.text or "").strip():
                data.translation_text = translation.strip()
                data.translation_provider = "mirror"

        data.text = self._clean_text(data.text)
        if not data.text and not data.has_any_media:
            data.error = "推文内容为空（可能是纯转推或已被删除）"
            return data
        data.url = build_tweet_url(data.author_screen_name or None, data.tweet_id)
        data.source = "mirror"
        data.fetched_at = time.time()
        return data

    # ---------------------------- 源3：Syndication CDN ---------------------------- #

    async def fetch_via_syndication(self, tweet_id: str) -> TweetData:
        """通过 X 官方 Syndication CDN 获取推文（FxTwitter 故障时的异构备用源）。

        与 FxTwitter 的差异：有结构化正文 / 作者 / 媒体 / 父子关系，但超长推文
        会被截断（只返回可见部分 + t.co 短链），转发数与浏览量字段为 null，
        也没有自带翻译。
        """
        data = TweetData(tweet_id=tweet_id, url=build_tweet_url(None, tweet_id))
        token = generate_syndication_token(tweet_id)
        params = {"id": tweet_id, "token": token or "x", "lang": "en"}
        try:
            response = await self.client.get(
                SYNDICATION_API,
                params=params,
                headers=self._syndication_headers(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            data.error = "Syndication 接口请求超时"
            return data
        except httpx.HTTPError as exc:
            data.error = f"Syndication 接口请求失败：{exc}"
            return data
        except Exception as exc:  # pragma: no cover
            data.error = f"Syndication 接口未知错误：{exc}"
            return data
        if response.status_code >= 400:
            data.error = f"Syndication 接口返回 HTTP {response.status_code}"
            return data
        try:
            payload = response.json()
        except Exception:
            data.error = "Syndication 接口返回了非 JSON 数据"
            return data
        return self._parse_syndication_response(payload, data)

    def _syndication_headers(self) -> dict[str, str]:
        """Syndication CDN 要求真实浏览器 UA 与 Referer，否则可能 403。"""
        headers = self.headers()
        headers["Referer"] = "https://platform.twitter.com/"
        return headers

    def _parse_syndication_response(
        self, payload: Any, data: TweetData
    ) -> TweetData:
        """把 Syndication CDN 的 JSON 映射为 TweetData（quoted_tweet 递归解析）。"""
        if not isinstance(payload, dict):
            data.error = "Syndication 接口未返回推文数据"
            return data
        typename = str(payload.get("__typename") or "")
        if typename and typename != "Tweet":
            data.error = f"推文不可用（{typename}）"
            return data

        user = payload.get("user") or {}
        data.text = self._clean_syndication_text(payload)
        data.author_name = _clean_str(user.get("name"))
        data.author_screen_name = _clean_str(user.get("screen_name"))
        data.author_avatar = _clean_str(user.get("profile_image_url_https"))
        data.author_verified = _truthy(user.get("is_blue_verified")) or _truthy(
            user.get("verified")
        )
        data.created_timestamp = parse_twitter_time(payload.get("created_at"))
        data.lang = _clean_str(payload.get("lang"))
        data.likes = _to_int(payload.get("favorite_count"))
        data.retweets = _to_int(payload.get("retweet_count"))  # 实测恒为 null
        data.replies = _to_int(payload.get("conversation_count")) or _to_int(
            payload.get("reply_count")
        )
        data.views = None
        data.is_note_tweet = False
        data.images, data.videos, data.video_thumbnails = (
            self._collect_syndication_media(payload)
        )

        # 引用的推文：Syndication 返回完整对象，递归解析（含其媒体与正文）
        quoted = payload.get("quoted_tweet")
        if isinstance(quoted, dict) and quoted:
            quoted_data = self._parse_syndication_response(
                quoted, TweetData(tweet_id=str(quoted.get("id_str") or ""))
            )
            if quoted_data.ok:
                data.quote_id = quoted_data.tweet_id
                data.quote_url = quoted_data.url
                prefix = (
                    f"@{quoted_data.author_screen_name}: "
                    if quoted_data.author_screen_name
                    else ""
                )
                data.quote_text = truncate_text(
                    prefix + (quoted_data.text or ""), 500
                )
                data.quote_images = quoted_data.images
                data.quote_videos = quoted_data.videos
                data.quote_video_thumbnails = quoted_data.video_thumbnails
            else:
                # 引用对象不完整（如已删除）时至少留下可再解析的 ID 与链接
                data.quote_id = str(quoted.get("id_str") or "")
                data.quote_url = build_tweet_url(None, data.quote_id) if data.quote_id else ""

        # 被回复的推文：只取 ID / 作者 / 链接，正文交给主链按楼中楼逻辑二次解析
        reply_to_id = str(payload.get("in_reply_to_status_id_str") or "")
        parent = payload.get("parent")
        if not reply_to_id and isinstance(parent, dict):
            reply_to_id = str(parent.get("id_str") or "")
        data.reply_to_id = reply_to_id
        data.reply_to_author = _clean_str(payload.get("in_reply_to_screen_name"))
        if data.reply_to_id:
            data.reply_to_url = build_tweet_url(
                data.reply_to_author or None, data.reply_to_id
            )

        data.text = self._clean_text(data.text)
        if not data.text and not data.has_any_media:
            data.error = "推文内容为空（可能是纯转推或已被删除）"
            return data
        data.url = build_tweet_url(data.author_screen_name or None, data.tweet_id)
        data.source = "syndication"
        data.fetched_at = time.time()
        return data

    @staticmethod
    def _clean_syndication_text(payload: dict[str, Any]) -> str:
        """从 Syndication 响应提取正文：裁剪可见范围、还原 t.co、反转义 HTML。

        display_text_range 之前的片段是「@某人」回复前缀，与 FxTwitter 的行为
        保持一致，只保留可见正文；text 里的 t.co 短链用 entities.urls 还原成
        原始链接。正文超出可见范围（超长推文被截断）时记一条 WARNING。
        """
        text = payload.get("text") or ""
        if not isinstance(text, str):
            return ""
        text_range = payload.get("display_text_range")
        if isinstance(text_range, list) and len(text_range) == 2:
            start = max(0, _to_int(text_range[0]) or 0)
            end = _to_int(text_range[1])
            if end is None:
                end = len(text)
            if end < len(text):
                logger.warning(
                    f"[{PLUGIN_NAME}] Syndication 推文 {payload.get('id_str')} 正文被截断"
                    f"（可见 {end} / 全文 {len(text)} 字符，超长推文拿不到完整正文）"
                )
            if 0 <= start < end:
                text = text[start:end]
        entities = payload.get("entities") or {}
        urls = entities.get("urls") if isinstance(entities, dict) else None
        for item in urls or []:
            if not isinstance(item, dict):
                continue
            short = item.get("url") or ""
            expanded = item.get("expanded_url") or ""
            if short and expanded and short in text:
                text = text.replace(short, expanded)
        return html.unescape(text).strip()

    @staticmethod
    def _collect_syndication_media(
        payload: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str]]:
        """从 mediaDetails / photos / entities.media 提取图片、视频直链与封面。"""
        images: list[str] = []
        videos: list[str] = []
        thumbnails: list[str] = []

        def _add(bucket: list[str], url: Any) -> None:
            if isinstance(url, str) and url.startswith("http") and url not in bucket:
                bucket.append(url)

        for item in payload.get("mediaDetails") or []:
            if not isinstance(item, dict):
                continue
            media_url = item.get("media_url_https") or ""
            item_type = (item.get("type") or "").lower()
            if item_type == "photo":
                _add(images, media_url)
            elif item_type in ("video", "animated_gif"):
                chosen = ""
                best_rate = -1
                info = item.get("video_info")
                for variant in (info.get("variants") if isinstance(info, dict) else None) or []:
                    if not isinstance(variant, dict):
                        continue
                    if (variant.get("content_type") or "").lower() != "video/mp4":
                        continue
                    rate = _to_int(variant.get("bitrate")) or 0
                    if rate >= best_rate:
                        best_rate = rate
                        chosen = variant.get("url") or ""
                _add(videos, chosen)
                _add(thumbnails, media_url)

        # photos 与 entities.media 是旧结构，个别推文仍在使用，做兼容
        for item in payload.get("photos") or []:
            if isinstance(item, dict):
                _add(images, item.get("url") or item.get("media_url_https"))
            elif isinstance(item, str):
                _add(images, item)
        entities = payload.get("entities")
        if isinstance(entities, dict):
            for item in entities.get("media") or []:
                if isinstance(item, dict):
                    _add(images, item.get("media_url_https"))
        return images, videos, thumbnails

    async def fetch_profile(self, handle_or_url: str) -> ProfileData:
        """拉取 X 个人主页资料（/2/profile/{handle}）。

        FxTwitter 的该端点返回 {"code", "message", "user": {...}}，
        其中粉丝数一类的数字都是字符串，这里统一转成 int。
        """
        handle = normalize_handle(handle_or_url)
        data = ProfileData(handle=handle)
        if not handle:
            data.error = "无法识别的 X 账号名"
            return data

        data.url = f"https://x.com/{handle}"
        api_url = PROFILE_API.format(handle=urllib.parse.quote(handle))
        try:
            response = await self.client.get(api_url, headers=self.headers())
        except Exception as exc:
            data.error = f"请求主页失败：{type(exc).__name__}: {exc}"
            return data

        if response.status_code == 404:
            data.error = f"账号 @{handle} 不存在或已改名"
            return data
        if response.status_code == 403:
            data.error = f"@ {handle} 的主页不可访问（可能是私有账号）"
            return data
        if response.status_code != 200:
            data.error = f"主页解析失败：HTTP {response.status_code}"
            return data

        try:
            payload = response.json()
        except Exception as exc:
            data.error = f"主页响应不是合法 JSON：{exc}"
            return data

        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or not user:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or "")
            data.error = f"接口未返回账号数据（{message or '未知原因'}）"
            return data

        data.handle = _clean_str(user.get("screen_name")).strip() or handle
        data.url = _clean_str(user.get("url")) or f"https://x.com/{data.handle}"
        data.name = _clean_str(user.get("name"))
        data.description = _clean_str(user.get("description"))
        data.avatar = _clean_str(user.get("avatar_url"))
        data.banner = _clean_str(user.get("banner_url"))
        data.location = _clean_str(user.get("location"))
        data.website = _clean_str(user.get("website"))
        data.joined = _clean_str(user.get("joined"))
        data.followers = _to_int(user.get("followers"))
        data.following = _to_int(user.get("following"))
        data.statuses = _to_int(user.get("statuses"))
        data.likes = _to_int(user.get("likes"))
        data.media_count = _to_int(user.get("media_count"))
        verification = user.get("verification")
        if isinstance(verification, dict):
            data.verified = str(verification.get("verified") or "").lower() in ("true", "1", "yes")
        else:
            data.verified = bool(verification)
        data.protected = bool(_truthy(user.get("protected")))
        data.source = "fxtwitter"
        data.fetched_at = time.time()
        return data

    @staticmethod
    def _apply_translation(data: TweetData, tweet: dict[str, Any]) -> None:
        """把 v2 响应里的 translation 字段写入 TweetData。"""
        translation = tweet.get("translation")
        if not isinstance(translation, dict):
            return
        text = (translation.get("text") or "").strip()
        if not text:
            return
        target_lang = translation.get("target_lang") or ""
        # 接口在原文已是目标语言时会把译文原样返回，这种“译文”没有价值，丢掉
        if text == (data.text or "").strip():
            logger.debug(f"[{PLUGIN_NAME}] 推文 {data.tweet_id} 无需翻译（原文即目标语言）")
            return
        data.translation_text = text
        data.translation_lang = target_lang
        data.translation_source_lang = translation.get("source_lang") or ""
        data.translation_provider = f"fxtwitter({translation.get('provider') or 'grok'})"

    async def _get_tweet_payload(
        self, api_url: str, tweet_id: str, translate_lang: str = ""
    ) -> tuple[TweetData, dict[str, Any]]:
        """请求 FxTwitter 并把响应归一化为 (TweetData, tweet 原始字典)。"""
        data = TweetData(tweet_id=tweet_id, url=build_tweet_url(None, tweet_id))

        if translate_lang:
            api_url = f"{api_url}?lang={urllib.parse.quote(translate_lang)}"

        try:
            response = await self.client.get(
                api_url, headers=self.headers(), timeout=self.timeout
            )
        except httpx.TimeoutException:
            data.error = "FxTwitter 接口请求超时"
            return data, {}
        except httpx.HTTPError as exc:
            data.error = f"FxTwitter 接口请求失败：{exc}"
            return data, {}
        except Exception as exc:  # pragma: no cover
            data.error = f"FxTwitter 接口未知错误：{exc}"
            return data, {}

        # FxTwitter 对无 UA 请求返回 401，私有推文同样返回 401
        if response.status_code == 401:
            data.error = "该推文不可访问（可能为私有账号或被限制）"
            return data, {}
        if response.status_code == 404:
            data.error = "推文不存在或已被删除"
            return data, {}
        if response.status_code >= 400:
            data.error = f"FxTwitter 接口返回 HTTP {response.status_code}"
            return data, {}

        try:
            payload = response.json()
        except Exception:
            data.error = "FxTwitter 接口返回了非 JSON 数据"
            return data, {}

        # v2 的顶层键是 status，v1 是 tweet，两者都做兼容
        tweet = payload.get("tweet") or payload.get("status") or {}
        if not isinstance(tweet, dict) or not tweet:
            message = payload.get("message") or "接口未返回推文数据"
            data.error = f"解析失败：{message}"
            return data, {}

        self.fill_from_tweet(data, tweet)
        return data, tweet

    def fill_from_tweet(self, data: TweetData, tweet: dict[str, Any]) -> None:
        """把 FxTwitter 的 tweet 对象填充进 TweetData（不含翻译字段）。"""
        author = tweet.get("author") or {}
        media = tweet.get("media") or {}

        data.url = tweet.get("url") or data.url
        data.text = tweet.get("text") or ""
        data.author_name = (author.get("name") or "").strip()
        data.author_screen_name = (author.get("screen_name") or "").strip()
        data.author_avatar = author.get("avatar_url") or ""
        # 认证（蓝V）：v2 用 author.verification.verified，旧字段 is_blue_verified 兜底
        verification = author.get("verification")
        if isinstance(verification, dict):
            data.author_verified = _truthy(verification.get("verified"))
            # 认证类型：individual（蓝）/ business（金）/ government（灰）
            v_type = str(verification.get("type") or "").strip().lower()
            data.author_verified_type = v_type if v_type in ("business", "government") else "individual"
        elif verification is not None:
            data.author_verified = _truthy(verification)
        else:
            data.author_verified = _truthy(author.get("is_blue_verified"))
        data.created_timestamp = tweet.get("created_timestamp")
        # 兼容标准字段与旧版字段
        data.likes = tweet.get("likes")
        data.retweets = (
            tweet.get("reposts") if tweet.get("reposts") is not None else tweet.get("retweets")
        )
        data.replies = tweet.get("replies")
        data.views = tweet.get("views")
        data.lang = tweet.get("lang") or ""
        data.is_note_tweet = bool(tweet.get("is_note_tweet"))

        # 原文未展开时（长推文）优先读取完整正文
        article = tweet.get("article")
        if isinstance(article, dict):
            article_text = article.get("text") or ""
            if article_text and len(article_text) > len(data.text):
                data.text = article_text

        data.images, data.videos, data.video_thumbnails = self._collect_media(media)

        quote = tweet.get("quote")
        if isinstance(quote, dict):
            data.quote_url = quote.get("url") or ""
            quote_text = quote.get("text") or ""
            if quote_text:
                quote_author = (quote.get("author") or {}).get("screen_name") or ""
                prefix = f"@{quote_author}: " if quote_author else ""
                data.quote_text = truncate_text(prefix + quote_text, 500)
            # 引用推文的 ID：优先用接口给的 id，其次从 URL 里解析
            data.quote_id = str(quote.get("id") or "").strip() or (
                normalize_tweet_ref(data.quote_url) or ""
            )
            # 引用推文自带的译文（FxTwitter 在 ?lang= 时也会给 quote 带上译文）
            quote_translation = quote.get("translation")
            if isinstance(quote_translation, dict):
                translated = (quote_translation.get("text") or "").strip()
                if translated and translated != quote_text.strip():
                    data.quote_translation_text = translated
                    data.quote_translation_lang = quote_translation.get("target_lang") or ""
                    provider = quote_translation.get("provider") or "grok"
                    data.quote_translation_provider = f"fxtwitter({provider})"
            # 引用推文的媒体单独存放：主贴与回复贴的图片/视频要能区分开，
            # 需要展示时由 _build_chain 决定是否并入（见 thread_split 配置）
            (
                data.quote_images,
                data.quote_videos,
                data.quote_video_thumbnails,
            ) = self._collect_media(quote.get("media") or {})

        # 被回复的推文（replying_to）：新版接口把主贴信息放在这里，
        # 旧字段 replying_to_status 已废弃，仅在它仍是 dict 时兜底读取
        reply = tweet.get("replying_to")
        if not isinstance(reply, dict):
            reply = tweet.get("replying_to_status")
        if isinstance(reply, dict):
            data.reply_to_id = str(reply.get("status") or reply.get("id") or "").strip()
            data.reply_to_author = (reply.get("screen_name") or "").strip()
            data.reply_to_url = reply.get("url") or ""
            if not data.reply_to_id and data.reply_to_url:
                data.reply_to_id = normalize_tweet_ref(data.reply_to_url) or ""
            if not data.reply_to_url and data.reply_to_id:
                data.reply_to_url = build_tweet_url(
                    data.reply_to_author or None, data.reply_to_id
                )

        # 去掉正文末尾指向自身的短链
        data.text = self._clean_text(data.text)

        if not data.text and not data.has_any_media:
            data.error = "推文内容为空（可能是纯转推或已被删除）"
            return

        data.source = "fxtwitter"
        data.fetched_at = time.time()

    @staticmethod
    def _collect_media(media: Any) -> tuple[list[str], list[str], list[str]]:
        """从 media 字段中提取图片、视频直链与视频封面。"""
        images: list[str] = []
        videos: list[str] = []
        thumbnails: list[str] = []

        if not isinstance(media, dict):
            return images, videos, thumbnails

        def _add(bucket: list[str], url: Any) -> None:
            if isinstance(url, str) and url.startswith("http") and url not in bucket:
                bucket.append(url)

        for item in media.get("all") or []:
            if not isinstance(item, dict):
                continue
            item_type = (item.get("type") or "").lower()
            if item_type in ("photo", "mosaic_photo"):
                formats = item.get("formats")
                if isinstance(formats, dict):
                    _add(images, formats.get("jpeg") or formats.get("webp"))
                _add(images, item.get("url"))
            elif item_type in ("video", "gif"):
                # 优先选择 MP4 直链，便于直接发送
                variants = item.get("variants") or []
                chosen = ""
                for variant in variants:
                    if not isinstance(variant, dict):
                        continue
                    if (variant.get("content_type") or "").lower() == "video/mp4":
                        chosen = variant.get("url") or ""
                        break
                _add(videos, chosen or item.get("url"))
                _add(thumbnails, item.get("thumbnail_url"))

        for item in media.get("photos") or []:
            if isinstance(item, dict):
                _add(images, item.get("url"))

        for item in media.get("videos") or []:
            if isinstance(item, dict):
                _add(videos, item.get("url"))
                _add(thumbnails, item.get("thumbnail_url"))

        return images, videos, thumbnails

    @staticmethod
    def _clean_text(text: str) -> str:
        """
        清理正文中无信息量的链接，并规范化空白。

        - 指向推文自身的 status 链接会原样出现（网页抓取时会附加），直接移除；
        - 尾部指向自身的 t.co 短链同样移除，图片与视频已经单独发送；
        - 其它普通链接保留，避免丢失推文内容。
        """
        if not text:
            return ""
        text = re.sub(r"https?://(?:x\.com|twitter\.com)/\S*?/status/\d+\S*", "", text)
        text = re.sub(r"https?://t\.co/[A-Za-z0-9_-]+", "", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    # ---------------------------- 源2：oEmbed ---------------------------- #

    async def fetch_via_oembed(self, tweet_id: str) -> TweetData:
        """通过 Twitter oEmbed 获取纯文本兜底内容。"""
        data = TweetData(tweet_id=tweet_id, url=build_tweet_url(None, tweet_id))
        params = {
            "url": build_tweet_url(None, tweet_id),
            "omit_script": "true",
            "dnt": "true",
            "lang": "en",
        }

        try:
            response = await self.client.get(
                OEMBED_API,
                params=params,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except httpx.TimeoutException:
            data.error = data.error or "Twitter oEmbed 接口请求超时"
            return data
        except Exception as exc:
            data.error = data.error or f"Twitter oEmbed 接口请求失败：{exc}"
            return data

        if response.status_code >= 400:
            data.error = data.error or f"oEmbed 接口返回 HTTP {response.status_code}"
            return data

        try:
            payload = response.json()
        except Exception:
            data.error = data.error or "oEmbed 接口返回了非 JSON 数据"
            return data

        author_name = (payload.get("author_name") or "").strip()
        author_url = payload.get("author_url") or ""
        html_block = payload.get("html") or ""

        screen_name = ""
        if author_url:
            match = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", author_url)
            if match:
                screen_name = match.group(1)

        text = self._extract_oembed_text(html_block)
        if not text:
            data.error = data.error or "oEmbed 未返回可用正文"
            return data

        data.text = text
        data.author_name = author_name
        data.author_screen_name = screen_name
        data.url = build_tweet_url(screen_name or None, tweet_id)
        data.source = "oembed"
        data.fetched_at = time.time()
        return data

    @staticmethod
    def _strip_oembed_signature(text: str) -> str:
        """移除 oEmbed 正文末尾的「— Author (@handle) 日期」署名行。"""
        if not text:
            return ""
        # 署名可能紧跟在正文之后（中间为空白或换行），也可能以 &mdash; 转义形式出现
        text = re.sub(
            r"\s*[—–-]{1,2}\s*[^\n()]{0,50}\((?:@)?[A-Za-z0-9_]{1,15}\)[^\n]*$",
            "",
            text,
        )
        return text.strip()

    @staticmethod
    def _extract_oembed_text(html_block: str) -> str:
        """从 oEmbed 返回的 blockquote HTML 中抽取纯文本正文。"""
        if not html_block:
            return ""

        if BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(html_block, "html.parser")
                paragraph = soup.find("p")
                if paragraph is not None:
                    # <br> 代表换行；<a> 之类的行内标签直接取文本
                    for br in paragraph.find_all("br"):
                        br.replace_with("\n")
                    raw = paragraph.get_text()
                    return XTweetParser._strip_oembed_signature(html.unescape(raw))
            except Exception as exc:
                logger.debug(f"[{PLUGIN_NAME}] BeautifulSoup 解析 oEmbed 失败，回退正则: {exc}")

        match = re.search(r"<p[^>]*>(.*?)</p>", html_block, re.DOTALL | re.IGNORECASE)
        raw = match.group(1) if match else html_block
        raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
        raw = re.sub(r"<[^>]+>", "", raw)
        # 先还原 HTML 实体，署名里的 &mdash; 才能被识别
        raw = html.unescape(raw)
        return XTweetParser._strip_oembed_signature(raw)

    # ---------------------------- 短链展开 ---------------------------- #

    async def expand_tco(self, url: str, max_redirects: int = 5) -> str:
        """
        展开 t.co 短链，返回最终真实 URL（失败时原样返回）。

        注意：t.co 目前**不再返回 301 Location 头**，而是返回 HTTP 200 + 一小段
        HTML，靠 `<noscript><META http-equiv="refresh" ...>`、`<title>` 和
        `location.replace("...")` 三处之一携带真实目标地址。因此这里必须
        按顺序尝试：Location 头 → meta refresh → title → location.replace。
        """
        if not url:
            return url

        current = url
        try:
            for _ in range(max_redirects):
                response = await self.client.get(
                    current,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    },
                    follow_redirects=False,
                    timeout=self.timeout,
                )

                # 1) 标准重定向
                location = response.headers.get("location")
                if response.status_code in (301, 302, 303, 307, 308) and location:
                    current = urllib.parse.urljoin(current, location)
                    if extract_tweet_ids(current):
                        return current
                    continue

                # 2/3/4) t.co 的客户端跳转载体
                target = _extract_client_redirect(response.text)
                if target and target != current:
                    current = target
                    if extract_tweet_ids(current):
                        return current
                    continue
                return current
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 展开短链 {url} 失败: {exc}")
        return current

    # ---------------------------- 描述文本 ---------------------------- #

    @staticmethod
    def build_description(data: TweetData, limit: int = MAX_DESC_LEN) -> str:
        """把推文数据整理为供 LLM 阅读的结构化纯文本。"""
        if not data.ok:
            return f"解析失败：{data.error or '未知错误'}"

        parts = [
            f"作者：{data.author_display}",
            f"链接：{data.url or build_tweet_url(data.author_screen_name, data.tweet_id)}",
        ]

        created = format_relative_time(data.created_timestamp)
        if created:
            parts.append(f"发布时间：{created}")

        if data.quote_text:
            parts.append(f"引用内容：{data.quote_text}")

        parts.append("正文：\n" + (data.text or "（无正文，可能仅含媒体）"))

        if data.images:
            parts.append("图片：\n" + "\n".join(data.images))
        if data.videos:
            parts.append("视频：\n" + "\n".join(data.videos))
        if data.quote_url:
            parts.append(f"被引用推文：{data.quote_url}")

        stats = []
        if data.likes is not None:
            stats.append(f"点赞 {format_count(data.likes)}")
        if data.retweets is not None:
            stats.append(f"转发 {format_count(data.retweets)}")
        if data.replies is not None:
            stats.append(f"回复 {format_count(data.replies)}")
        if data.views is not None:
            stats.append(f"浏览 {format_count(data.views)}")
        if stats:
            parts.append("互动：" + "，".join(stats))

        description = "\n".join(parts)
        return truncate_text(description, limit)

# --------------------------------------------------------------------------- #
# 五、翻译服务 Translator
# --------------------------------------------------------------------------- #


class Translator:
    """
    大模型翻译服务（FxTwitter 自带翻译不可用时的回退方案）。

    通过 AstrBot 的 Context.llm_generate 调用用户已经配置好的任意对话模型，
    因此不需要在插件里额外填写 API Key，也不会绕过 AstrBot 的密钥管理。
    推荐使用专门的翻译小模型（例如腾讯混元 MT：openai/Hy-MT2-30B-A3B）。
    """

    def __init__(
        self,
        context: Context,
        provider_id: str = "",
        timeout: float = 30.0,
        max_chars: int = 2000,
    ) -> None:
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.timeout = timeout
        # 超长推文只翻译前 max_chars 个字符，避免超出小模型的上下文窗口
        self.max_chars = max_chars
        # 运行时统计，供 /xt 状态 展示
        self.success_count = 0
        self.failure_count = 0
        self.last_error = ""
        # 配置的模型不存在时只警告一次，避免刷屏
        self._provider_warned = False

    async def available_provider_ids(self) -> list[str]:
        """返回当前 AstrBot 里可用的对话模型 ID 列表。"""
        try:
            providers = self.context.get_all_providers() or []
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 读取模型列表失败：{exc}")
            return []
        ids: list[str] = []
        for provider in providers:
            meta = getattr(provider, "meta", None)
            # AstrBot 的 Provider.meta 是方法而不是属性，必须调用后才拿得到 id
            if callable(meta):
                try:
                    meta = meta()
                except Exception:
                    continue
            provider_id = str(getattr(meta, "id", "") or "").strip()
            if provider_id and provider_id not in ids:
                ids.append(provider_id)
        return ids

    async def resolve_provider_id(self) -> str:
        """确定要使用的模型 ID。

        优先使用配置项，但会先确认它真实存在；不存在或未配置时回退到当前
        AstrBot 里第一个可用的对话模型，避免整条翻译链路静默失效。
        """
        available = await self.available_provider_ids()
        if self.provider_id:
            if not available or self.provider_id in available:
                return self.provider_id
            if not self._provider_warned:
                self._provider_warned = True
                logger.warning(
                    f"[{PLUGIN_NAME}] 配置的翻译模型 {self.provider_id} 不存在，"
                    f"已自动改用 {available[0]}（可在插件配置里重新选择）"
                )
            return available[0]
        return available[0] if available else ""

    async def translate(self, text: str, target_lang: str, source_lang: str = "") -> str:
        """把 text 翻译成 target_lang；失败返回空字符串（调用方负责降级）。"""
        if not text or not text.strip():
            return ""

        provider_id = await self.resolve_provider_id()
        if not provider_id:
            self.failure_count += 1
            self.last_error = "没有可用的对话模型"
            logger.warning(f"[{PLUGIN_NAME}] 无法翻译：未配置翻译模型且没有可用的对话模型")
            return ""

        prompt = build_translate_prompt(text[: self.max_chars], target_lang, source_lang)
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=(
                        "你是一名专业的推文翻译器，只输出译文，不添加任何解释或格式。"
                    ),
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.failure_count += 1
            self.last_error = f"翻译模型超时（{provider_id}）"
            logger.warning(f"[{PLUGIN_NAME}] 大模型翻译超时：{provider_id}")
            return ""
        except Exception as exc:
            self.failure_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[{PLUGIN_NAME}] 大模型翻译失败（{provider_id}）：{exc}")
            return ""

        result = (getattr(response, "completion_text", "") or "").strip()
        if not result:
            self.failure_count += 1
            self.last_error = "翻译模型返回了空内容"
            return ""
        self.success_count += 1
        self.last_error = ""
        return result



# 动态导出：`from .x_tweet_core import *` 要连 _ 前缀的常量、正则、私有工具函数一起带出去，
# 因此不能依赖 Python「import * 跳过下划线名字」的默认行为。
__all__ = [name for name in list(globals()) if not name.startswith("__")]
