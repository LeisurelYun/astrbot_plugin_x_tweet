# -*- coding: utf-8 -*-
"""
astrbot_plugin_x_tweet —— 公共基础层

包含所有可调常量与正则、与框架无关的纯工具函数，以及 TweetData / ProfileData 两个数据结构。
本模块不依赖 AstrBot 的运行时对象（只用 astrbot.api.logger 打日志），
供 x_tweet_parser / x_tweet_media / main 通过 `from .x_tweet_core import *` 复用。

作者：Leisurelyun
版本：v1.13.1
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import weakref
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp

try:  # MessageChain 用于「主贴 / 主页单独先发一条消息」的即时发送
    from astrbot.api.event import MessageChain
except Exception:  # pragma: no cover - 兼容未导出的版本
    try:
        from astrbot.core.message.message_event_result import MessageChain  # type: ignore
    except Exception:  # pragma: no cover
        MessageChain = None  # type: ignore[assignment]

try:  # AstrBotConfig 在不同版本中的位置可能不同，做兼容导入
    from astrbot.core.config.astrbot_config import AstrBotConfig
except Exception:  # pragma: no cover - 仅用于类型标注兜底
    AstrBotConfig = Any  # type: ignore[assignment,misc]

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 未安装时退化为正则解析
    BeautifulSoup = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "astrbot_plugin_x_tweet"
# 旧版默认缓存目录名（data/x_tweet_cache）。v1.10.0 起默认目录改为 AstrBot 规范的
# data/plugin_data/<插件名>/cache，这个常量仅保留给「配置里填了旧目录」的兼容判断。
LEGACY_CACHE_DIRNAME = "x_tweet_cache"

# --------------------------------------------------------------------------- #
# 一、常量与正则
#
# 所有外部接口地址、可选值枚举与默认值都集中在本节，便于审查与调整。
# 真正的默认值只在 _conf_schema.json 与函数签名里出现，这里放“可选项定义”。
# --------------------------------------------------------------------------- #

# FxTwitter API 基址。v2 支持 ?lang= 机器翻译（由 FxTwitter 侧调用 Grok 完成），
# v1 不支持翻译，仅作为 v2 不可用时的兜底。
FXTWITTER_API_BASE = "https://api.fxtwitter.com"
TWEET_API_V2 = FXTWITTER_API_BASE + "/2/status/{tweet_id}"
TWEET_API_V1 = FXTWITTER_API_BASE + "/i/status/{tweet_id}"
OEMBED_API = "https://publish.twitter.com/oembed"

# Syndication CDN：X 官方嵌入组件的取数端点，无需登录与密钥，作为 FxTwitter
# 故障时的异构备用源。注意：对超长推文只返回截断正文，且无转发数 / 浏览量。
SYNDICATION_API = "https://cdn.syndication.twimg.com/tweet-result"
# 解析源优先级顺序（可在面板调整）。未列出的源会自动追加到末尾，不会丢源。
DEFAULT_PARSE_SOURCE_ORDER: list[str] = ["fxtwitter", "mirror", "syndication", "oembed"]
PARSE_SOURCE_CHOICES: dict[str, str] = {
    "fxtwitter": "FxTwitter 主站（v2 → v1，支持自带翻译）",
    "mirror": "镜像实例（vx 系 / FxTwitter 同构）",
    "syndication": "Syndication CDN（X 官方嵌入端点）",
    "oembed": "oEmbed（纯文本兜底）",
}
# vx 系镜像（vxtwitter 等）必须带 Referer 才放行，否则一律 403
VX_MIRROR_REFERER = "https://x.com/"
# 是否启用 FxTwitter 镜像实例轮询（FxTwitter 主站单点故障时的第二级保障）
DEFAULT_ENABLE_MIRROR_SOURCES = True
# 镜像实例列表：FxTwitter 同构或 vx 系 API 的部署（形如 https://api.vxtwitter.com）。
# 默认为空——公共镜像可用性不稳定（403 / TLS 断开），留给用户按需填写。
DEFAULT_MIRROR_FX_INSTANCES: list[str] = []

# 图片尺寸档位：配置值 -> (Twitter CDN 的 name 参数, 面板里显示的名字)
# pbs.twimg.com 支持的取值实测量级：orig≈large(2048px) > medium(1200px) > small(680px)
IMAGE_SIZE_CHOICES: dict[str, tuple[str, str]] = {
    "orig": ("orig", "原图"),
    "large": ("large", "大图（2048px）"),
    "medium": ("medium", "中图（1200px）"),
    "small": ("small", "小图（680px）"),
}
DEFAULT_IMAGE_SIZE = "medium"

# 正文展示模式。both=原文与译文都发；original=只发原文；translation=只发译文。
# 配置项 text_content_mode 使用这里的取值。
TEXT_CONTENT_MODE_CHOICES = ("both", "original", "translation")
DEFAULT_TEXT_CONTENT_MODE = "both"
TEXT_CONTENT_MODE_LABELS: dict[str, str] = {
    "both": "原文 + 译文",
    "original": "只发原文",
    "translation": "只发译文",
}

# 翻译模式。off=不翻译；auto=优先用 FxTwitter 免费翻译，失败再调用大模型；
# fxtwitter=只用 FxTwitter；llm=只用大模型。
TRANSLATE_MODE_CHOICES = ("off", "auto", "fxtwitter", "llm")
DEFAULT_TRANSLATE_MODE = "auto"
DEFAULT_TRANSLATE_LANG = "zh-cn"
DEFAULT_TRANSLATE_PROVIDER_ID = ""  # 留空 = 用第一个可用的对话模型（硬编码某个模型在别人机器上多半不存在）
# FxTwitter ?lang= 的取值白名单：不在此列的取值接口会忽略翻译，因此统一做归一化
TRANSLATE_LANG_CHOICES: dict[str, str] = {
    "zh-cn": "简体中文",
    "zh-tw": "繁體中文",
    "zh": "中文（同简体）",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "it": "Italiano",
    "ar": "العربية",
    "th": "ไทย",
    "vi": "Tiếng Việt",
    "id": "Bahasa Indonesia",
    "tr": "Türkçe",
    "hi": "हिन्दी",
    "pl": "Polski",
    "nl": "Nederlands",
    "uk": "Українська",
    "fa": "فارسی",
    "he": "עברית",
    "ms": "Bahasa Melayu",
}
# 语言代码归一化（兼容用户填 zh_CN / ZH / 中文 等写法）
_TRANSLATE_LANG_ALIASES: dict[str, str] = {
    "zh_cn": "zh-cn",
    "zh-hans": "zh-cn",
    "zh_hans": "zh-cn",
    "cn": "zh-cn",
    "zhongwen": "zh-cn",
    "中文": "zh-cn",
    "简体": "zh-cn",
    "简体中文": "zh-cn",
    "zh_tw": "zh-tw",
    "zh-hant": "zh-tw",
    "zh_hant": "zh-tw",
    "tw": "zh-tw",
    "繁體": "zh-tw",
    "繁体": "zh-tw",
    "繁體中文": "zh-tw",
    "jp": "ja",
    "kr": "ko",
    "eng": "en",
}

# 表情回应的候选编码。QQ 后端对 emoji_id 的约定不统一，这里按顺序尝试：
#   * 部分后端把系统表情编码为 3 位数字（144 = 👍）
#   * Lagrange / NapCat 等按 QQ 官方 emoji 表使用 6 位编码（128077 = 👍）
DEFAULT_EMOJI_ID = "128077"
EMOJI_ID_FALLBACKS: dict[str, str] = {DEFAULT_EMOJI_ID: "144"}

# --------------------------------------------------------------------------- #
# X 搜索（/2/profile/{handle}/statuses）
# --------------------------------------------------------------------------- #
# 说明：FxTwitter 的全局搜索端点 /2/search 目前对普通调用者一律返回 404，
# 因此这里采用「账号时间线 + 本地关键词过滤」的方案：
#   * 用 /2/profile/{handle}/statuses 拉取账号时间线（该端点稳定可用）
#   * 再用关键词白名单 / 黑名单、时间窗口在本地过滤
# v1.5.2 起：账号来源有两种——配置的「搜索账号列表」；账号列表为空时用
# /2/typeahead?q=关键词 自动匹配的相关账号（search_auto_accounts 控制开关）。
SEARCH_STATUSES_API = "https://api.fxtwitter.com/2/profile/{handle}/statuses"
# 按关键词查找相关账号（无需登录，FxTwitter 站内搜索框同款）
TYPEAHEAD_API = "https://api.fxtwitter.com/2/typeahead"
# data/<tweet_id>.json 解析缓存的保留天数，超过后由定时清理任务删除
DATA_CACHE_TTL_DAYS = 7
DEFAULT_SEARCH_ACCOUNTS: list[str] = []
# 单次搜索返回条数：默认 3，最多 5（超出会被夹到上限）
DEFAULT_SEARCH_COUNT = 3
SEARCH_COUNT_MAX = 5
# typeahead 自动匹配结果的内存缓存时长（秒）：防止短时间内重复搜索触发 429 限速
TYPEAHEAD_CACHE_TTL_SECONDS = 900
# 搜索结果的交付方式：forward=合并转发（聊天记录，每条一个节点）；
# text=一条纯文本；image=渲染成图片卡片（AstrBot 文转图，失败自动回退）
DEFAULT_SEARCH_DELIVERY = "forward"
SEARCH_DELIVERY_CHOICES: dict[str, str] = {
    "forward": "合并转发（聊天记录）",
    "text": "一条纯文本",
    "image": "渲染成图片",
}
# 搜索结果是否补译文：默认关闭（补译文要额外请求，按需在面板开启）
DEFAULT_SEARCH_TRANSLATE = False
# 补译文时的并发上限（同时请求多少条）
DEFAULT_SEARCH_TRANSLATE_CONCURRENCY = 3

# 推文图片卡片的交付方式：media=文字与媒体混排（默认，旧行为）；
# card=X 风格卡片（头像 / 昵称 / 蓝V / 日期 / 正文 / 互动栏 / 引用推文）+ 媒体另发
DEFAULT_TWEET_DELIVERY = "media"
TWEET_DELIVERY_CHOICES: dict[str, str] = {
    "media": "文字 + 媒体混排（默认）",
    "card": "X 风格卡片（媒体另发）",
}
# X 风格推文卡片模板（Jinja2 + 内联 CSS）。头像以 base64 内嵌——
# 实测渲染服务器拉不到 pbs.twimg.com，直链会破图。
# ---- X 官方风格的内联 SVG 图标 ----
# 刻意不用 emoji：emoji 的字形与配色随平台/字体差异很大（同一个 👍 在 Windows 是彩色、
# 在 X 上是单色线条），还原度差。这里直接内联 X 的图标路径，单色、大小可控。
_X_SVG = 'class="xicon" viewBox="0 0 24 24" aria-hidden="true"'
X_ICON_REPLY = (
    f"<svg {_X_SVG}>"
    '<path d="M1.751 10c0-4.42 3.584-8 8.005-8h4.366c4.49 0 8.129 3.64 8.129 8.13'
    ' 0 2.96-1.607 5.68-4.196 7.11l-8.054 4.46v-3.69h-.067c-4.49.1-8.183-3.51-8.183-8.01z"/>'
    "</svg>"
)
X_ICON_RETWEET = (
    f"<svg {_X_SVG}>"
    '<path d="M4.5 3.88l4.432 4.14-1.364 1.46L5.5 7.55V16c0 1.1.896 2 2 2H13v2H7.5c-2.209 0-4-1.79-4-4V7.55'
    'L1.432 9.48.068 8.02 4.5 3.88zM16.5 6H11V4h5.5c2.209 0 4 1.79 4 4v8.45l2.068-1.93 1.364 1.46'
    '-4.432 4.14-4.432-4.14 1.364-1.46 2.068 1.93V8c0-1.1-.896-2-2-2z"/>'
    "</svg>"
)
X_ICON_LIKE = (
    f"<svg {_X_SVG}>"
    '<path d="M16.697 5.5c-1.222-.06-2.679.51-3.89 2.16l-.805 1.09-.806-1.09C9.984 6.01 8.526 5.44 7.304 5.5'
    'c-1.243.07-2.349.78-2.91 1.91-.552 1.12-.633 2.78.479 4.82 1.074 1.97 3.257 4.27 7.129 6.61'
    ' 3.87-2.34 6.052-4.64 7.126-6.61 1.111-2.04 1.03-3.7.477-4.82-.561-1.13-1.666-1.84-2.908-1.91z"/>'
    "</svg>"
)
X_ICON_VIEWS = (
    f"<svg {_X_SVG}>"
    '<path d="M8.75 21V3h2v18h-2zM18 21V8.5h2V21h-2zM13.75 21V11.5h2V21h-2zM3.5 21v-6h2v6h-2z"/>'
    "</svg>"
)
X_ICON_BOOKMARK = (
    f"<svg {_X_SVG}>"
    '<path d="M4 4.5C4 3.12 5.119 2 6.5 2h11C18.881 2 20 3.12 20 4.5v18.44l-8-5.71-8 5.71V4.5z"/>'
    "</svg>"
)
X_ICON_SHARE = (
    f"<svg {_X_SVG}>"
    '<path d="M12 2.59l5.7 5.7-1.41 1.42L13 6.41V16h-2V6.41l-3.3 3.3-1.41-1.42L12 2.59zM21 15l-.02 3.51'
    'c0 1.38-1.12 2.49-2.5 2.49H5.5C4.11 21 3 19.88 3 18.5V15h2v3.5c0 .28.22.5.5.5h12.98c.28 0 .5-.22.5-.5'
    'L19 15h2z"/>'
    "</svg>"
)
# 认证徽章：X 的蓝色多角徽章形状 + 白色对勾（两个 path 叠出来）。
# 形状相同、颜色不同，对应 X 的三档认证：蓝=个人 / 金=商家组织 / 灰=政府官方。
_BADGE_SHAPE = (
    '<path fill="{color}" d="M22.25 12c0-1.43-.88-2.67-2.19-3.34.46-1.39.2-2.9-.81-3.91s-2.52-1.27-3.91-.81'
    'c-.66-1.31-1.91-2.19-3.34-2.19s-2.67.88-3.33 2.19c-1.4-.46-2.91-.2-3.92.81s-1.26 2.52-.8 3.91'
    'c-1.31.67-2.2 1.91-2.2 3.34s.89 2.67 2.2 3.34c-.46 1.39-.21 2.9.8 3.91s2.52 1.26 3.91.81'
    'c.67 1.31 1.91 2.19 3.34 2.19s2.68-.88 3.34-2.19c1.39.45 2.9.2 3.91-.81s1.27-2.52.81-3.91'
    'c1.31-.67 2.19-1.91 2.19-3.34z"/>'
)
_BADGE_CHECK = (
    '<path fill="#ffffff" d="M10.54 16.2L6.8 12.46l1.41-1.42 2.26 2.26 4.8-5.23 1.47 1.36-6.2 6.77z"/>'
)
_X_BADGE_HEAD = '<svg class="xbadge" viewBox="0 0 24 24" aria-hidden="true">'
# 蓝：个人认证（X 的 #1D9BF0）
X_BADGE_BLUE = _X_BADGE_HEAD + _BADGE_SHAPE.format(color="#1d9bf0") + _BADGE_CHECK + "</svg>"
# 金：商家 / 组织认证
X_BADGE_GOLD = _X_BADGE_HEAD + _BADGE_SHAPE.format(color="#e2b719") + _BADGE_CHECK + "</svg>"
# 灰：政府 / 官方机构
X_BADGE_GREY = _X_BADGE_HEAD + _BADGE_SHAPE.format(color="#829aab") + _BADGE_CHECK + "</svg>"

# 卡片主题（浅色 / 深色）配色，取值参考 X 官方：
#   浅色：白底 + #0f1419 主文字 + #536471 次要文字 + #eff3f4 分隔线
#   深色：纯黑底 + #e7e9ea 主文字 + #71767b 次要文字 + #2f3336 分隔线
X_THEME_LIGHT: dict[str, str] = {
    "bg": "#ffffff",
    "text": "#0f1419",
    "muted": "#536471",
    "faint": "#8b98a5",
    "border": "#eff3f4",
    "border_strong": "#cfd9de",
    "surface": "#f7f9f9",
    "icon": "#536471",
}
X_THEME_DARK: dict[str, str] = {
    "bg": "#000000",
    "text": "#e7e9ea",
    "muted": "#71767b",
    "faint": "#71767b",
    "border": "#2f3336",
    "border_strong": "#333639",
    "surface": "#16181c",
    "icon": "#71767b",
}
# 卡片主题配置：light / dark / auto（auto = 在下面的时段内自动用深色）
DEFAULT_CARD_THEME = "auto"
CARD_THEME_CHOICES: dict[str, str] = {
    "light": "浅色（白天）",
    "dark": "深色（黑夜）",
    "auto": "自动（19:00-07:00 用深色）",
}
# 深色时段默认值（左闭右开：19 点到次日 7 点），可在配置里改
DEFAULT_CARD_DARK_START_HOUR = 19
DEFAULT_CARD_DARK_END_HOUR = 7

# X 风格卡片样式：头像在左、内容在右的两列布局；楼层之间用灰色竖线连接
# （还原 X 的 thread 视图：上下两个头像之间有一条灰线）。
# 颜色全部走 Jinja2 变量注入，同一套模板同时支持浅色 / 深色两种主题。
# zoom: 2 —— 按 2 倍分辨率渲染（布局比例不变），否则 720px / 17px 的 1x 出图在高分屏上看字会糊。
X_CARD_STYLE = """\
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: {{ theme.bg }}; height: auto; }
  .xcard { width: 720px; padding: 22px 24px; zoom: 2; background: {{ theme.bg }};
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; color: {{ theme.text }}; }
  .xhead-top { display: flex; align-items: flex-start; gap: 10px; padding-bottom: 14px; }
  .xsub { font-size: 14px; color: {{ theme.muted }}; margin-top: 4px; }
  .xthread-item { display: flex; gap: 12px; }
  .xavatar-col { display: flex; flex-direction: column; align-items: center; flex: none; width: 48px; }
  .xavatar { width: 48px; height: 48px; border-radius: 50%; object-fit: cover; flex: none; }
  .xavatar-ph { background: {{ theme.border_strong }}; color: {{ theme.bg }}; font-size: 22px;
    font-weight: 700; display: flex; align-items: center; justify-content: center; }
  .xline { flex: 1; width: 2px; background: {{ theme.border_strong }}; margin-top: 6px; }
  .xmain { flex: 1; min-width: 0; padding-bottom: 18px; }
  .xnameline { display: flex; align-items: center; flex-wrap: wrap; gap: 5px;
    font-size: 15px; line-height: 22px; }
  .xname { font-weight: 700; color: {{ theme.text }}; }
  .xbadge { width: 18px; height: 18px; flex: none; }
  .xhandle, .xdate { color: {{ theme.muted }}; font-size: 15px; }
  .xspacer { flex: 1; }
  .xlogo { color: {{ theme.text }}; font-size: 20px; font-weight: 700; flex: none; }
  .xbody { margin-top: 4px; font-size: 17px; line-height: 1.55; word-break: break-word; }
  .xtrans { color: {{ theme.text }}; }
  .xtext { color: {{ theme.muted }}; font-size: 15px; margin-top: 6px; }
  .xmedia { margin-top: 10px; padding: 10px 14px; background: {{ theme.surface }};
    border: 1px solid {{ theme.border }}; border-radius: 12px; color: {{ theme.muted }}; font-size: 14px; }
  .xquote { margin-top: 12px; padding: 12px 14px; border: 1px solid {{ theme.border_strong }};
    border-radius: 16px; }
  .xqhead { display: flex; align-items: center; flex-wrap: wrap; gap: 5px; font-size: 14px; }
  .xqname { font-weight: 700; color: {{ theme.text }}; }
  .xqhandle, .xqdate { color: {{ theme.muted }}; }
  .xqtext { margin-top: 6px; font-size: 15px; line-height: 1.5; color: {{ theme.text }}; }
  .xlink { margin-top: 8px; font-size: 13px; color: #1d9bf0; word-break: break-all; }
  .xstats { display: flex; align-items: center; gap: 34px; margin-top: 12px; padding-top: 10px;
    border-top: 1px solid {{ theme.border }}; color: {{ theme.muted }}; font-size: 14px; }
  .xstat { display: inline-flex; align-items: center; gap: 6px; }
  .xtools { display: inline-flex; align-items: center; gap: 16px; }
  .xicon { width: 18px; height: 18px; fill: {{ theme.icon }}; flex: none; }
  .xfooter { text-align: center; color: {{ theme.faint }}; font-size: 12px; margin-top: 6px; }
</style>
"""

# 互动栏图标按 kind 选择（模板内联，避免走 Jinja2 转义）
_STAT_ICONS = (
    '{% if s.kind == "reply" %}'
    + X_ICON_REPLY
    + '{% elif s.kind == "retweet" %}'
    + X_ICON_RETWEET
    + '{% elif s.kind == "like" %}'
    + X_ICON_LIKE
    + '{% elif s.kind == "views" %}'
    + X_ICON_VIEWS
    + "{% endif %}"
)

# 认证徽章按类型选择（business=金 / government=灰 / 其它=蓝）
_BADGE_BY_TYPE = (
    '{% if item.badge_type == "business" %}'
    + X_BADGE_GOLD
    + '{% elif item.badge_type == "government" %}'
    + X_BADGE_GREY
    + "{% else %}"
    + X_BADGE_BLUE
    + "{% endif %}"
)

# 统一的 X 风格卡片模板：单推文 / 楼中楼（show_line=True 时楼层间画连接线）/
# 搜索结果（带 header 与 footer）共用同一个模板，样式完全一致。
X_CARD_TMPL = (
    X_CARD_STYLE
    + """\
<div class="xcard">
  {% if header %}
  <div class="xhead-top">
    <div class="xmeta">
      <div class="xnameline"><span class="xname">{{ header.title }}</span></div>
      {% if header.sub %}<div class="xsub">{{ header.sub }}</div>{% endif %}
    </div>
    <div class="xlogo">&#120143;</div>
  </div>
  {% endif %}
  {% for item in items %}
  <div class="xthread-item">
    <div class="xavatar-col">
      {% if item.avatar %}
      <img class="xavatar" src="{{ item.avatar }}">
      {% else %}
      <div class="xavatar xavatar-ph">{{ item.initial }}</div>
      {% endif %}
      {% if show_line and not loop.last %}<div class="xline"></div>{% endif %}
    </div>
    <div class="xmain">
      <div class="xnameline">
        <span class="xname">{{ item.name }}</span>
        {% if item.verified %}"""
    + _BADGE_BY_TYPE
    + """{% endif %}
        <span class="xhandle">@{{ item.handle }}</span>
        <span class="xdate">· {{ item.date }}</span>
        <span class="xspacer"></span>
        <span class="xlogo">&#120143;</span>
      </div>
      <div class="xbody">
        {% if item.translation %}<div class="xtrans">{{ item.translation }}</div>{% endif %}
        {% if item.text %}<div class="xtext">{{ item.text }}</div>{% endif %}
        {% if not item.translation and not item.text %}<div class="xtext">（本条推文没有文字内容）</div>{% endif %}
      </div>
      {% if item.media_hint %}<div class="xmedia">{{ item.media_hint }}</div>{% endif %}
      {% if item.quote %}
      <div class="xquote">
        <div class="xqhead">
          <span class="xqname">{{ item.quote.name }}</span>
          <span class="xqhandle">@{{ item.quote.handle }}</span>
          <span class="xqdate">· {{ item.quote.date }}</span>
        </div>
        <div class="xqtext">{{ item.quote.text }}</div>
      </div>
      {% endif %}
      {% if item.url %}<div class="xlink">{{ item.url }}</div>{% endif %}
      {% if item.stats %}
      <div class="xstats">
        {% for s in item.stats %}<span class="xstat">"""
    + _STAT_ICONS
    + """<span>{{ s.value }}</span></span>{% endfor %}
        <span class="xspacer"></span>
        <span class="xtools">"""
    + X_ICON_BOOKMARK
    + X_ICON_SHARE
    + """</span>
      </div>
      {% endif %}
    </div>
  </div>
  {% endfor %}
  {% if footer %}<div class="xfooter">{{ footer }}</div>{% endif %}
</div>
"""
)

# 说明：账号列表为空时一律按关键词向 /2/typeahead 自动匹配相关账号（不再单独设开关）
# 单次搜索最多查询几个账号（自动匹配与配置列表共用这一个上限）
DEFAULT_SEARCH_ACCOUNT_LIMIT = 5
# 默认黑名单：过滤掉常见的成人 / 广告 / 拉群内容，用户可在配置面板里改
DEFAULT_BLOCKED_KEYWORDS: list[str] = [
    "nsfw",
    "porn",
    "porno",
    "xxx",
    "r18",
    "onlyfans",
    "18+",
    "约炮",
    "色情",
    "裸聊",
    "福利姬",
    "博彩",
    "赌博",
    "赌场",
    "六合彩",
    "加微信",
    "加群",
    "扫码进群",
    "私聊我",
    "优惠券",
    "返利",
    "刷单",
    "兼职日结",
    "代理招商",
]

# --------------------------------------------------------------------------- #
# X 个人主页（/2/profile/{handle}）
# --------------------------------------------------------------------------- #
PROFILE_API = FXTWITTER_API_BASE + "/2/profile/{handle}"
# 主页头像下载时使用的伪推文 ID 前缀（缓存文件名形如 profile_elonmusk_...）
PROFILE_CACHE_PREFIX = "profile_"
DEFAULT_PROFILE_POST_COUNT = 3
# 主页的交付方式：card=渲染成 X 风格主页卡片（横幅 / 大头像 / 简介 / 元信息 / 关注数 /
# 最近推文），text=旧的纯文字资料卡 + 头像图 + 推文清单
DEFAULT_PROFILE_DELIVERY = "card"
PROFILE_DELIVERY_CHOICES: dict[str, str] = {
    "card": "X 风格主页卡片",
    "text": "纯文字资料卡",
}
# 主页资料的内存缓存时长（秒）：主页数据变化慢，缓存可避免同一链接被反复请求
PROFILE_CACHE_TTL = 600

# 一条消息里最多主动发出多少条消息（主贴 + 回复贴会分开成两条，防止刷屏上限）
MAX_AUTO_MESSAGES = 3

# ---- 楼中楼（多层引用 / 回复链）默认值 ----
# 最多向上追溯的楼层数（含链接本身）：1 表示只解析这一条
DEFAULT_THREAD_CHAIN_DEPTH = 4
MAX_THREAD_CHAIN_DEPTH = 10
# 楼层数达到该值时改用「合并转发」（聊天记录）发送
DEFAULT_THREAD_MERGE_MIN_NODES = 3
# 合并转发的节点里是否带上每层楼的图片
DEFAULT_THREAD_MERGE_MEDIA = True
# 楼中楼的交付方式：bubble=逐层引用气泡；forward=一条合并转发（每层一个节点）；
# card=把整条楼中楼渲染成一张 X 风格卡片图（楼层之间用灰色竖线连接）
DEFAULT_THREAD_DELIVERY = "forward"
THREAD_DELIVERY_CHOICES: dict[str, str] = {
    "forward": "合并转发（每层一个节点）",
    "bubble": "逐层引用气泡",
    "card": "X 风格卡片（一张长图）",
}
# 合并转发的节点里是否在层号标签后渲染「↩ 回复 @上一层：摘要」文字引用块
# （节点是本地合成的、没有真实 message_id，塞不进真正的引用气泡，用文字表达回复关系）
DEFAULT_THREAD_FORWARD_QUOTE = True

# ---- 翻译相关默认值 ----
# FxTwitter 自带译文偶尔会缺一次，先重试几次再回退到大模型（0 = 直接回退）
DEFAULT_TRANSLATE_RETRY = 2
MAX_TRANSLATE_RETRY = 3
# 两次重试之间的等待秒数，按 1 倍、2 倍递增
TRANSLATE_RETRY_DELAY_SECONDS = 0.4
# 去掉 @提及 / 链接 / 表情后，有效字符少于该值就不调用大模型（0 = 不限制）
DEFAULT_TRANSLATE_LLM_MIN_CHARS = 4
# 「只发译文」模式下拿不到译文时，正文前面加的一行提示（原文已是目标语言时不加）
TRANSLATION_UNAVAILABLE_HINT = "⚠️ 翻译不可用，以下为原文"

# 目标语言 -> 该语言的特征字符区间。仅用于 lang 字段缺失时粗略判断
# 「原文是不是已经是目标语言」，从而避免误报「翻译不可用」。
_LANG_CHAR_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    "zh": ((0x4E00, 0x9FFF), (0x3400, 0x4DBF)),  # 汉字（日文假名占比高时不会被判成中文）
    "ja": ((0x3040, 0x30FF),),                    # 平假名 + 片假名（中文文本不含，避免中日混淆）
    "ko": ((0xAC00, 0xD7AF), (0x1100, 0x11FF)),   # 谚文
    "ru": ((0x0400, 0x04FF),),
    "uk": ((0x0400, 0x04FF),),
    "ar": ((0x0600, 0x06FF),),
    "fa": ((0x0600, 0x06FF),),
    "he": ((0x0590, 0x05FF),),
    "th": ((0x0E00, 0x0E7F),),
    "hi": ((0x0900, 0x097F),),
}


def text_looks_like_lang(text: str, lang: str) -> bool:
    """粗略判断文本「看起来已经是某种语言」（lang 字段缺失时的兜底）。

    只覆盖有明确字符区间的语言（中文 / 日文 / 韩文 / 俄文等），其它语言一律返回
    False（宁可不判断，也不误判）。判定标准：该语言特征字符占有意义字符的比例
    不低于 30%，且有意义字符本身不少于 4 个。
    """
    if not text:
        return False
    main = normalize_translate_lang(lang).split("-")[0]
    ranges = _LANG_CHAR_RANGES.get(main)
    if not ranges:
        return False
    meaningful = _MEANINGFUL_CHAR_RE.findall(text)
    if len(meaningful) < 4:
        return False
    hits = 0
    for char in meaningful:
        code = ord(char)
        if any(low <= code <= high for low, high in ranges):
            hits += 1
    return hits / len(meaningful) >= 0.3


def already_in_target_lang(data: "TweetData", target_lang: str) -> bool:
    """判断推文正文是否已经是目标语言（不需要翻译）。

    优先用接口给的 lang 字段；lang 缺失时退化为字符集粗略判断，
    避免把「本来就是中文」的推文误报成「翻译不可用」。
    """
    if not target_lang:
        return False
    if not data.needs_translation(target_lang):
        return True
    if not (data.lang or "").strip():
        return text_looks_like_lang(data.text, target_lang)
    return False

# 当前生效的目标语言（会话级覆盖）。/xt 翻译 只影响当前会话，用 ContextVar 保存，
# 这样并发处理多个群的消息时不会互相串味；为空表示使用插件配置里的默认值。
_ACTIVE_TRANSLATE_LANG: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "x_tweet_translate_lang", default=""
)

# 默认 User-Agent。FxTwitter 要求请求必须携带 UA，否则直接返回 401。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 匹配 x.com / twitter.com 域名（含 mobile.、www. 等子域）以及 fx/vx 系列镜像域
_TWEET_HOSTS = (
    r"(?:mobile\.|www\.|m\.)?(?:x\.com|twitter\.com)"
    r"|(?:www\.)?(?:fxtwitter\.com|vxtwitter\.com|fixupx\.com|nitter\.[a-z0-9.-]+)"
)
# /i/status/{id} 与 /{screen_name}/status/{id} 两种形态。
# 说明：screen_name 段用 (?!i/) 排除掉 /i/ 这条特殊路径，否则 i 会被误当成用户名。
_STATUS_RE = re.compile(
    r"https?://(?:"
    + _TWEET_HOSTS
    + r")/(?:([A-Za-z0-9_]{1,20})/)?(?:i/)?status(?:es)?/(\d{5,25})",
    re.IGNORECASE,
)
# 裸推文数字 ID（仅在 LLM 工具入参里使用）
_BARE_ID_RE = re.compile(r"^\d{5,25}$")
# t.co 短链
_TCO_RE = re.compile(r"https?://t\.co/[A-Za-z0-9_-]{4,30}", re.IGNORECASE)
_ANY_URL_RE = re.compile(r"https?://[^\s\u200b]+", re.IGNORECASE)

# 个人主页链接：https://x.com/{handle}（允许结尾带 "/"、"?" 或 /media 这类标签页，
# /status/123 形式的推文链接由 extract_profile_handles 里的 _STATUS_RE 单独排除）
_PROFILE_URL_RE = re.compile(
    r"https?://(?:" + _TWEET_HOSTS + r")/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
# X 上不是「用户主页」的保留路径，避免把 /i/lists/... 之类当成账号
_PROFILE_RESERVED = {
    "i",
    "home",
    "explore",
    "notifications",
    "messages",
    "settings",
    "search",
    "compose",
    "intent",
    "share",
    "hashtag",
    "login",
    "logout",
    "signup",
    "tos",
    "privacy",
    "about",
    "jobs",
    "help",
    "download",
    "account",
    "following",
    "followers",
    "lists",
    "bookmarks",
    "communities",
    "topics",
    "media",
    "with_replies",
    "status",
    "statuses",
    "tweet",
    "twitter",
    "x",
}
# 触发正则里用于排除保留路径的分支（如 x.com/home、x.com/i/lists/...）
_RESERVED_ALT = "|".join(sorted(_PROFILE_RESERVED))

# 自动解析 Handler 的触发正则：只有当消息里真的出现推文链接（或 t.co 短链、
# 或 X 个人主页链接）时，本 handler 才会被激活。AstrBot 的唤醒检查会把任何
# filter 通过的 handler 记入 activated_handlers 并令 is_wake=True，若这里不加
# 链接判断，普通聊天消息也会被本插件"唤醒"，从而绕过 wake_prefix 的静默语义
# （虽然 process_stage 只在 is_at_or_wake_command 为真时才请求 LLM，但没必要让
# 每条消息都走完后续管道）。
_TRIGGER_RE = re.compile(
    r"https?://(?:"
    + _TWEET_HOSTS
    + r")/(?:([A-Za-z0-9_]{1,20})/)?(?:i/)?status(?:es)?/(\d{5,25})"
    r"|https?://t\.co/[A-Za-z0-9_-]{4,30}"
    r"|https?://(?:"
    + _TWEET_HOSTS
    + r")/(?!(?:"
    + _RESERVED_ALT
    + r")(?![A-Za-z0-9_]))[A-Za-z0-9_]{1,15}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# 文本与正文长度限制，避免超长推文刷屏
MAX_TEXT_LEN = 3000
MAX_DESC_LEN = 4000

# 判断正文「有效内容」长度时先剔除这些东西
_URL_ONLY_RE = re.compile(r"https?://\S+")
_MENTION_ONLY_RE = re.compile(r"@[A-Za-z0-9_]{1,15}")
# 只统计真正的文字：拉丁字母、数字、中日韩字符（表情 / 标点 / 空格都不算）
_MEANINGFUL_CHAR_RE = re.compile(
    r"[0-9A-Za-z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


# --------------------------------------------------------------------------- #
# 二、基础工具函数（纯函数，便于单独测试）
# --------------------------------------------------------------------------- #


def get_astrbot_data_path() -> Path:
    """获取 AstrBot 的 data 目录，兼容不同版本的导入路径。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path as _get

        return Path(_get())
    except Exception:
        pass

    try:  # 兜底：通过已导入的 astrbot 包位置推断
        import astrbot as _astrbot_pkg

        return Path(_astrbot_pkg.__file__).resolve().parent.parent / "data"
    except Exception:
        pass

    return Path.home() / ".astrbot" / "data"


def get_default_cache_dir() -> Path:
    """默认缓存目录（AstrBot 规范位置：data/plugin_data/<插件名>/cache）。

    优先用 AstrBot 的 StarTools.get_data_dir(插件名)，它正是官方约定的插件数据目录；
    取不到时退化为 data/plugin_data/<插件名>/cache，再不行才落到 home 下的 .astrbot。
    """
    try:
        from astrbot.api.star import StarTools

        base = Path(StarTools.get_data_dir(PLUGIN_NAME))
        if str(base) not in ("", "."):
            return base / "cache"
    except Exception:
        pass
    try:
        return get_astrbot_data_path() / "plugin_data" / PLUGIN_NAME / "cache"
    except Exception:
        pass
    return Path.home() / ".astrbot" / "data" / "plugin_data" / PLUGIN_NAME / "cache"


def extract_tweet_ids(text: str) -> list[str]:
    """从任意文本中提取推文 ID（保持出现顺序、去重）。"""
    return [item[0] for item in extract_tweet_refs(text)]


def extract_tweet_refs(text: str) -> list[tuple[str, str]]:
    """从任意文本中提取 (tweet_id, screen_name) 列表，保持出现顺序并去重。

    screen_name 可能为空字符串（形如 https://x.com/i/status/123 的链接）。
    URL 里带的用户名只是“线索”，真实作者仍以接口返回为准，因此不匹配时不会报错。
    """
    if not text:
        return []

    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _STATUS_RE.finditer(text):
        screen_name = (match.group(1) or "").strip()
        tweet_id = match.group(2)
        # /i/status/... 会被前面的可选组吃掉一个 "i"，这里做一次纠正
        if screen_name.lower() == "i":
            screen_name = ""
        if tweet_id not in seen:
            seen.add(tweet_id)
            refs.append((tweet_id, screen_name))

    # 兜底：形如 https://x.com/i/status/123 的宽松匹配
    if not refs:
        for match in re.finditer(
            r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{0,20})/?[^\s\u200b]*?status(?:es)?/(\d{5,25})",
            text,
            re.IGNORECASE,
        ):
            screen_name = (match.group(1) or "").strip()
            tweet_id = match.group(2)
            if screen_name.lower() == "i":
                screen_name = ""
            if tweet_id not in seen:
                seen.add(tweet_id)
                refs.append((tweet_id, screen_name))

    return refs


def extract_profile_handles(text: str) -> list[str]:
    """从任意文本中提取 X 个人主页的账号名（保持出现顺序、去重）。

    只有形如 https://x.com/{handle} 且后面不接路径的链接才算主页；
    /status/ 链接与 /i/lists/... 这类系统路径都会被排除。
    """
    if not text:
        return []

    handles: list[str] = []
    seen: set[str] = set()
    for match in _PROFILE_URL_RE.finditer(text):
        handle = (match.group(1) or "").strip()
        if not handle or handle.lower() in _PROFILE_RESERVED:
            continue
        # 整个 URL 若是推文链接（.../handle/status/123），说明这是推文而不是主页
        token = _ANY_URL_RE.match(text[match.start() :])
        if token and _STATUS_RE.search(token.group(0)):
            continue
        key = handle.lower()
        if key not in seen:
            seen.add(key)
            handles.append(handle)
    return handles


def normalize_handle(raw: str) -> str:
    """把 @name / 主页链接 / 裸账号统一成账号名；无法识别时返回空字符串。

    这里只做格式归一化，不校验账号是否真实存在（是否存在交给接口 404 判断）。
    """
    if not raw:
        return ""
    candidate = str(raw).strip()
    if not candidate:
        return ""

    for handle in extract_profile_handles(candidate):
        return handle

    candidate = candidate.lstrip("@").strip()
    # 允许 "x.com/name"、"twitter.com/name" 这类省略协议的写法
    candidate = re.sub(
        r"^(?:https?://)?(?:mobile\.|www\.|m\.)?(?:x\.com|twitter\.com)/",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = candidate.split("?")[0].split("#")[0].strip().strip("/")
    if "/" in candidate:  # 仍带路径，说明不是主页链接
        return ""
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", candidate):
        if candidate.lower() in _PROFILE_RESERVED:
            return ""
        return candidate
    return ""


_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["'][^"']*?url=([^"'>\s]+)""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_JS_REDIRECT_RE = re.compile(
    r"""(?:location(?:\.href)?\s*=\s*|location\.replace\s*\(\s*)["']([^"']+)["']""",
    re.IGNORECASE,
)
_URL_LIKE_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.IGNORECASE)


def _unescape_js_url(raw: str) -> str:
    """还原 JS 字符串里的转义斜杠与 HTML 实体，得到干净 URL。"""
    if not raw:
        return ""
    text = raw.strip()
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    text = text.replace("\\", "")
    text = html.unescape(text)
    return text.strip().strip("\"'")


def _extract_client_redirect(body: str) -> str:
    """
    从 t.co 这类「HTTP 200 + 客户端跳转」的 HTML 里提取真实目标地址。

    依次尝试：<noscript> meta refresh、<title>、location.replace/"location.href"。
    都没有时返回空串。
    """
    if not body:
        return ""

    candidates: list[str] = []

    match = _META_REFRESH_RE.search(body)
    if match:
        candidates.append(match.group(1))

    match = _TITLE_RE.search(body)
    if match:
        candidates.append(match.group(1))

    for match in _JS_REDIRECT_RE.finditer(body):
        candidates.append(match.group(1))

    for candidate in candidates:
        url = _unescape_js_url(candidate)
        if _URL_LIKE_RE.match(url):
            return url
    return ""


def normalize_tweet_ref(raw: str) -> Optional[str]:
    """把用户/LLM 传入的推文引用（链接或纯数字 ID）归一化为推文 ID。"""
    if not raw:
        return None

    raw = raw.strip().strip("<>\"'").strip()
    if not raw:
        return None

    if _BARE_ID_RE.match(raw):
        return raw

    ids = extract_tweet_ids(raw)
    return ids[0] if ids else None


def strip_url_query(url: str) -> str:
    """移除 URL 中的查询参数与锚点，保证同一推文缓存键稳定。"""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return url.split("?", 1)[0].split("#", 1)[0]


def stable_digest(source: str, length: int = 12) -> str:
    """生成稳定的短摘要，用于缓存文件名。

    不能使用内置 hash()：Python 的字符串哈希每个进程都会加随机盐（PYTHONHASHSEED），
    重启后同一 URL 会算出不同的文件名，导致旧缓存永远命中不到、缓存目录不断膨胀。
    """
    digest = hashlib.blake2s(str(source).encode("utf-8"), digest_size=8).hexdigest()
    return digest[: max(1, length)]


def build_tweet_url(screen_name: Optional[str], tweet_id: str) -> str:
    """构造标准的推文链接。"""
    if screen_name:
        return f"https://x.com/{screen_name}/status/{tweet_id}"
    return f"https://x.com/i/status/{tweet_id}"


def truncate_text(text: str, limit: int = MAX_TEXT_LEN) -> str:
    """截断过长文本，并追加省略提示。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……（内容过长已截断）"


def _to_int(value: Any) -> Optional[int]:
    """把接口返回的数字（可能是字符串 / "None" / 空串）转成 int，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text or text.lower() in ("none", "null", "-"):
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


_SYNDICATION_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _int_to_base36(value: int) -> str:
    """非负整数转 36 进制字符串。"""
    if value <= 0:
        return "0"
    digits: list[str] = []
    while value:
        value, rem = divmod(value, 36)
        digits.append(_SYNDICATION_BASE36[rem])
    return "".join(reversed(digits))


def generate_syndication_token(tweet_id: str) -> str:
    """复刻 JS 算法 (Number(id)/1e15*Math.PI).toString(36).replace(/(0+|\\.)/g,"")。

    生成 Syndication CDN 的请求 token。推文 ID 是 19 位整数，JS Number() 与
    Python float() 都按 IEEE-754 double 舍入，因此两边算法结果一致。token 仅
    作为请求参数，不参与业务逻辑；生成失败时返回空串（调用方回退 token=x）。
    """
    try:
        numeric = float(int(str(tweet_id).strip()))
    except (TypeError, ValueError):
        return ""
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return ""
    value = (numeric / 1e15) * math.pi
    sign = "-" if value < 0 else ""
    value = abs(value)
    integer_part = int(value)
    fraction = value - integer_part
    token = sign + _int_to_base36(integer_part)
    if fraction > 0:
        digits: list[str] = []
        for _ in range(30):
            fraction *= 36
            digit = int(fraction)
            digits.append(_SYNDICATION_BASE36[digit])
            fraction -= digit
            if fraction <= 0:
                break
        token += "." + "".join(digits)
    return re.sub(r"0+|\.", "", token)


def parse_twitter_time(raw: Any) -> Optional[int]:
    """解析 Twitter 的 created_at 格式（"Wed Oct 12 12:00:00 +0000 2022"）为时间戳。

    Syndication 等旧格式接口使用该时间表示；解析失败返回 None。
    """
    if not raw:
        return None
    try:
        parsed = datetime.strptime(str(raw).strip(), "%a %b %d %H:%M:%S %z %Y")
    except Exception:
        return None
    return int(parsed.timestamp())


def _truthy(value: Any) -> bool:
    """接口里的布尔值常常是字符串 "True"/"False"，这里统一判断。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _profile_avatar_url(url: str) -> str:
    """把 X 头像的 _normal 小图换成分辨率更高的 _400x400（失败时原样返回）。"""
    if not url:
        return ""
    return url.replace("_normal.", "_400x400.") if "_normal." in url else url


# FxTwitter 用字符串 "None" / "False" 表示缺失的可选字段（如 website），
# 直接拼进文案会变成「🔗 None」，这里统一按空串处理。
_PLACEHOLDER_STRINGS = frozenset({"", "none", "null", "false", "nil", "undefined"})


def _clean_str(value: Any) -> str:
    """把接口返回的可选字符串规范化：占位符（"None"/"False"/""）一律当空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value).strip()
    text = value.strip()
    return "" if text.lower() in _PLACEHOLDER_STRINGS else text


def escape_zero_width(text: str) -> str:
    """
    在文本首尾添加零宽空格。

    aiocqhttp 适配器在发送 plain 消息段时会 strip() 掉首尾空白，
    添加零宽空格可以保留用户期望的换行与缩进。
    """
    if not text:
        return ""
    return "\u200b" + text + "\u200b"


def format_relative_time(timestamp: Optional[int]) -> str:
    """把 UNIX 时间戳格式化为相对时间描述。"""
    if not timestamp:
        return ""
    try:
        created = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except Exception:
        return ""

    delta = datetime.now(timezone.utc) - created
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return created.strftime("%Y-%m-%d %H:%M")
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 86400 * 30:
        return f"{seconds // 86400} 天前"
    return created.strftime("%Y-%m-%d %H:%M")


def format_join_date(raw: Any) -> str:
    """把主页的「加入时间」格式化成 X 的样式（如「2010年3月」）。

    接口给的是 ISO 时间串（2010-03-01T00:00:00.000Z），直接显示会很难看；
    解析不出来时返回空串，由调用方决定是否跳过这一项。
    """
    text = _clean_str(raw)
    if not text:
        return ""
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        except Exception:
            parsed = None
    if parsed is None:
        return ""
    return f"{parsed.year}年{parsed.month}月"


def status_is_repost(status: dict[str, Any], handle: str = "") -> bool:
    """时间线里的一条 status 是不是「转帖」。

    FxTwitter 的 /2/profile/{handle}/statuses 会把该账号的转帖一起返回（接口没有
    排除转帖的参数）：转帖的 author 是**原作者**，reposted_by 才是被查询的账号。
    不认出来就会出现「解析 A 的主页却显示 B 的推文」。
    """
    if not isinstance(status, dict):
        return False
    if status.get("reposted_by"):
        return True
    if handle:
        author = (status.get("author") or {}).get("screen_name") or ""
        if author and author.lstrip("@").lower() != handle.lstrip("@").lower():
            return True
    return False


def format_count(value: Optional[int]) -> str:
    """格式化数字：过亿用「亿」、过万用「万」，并去掉无意义的小数位。"""
    if value is None:
        return "0"
    try:
        value = int(value)
    except Exception:
        return str(value)
    if value >= 100000000:
        return f"{value / 100000000:.1f}亿".replace(".0亿", "亿")
    if value >= 10000:
        return f"{value / 10000:.1f}万".replace(".0万", "万")
    return str(value)


def normalize_translate_lang(raw: str) -> str:
    """把用户填写的目标语言归一化为 FxTwitter 能识别的语言代码。

    兼容 zh_CN / ZH-CN / 中文 / 中文（简体） 等写法，未知取值原样返回小写形式，
    由接口侧决定是否支持，不会导致报错。
    """
    value = (raw or "").strip()
    if not value:
        return DEFAULT_TRANSLATE_LANG

    lowered = value.lower().replace(" ", "")
    if lowered in _TRANSLATE_LANG_ALIASES:
        return _TRANSLATE_LANG_ALIASES[lowered]
    if lowered in TRANSLATE_LANG_CHOICES:
        return lowered

    # 形如 中文（简体）/ 简体中文(zh) 的写法，取出括号前的部分再试一次
    simplified = re.split(r"[（(]", value, maxsplit=1)[0].strip().lower()
    if simplified in _TRANSLATE_LANG_ALIASES:
        return _TRANSLATE_LANG_ALIASES[simplified]
    if simplified in TRANSLATE_LANG_CHOICES:
        return simplified
    return lowered


def meaningful_text_length(text: str) -> int:
    """统计正文里「真正有内容」的字符数（去掉链接、@提及与表情）。

    用来判断一条推文值不值得调用大模型翻译：像「@某人 ❤️」「😝😝」这类正文，
    去掉提及和表情后什么都不剩，交给大模型纯属浪费额度。
    """
    if not text:
        return 0
    cleaned = _URL_ONLY_RE.sub(" ", text)
    cleaned = _MENTION_ONLY_RE.sub(" ", cleaned)
    return len(_MEANINGFUL_CHAR_RE.findall(cleaned))


def apply_image_size(url: str, image_size: str) -> str:
    """给 pbs.twimg.com 的图片地址套上尺寸参数（?name=orig/medium/...）。

    非 twimg 的图片地址（例如外链头像或第三方图床）原样返回，避免破坏 URL。
    """
    if not url:
        return url

    size = IMAGE_SIZE_CHOICES.get((image_size or "").strip().lower())
    if size is None:
        return url

    name = size[0]
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return url

    if "twimg.com" not in (parts.netloc or "").lower():
        return url

    # Twitter 图片地址统一走 name 参数控制尺寸；先剔除可能已存在的同名参数
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k.lower() != "name"]
    query.append(("name", name))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def image_size_display(image_size: str) -> str:
    """把尺寸档位转换为面板 / 状态输出里的中文说明。"""
    size = IMAGE_SIZE_CHOICES.get((image_size or "").strip().lower())
    return size[1] if size else f"未知（{image_size}）"


def build_translate_prompt(text: str, target_lang: str, source_lang: str = "") -> str:
    """构造大模型翻译用的提示词（仅在 FxTwitter 翻译不可用时使用）。"""
    lang_name = TRANSLATE_LANG_CHOICES.get(target_lang, target_lang)
    source = f"（源语言代码：{source_lang}）" if source_lang else ""
    return (
        f"把下面这条推文翻译成{lang_name}{source}。要求：\n"
        "1. 只输出译文本身，不要任何解释、前缀、引号或 Markdown 代码块；\n"
        "2. 保留原文的语气、换行与话题标签（#）、提及（@）和链接；\n"
        "3. 如果原文已经是目标语言，原样输出。\n\n"
        f"{text}"
    )


# --------------------------------------------------------------------------- #
# 三、数据结构 TweetData
# --------------------------------------------------------------------------- #


@dataclass
class TweetData:
    """归一化后的推文数据。"""

    tweet_id: str
    url: str = ""
    text: str = ""
    author_name: str = ""
    author_screen_name: str = ""
    author_avatar: str = ""
    # 作者是否有认证（蓝V）。FxTwitter 读 author.verification，Syndication 读 user.is_blue_verified
    author_verified: bool = False
    # 认证类型：individual（蓝，个人）/ business（金，商家组织）/ government（灰，政府官方）；
    # 只有 FxTwitter 源能给出类型，其它源为空串（按蓝徽章处理）
    author_verified_type: str = ""
    created_timestamp: Optional[int] = None
    likes: Optional[int] = None
    retweets: Optional[int] = None
    replies: Optional[int] = None
    views: Optional[int] = None
    lang: str = ""
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    video_thumbnails: list[str] = field(default_factory=list)
    quote_url: str = ""
    quote_text: str = ""
    # ---- 引用推文（主贴）的独立字段：媒体与译文都和本推文分开存放 ----
    # 这样「回复/引用贴」与「主贴」的图片、视频不会混在一起无法区分
    quote_id: str = ""
    quote_images: list[str] = field(default_factory=list)
    quote_videos: list[str] = field(default_factory=list)
    quote_video_thumbnails: list[str] = field(default_factory=list)
    quote_translation_text: str = ""
    quote_translation_lang: str = ""
    quote_translation_provider: str = ""
    # ---- 被回复的推文（replying_to）：FxTwitter 只给 ID 与作者，正文需要二次解析 ----
    reply_to_id: str = ""
    reply_to_author: str = ""
    reply_to_url: str = ""
    is_note_tweet: bool = False
    # ---- 翻译相关（三个字段都可为空，旧版缓存反序列化后即为空，不会报错） ----
    translation_text: str = ""       # 译文正文
    translation_lang: str = ""       # 目标语言代码，如 zh-cn
    translation_provider: str = ""   # 译文来源：fxtwitter(grok) / 大模型名
    translation_source_lang: str = ""  # 接口识别出的源语言
    source: str = ""
    fetched_at: float = 0.0
    error: str = ""
    # 楼中楼里命中解析黑名单的楼层：只发占位说明，正文与媒体都不发
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TweetData":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.tweet_id)

    @property
    def translated(self) -> bool:
        """是否带有可用的译文。"""
        return bool(self.translation_text)

    def needs_translation(self, target_lang: str) -> bool:
        """判断是否需要翻译：原文语言与目标语言不同才翻译。

        语言代码可能带地区后缀（zh-cn / zh-tw / pt-br），因此只比较主语言码。
        """
        if not self.text:
            return False
        source = (self.lang or "").strip().lower()
        target = normalize_translate_lang(target_lang)
        if not source or not target:
            return True
        return source.split("-")[0] != target.split("-")[0]

    @property
    def author_display(self) -> str:
        if self.author_name and self.author_screen_name:
            return f"{self.author_name} (@{self.author_screen_name})"
        return self.author_name or (
            f"@{self.author_screen_name}" if self.author_screen_name else "未知作者"
        )

    @property
    def media_urls(self) -> list[str]:
        return [*self.images, *self.videos]

    @property
    def parent_tweet_id(self) -> str:
        """引用/回复的主贴 ID（用于二次解析后单独发送）。"""
        return self.quote_id or self.reply_to_id

    @property
    def parent_kind(self) -> str:
        """主贴与本推文的关系：quote（引用）/ reply（回复）/ 空字符串。"""
        if self.quote_id:
            return "quote"
        if self.reply_to_id:
            return "reply"
        return ""

    @property
    def quote_media_urls(self) -> list[str]:
        """引用推文（主贴）携带的媒体链接。"""
        return [*self.quote_images, *self.quote_videos]

    @property
    def has_any_media(self) -> bool:
        """本推文或它引用的主贴是否带媒体。"""
        return bool(self.media_urls or self.quote_media_urls)


@dataclass
class ProfileData:
    """归一化后的 X 个人主页数据（/2/profile/{handle}）。"""

    handle: str
    url: str = ""
    name: str = ""
    description: str = ""
    avatar: str = ""
    banner: str = ""
    location: str = ""
    website: str = ""
    joined: str = ""
    followers: Optional[int] = None
    following: Optional[int] = None
    statuses: Optional[int] = None
    likes: Optional[int] = None
    media_count: Optional[int] = None
    verified: bool = False
    protected: bool = False
    source: str = ""
    fetched_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileData":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.handle)

    @property
    def display_name(self) -> str:
        if self.name and self.handle:
            return f"{self.name} (@{self.handle})"
        return self.name or (f"@{self.handle}" if self.handle else "未知账号")




# 动态导出：`from .x_tweet_core import *` 要连 _ 前缀的常量、正则、私有工具函数一起带出去，
# 因此不能依赖 Python「import * 跳过下划线名字」的默认行为。
# ---- 个人主页卡片 ----
# 主页专属小图标（位置 / 链接 / 日历），同样不用 emoji
_X_SVG_SM = 'class="xicon-sm" viewBox="0 0 24 24" aria-hidden="true"'
X_ICON_LOCATION = (
    f"<svg {_X_SVG_SM}>"
    '<path d="M12 7c-1.93 0-3.5 1.57-3.5 3.5S10.07 14 12 14s3.5-1.57 3.5-3.5S13.93 7 12 7zm0 5'
    'c-.827 0-1.5-.673-1.5-1.5S11.173 9 12 9s1.5.673 1.5 1.5S12.827 12 12 12zm0-10c-4.687 0-8.5 3.813-8.5 8.5'
    ' 0 5.967 7.621 11.116 7.945 11.332l.555.37.555-.37c.324-.216 7.945-5.365 7.945-11.332C20.5 5.813 16.687 2 12 2z'
    'm0 17.77c-1.665-1.241-6.5-5.196-6.5-9.27C5.5 6.916 8.416 4 12 4s6.5 2.916 6.5 6.5c0 4.073-4.835 8.028-6.5 9.27z"/>'
    "</svg>"
)
X_ICON_LINK = (
    f"<svg {_X_SVG_SM}>"
    '<path d="M18.36 5.64c-1.95-1.96-5.11-1.96-7.07 0L9.88 7.05 8.46 5.64l1.42-1.42'
    'c2.73-2.73 7.16-2.73 9.9 0 2.73 2.74 2.73 7.17 0 9.9l-1.42 1.42-1.41-1.42 1.41-1.41'
    'c1.96-1.96 1.96-5.12 0-7.07zm-2.12 3.53l-7.07 7.07-1.41-1.41 7.07-7.07 1.41 1.41z'
    'm-12.02.71l1.42-1.42 1.41 1.42-1.41 1.41c-1.96 1.96-1.96 5.12 0 7.07 1.95 1.96 5.11 1.96 7.07 0'
    'l1.41-1.41 1.42 1.41-1.42 1.42c-2.73 2.73-7.16 2.73-9.9 0-2.73-2.74-2.73-7.17 0-9.9z"/>'
    "</svg>"
)
X_ICON_CALENDAR = (
    f"<svg {_X_SVG_SM}>"
    '<path d="M7 4V3h2v1h6V3h2v1h1.5C19.88 4 21 5.12 21 6.5v13c0 1.38-1.12 2.5-2.5 2.5h-13'
    'C4.12 22 3 20.88 3 19.5v-13C3 5.12 4.12 4 5.5 4H7zm13 6H4v9.5c0 .28.22.5.5.5h13c.28 0 .5-.22.5-.5V10z'
    'M5.5 6c-.28 0-.5.22-.5.5V9h14V6.5c0-.28-.22-.5-.5-.5h-13z"/>'
    "</svg>"
)
# 元信息图标按 kind 选择
_PROFILE_META_ICONS = (
    '{% if m.kind == "location" %}'
    + X_ICON_LOCATION
    + '{% elif m.kind == "link" %}'
    + X_ICON_LINK
    + '{% else %}'
    + X_ICON_CALENDAR
    + "{% endif %}"
)

# 个人主页卡片：横幅 + 大头像 + 昵称 / 简介 / 元信息 / 关注数 + 最近推文（复用推文条目样式）
PROFILE_CARD_TMPL = (
    X_CARD_STYLE
    + """\
<style>
  .xpbanner { width: 100%; height: 240px; overflow: hidden; background: {{ theme.border }}; }
  .xpbanner img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .xprow { display: flex; align-items: flex-end; justify-content: space-between; padding: 0 16px; }
  .xpavatar { width: 120px; height: 120px; border-radius: 50%; border: 4px solid {{ theme.bg }};
    object-fit: cover; margin-top: -60px; flex: none; }
  .xpavatar-ph { display: flex; align-items: center; justify-content: center;
    background: {{ theme.border_strong }}; color: {{ theme.bg }}; font-size: 48px; font-weight: 700; }
  .xpactions { display: flex; align-items: center; padding-bottom: 12px; }
  .xpbtn { border: 1px solid {{ theme.border_strong }}; border-radius: 999px; padding: 7px 20px;
    font-size: 15px; font-weight: 700; color: {{ theme.text }}; }
  .xpname { margin: 12px 16px 0; font-size: 22px; font-weight: 700; }
  .xpname .xbadge { width: 22px; height: 22px; vertical-align: -3px; margin-left: 4px; }
  .xphandle { margin: 2px 16px 0; color: {{ theme.muted }}; font-size: 15px; }
  .xpdesc { margin: 12px 16px 0; font-size: 16px; line-height: 1.5; white-space: pre-wrap;
    word-break: break-word; }
  .xpmeta { display: flex; flex-wrap: wrap; gap: 16px; margin: 12px 16px 0;
    color: {{ theme.muted }}; font-size: 14px; }
  .xpm { display: inline-flex; align-items: center; gap: 5px; }
  .xicon-sm { width: 17px; height: 17px; fill: {{ theme.muted }}; flex: none; }
  .xpstats { margin: 12px 16px 0; font-size: 15px; color: {{ theme.muted }}; }
  .xpstats b { color: {{ theme.text }}; }
  .xptabs { display: flex; gap: 56px; margin: 16px 16px 0; padding-bottom: 12px;
    border-bottom: 1px solid {{ theme.border }}; font-size: 15px; font-weight: 700; }
  .xptab-off { color: {{ theme.muted }}; font-weight: 400; }
  .xppost { display: flex; gap: 12px; padding: 16px 16px 0; }
  .xppost:last-of-type { padding-bottom: 20px; }
  .xppost .xstats { margin-top: 10px; }
</style>
<div class="xcard">
  <div class="xpbanner">{% if banner %}<img src="{{ banner }}">{% endif %}</div>
  <div class="xprow">
    {% if avatar %}
    <img class="xpavatar" src="{{ avatar }}">
    {% else %}
    <div class="xpavatar xpavatar-ph">{{ initial }}</div>
    {% endif %}
    <div class="xpactions"><span class="xpbtn">关注</span></div>
  </div>
  <div class="xpname">{{ name }}{% if verified %}"""
    + X_BADGE_BLUE
    + """{% endif %}</div>
  <div class="xphandle">@{{ handle }}</div>
  {% if description %}<div class="xpdesc">{{ description }}</div>{% endif %}
  {% if meta %}
  <div class="xpmeta">{% for m in meta %}<span class="xpm">"""
    + _PROFILE_META_ICONS
    + """<span>{{ m.text }}</span></span>{% endfor %}</div>
  {% endif %}
  <div class="xpstats"><b>{{ following }}</b> 正在关注&nbsp;&nbsp;&nbsp;<b>{{ followers }}</b> 关注者</div>
  {% if posts %}
  <div class="xptabs">
    <span>帖子</span><span class="xptab-off">回复</span>
    <span class="xptab-off">转帖</span><span class="xptab-off">媒体</span>
  </div>
  {% for item in posts %}
  <div class="xppost">
    <div class="xavatar-col">
      {% if item.avatar %}
      <img class="xavatar" src="{{ item.avatar }}">
      {% else %}
      <div class="xavatar xavatar-ph">{{ item.initial }}</div>
      {% endif %}
    </div>
    <div class="xmain" style="padding-bottom:0">
      <div class="xnameline">
        <span class="xname">{{ item.name }}</span>
        <span class="xhandle">@{{ item.handle }}</span>
        <span class="xdate">· {{ item.date }}</span>
      </div>
      <div class="xbody">
        {% if item.translation %}<div class="xtrans">{{ item.translation }}</div>{% endif %}
        {% if item.text %}<div class="xtext">{{ item.text }}</div>{% endif %}
        {% if not item.translation and not item.text %}<div class="xtext">（本条推文没有文字内容）</div>{% endif %}
      </div>
      {% if item.media_hint %}<div class="xmedia">{{ item.media_hint }}</div>{% endif %}
      {% if item.stats %}
      <div class="xstats">{% for s in item.stats %}<span class="xstat">"""
    + _STAT_ICONS
    + """<span>{{ s.value }}</span></span>{% endfor %}</div>
      {% endif %}
    </div>
  </div>
  {% endfor %}
  {% endif %}
</div>
"""
)

__all__ = [name for name in list(globals()) if not name.startswith("__")]
