# -*- coding: utf-8 -*-
"""
astrbot_plugin_x_tweet —— 媒体下载层

包含 MediaDownloader：图片走 httpx 流式下载（带体积上限），视频优先直链、否则调用本地 yt-dlp，
以及缓存文件的落盘与清理。依赖 x_tweet_core，不依赖插件主体。

作者：Leisurelyun
版本：v1.14.0
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
# 六、媒体下载 MediaDownloader（httpx 流式下载 + 本地 yt-dlp）
# --------------------------------------------------------------------------- #


class _MediaTooLargeError(Exception):
    """媒体体积超过配置上限（内部信号，用于中断流式下载并清理临时文件）。"""


class MediaDownloader:
    """
    媒体下载器。

    - 图片：通过 httpx 流式下载到本地缓存目录，超过大小上限时放弃本地文件并回退为链接。
    - 视频：优先使用解析源返回的 MP4 直链；没有直链时通过
      asyncio.create_subprocess_exec 调用本地 yt-dlp 下载。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cache_dir: Path,
        max_image_size: int,
        max_video_size: int,
        timeout: float = 20.0,
        download_timeout: float = 180.0,
        user_agent: str = DEFAULT_USER_AGENT,
        yt_dlp_path: str = "",
        cookie_file: str = "",
        proxy: str = "",
        cache_max_files: int = 300,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.max_image_size = max(1, max_image_size)
        self.max_video_size = max(1, max_video_size)
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.user_agent = user_agent
        self.yt_dlp_path = yt_dlp_path.strip()
        self.cookie_file = cookie_file.strip()
        self.proxy = proxy.strip()
        self.cache_max_files = max(0, cache_max_files)

        # 正在下载中的文件，避免并发重复下载（用弱引用，锁用完即可被回收，表不会无限增长）
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # yt-dlp 可用性缓存：None 表示尚未检测
        self._yt_dlp_cmd: Optional[list[str]] = None

        self.ensure_cache_dir()

    def ensure_cache_dir(self) -> bool:
        """确保缓存目录存在，失败时只记日志并返回 False（不让插件加载失败）。"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as exc:
            logger.error(
                f"[{PLUGIN_NAME}] 缓存目录不可用（{self.cache_dir}）：{exc}。"
                f"图片、视频与解析缓存将无法落盘，解析结果会降级为原链接。"
            )
            return False

    async def close(self) -> None:
        """释放下载器资源（插件卸载 / 重载时调用）。

        注意：HTTP 客户端是插件创建、与 XTweetParser 共用的同一个实例，
        所以这里不 aclose（避免把别的组件还在用的连接池关掉），真正的关闭
        由插件 terminate() 通过 parser.close() 完成一次。
        """
        self._locks.clear()

    # ---------------------------- 缓存管理 ---------------------------- #

    def build_cache_path(
        self, tweet_id: str, url: str, suffix: str = "", variant: str = ""
    ) -> Path:
        """为媒体 URL 生成稳定的缓存文件路径。

        variant 用于区分同一张图片的不同尺寸（orig / medium / ...）：带上 variant 后
        会生成不同的缓存文件，切换尺寸配置后不会误用旧尺寸的缓存。
        """
        clean = strip_url_query(url)
        name = Path(urllib.parse.urlsplit(clean).path).name or "media"
        # 过滤非法字符（Windows 下尤其重要）
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
        if not name or name in (".", ".."):
            name = "media"

        if not suffix:
            suffix = Path(name).suffix
        variant_tag = re.sub(r"[^A-Za-z0-9_-]", "", variant or "")[:16]
        digest_src = f"{clean}|{variant_tag}" if variant_tag else clean
        digest = f"{tweet_id}_{stable_digest(digest_src)}"
        return self.cache_dir / f"{tweet_id}_{digest}_{name}"[:200].lstrip()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def cleanup_cache(self) -> int:
        """按最久未修改顺序清理媒体缓存文件，返回删除数量。

        只统计媒体文件（图片 / 视频）；推文解析的 *.json 缓存由 DATA_CACHE_TTL_DAYS
        的过期清理负责，避免两类文件互相挤占配额。
        """
        if self.cache_max_files <= 0:
            return 0

        try:
            files = [
                p
                for p in self.cache_dir.iterdir()
                if p.is_file() and p.suffix.lower() != ".json"
            ]
        except Exception as exc:
            logger.debug(f"[{PLUGIN_NAME}] 读取缓存目录失败: {exc}")
            return 0

        if len(files) <= self.cache_max_files:
            return 0

        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        removed = 0
        for path in files[: len(files) - self.cache_max_files]:
            try:
                path.unlink()
                removed += 1
            except Exception:
                continue
        if removed:
            logger.info(f"[{PLUGIN_NAME}] 已清理 {removed} 个过期媒体缓存文件")
        return removed

    def clear_cache(self) -> tuple[int, int]:
        """清空缓存目录，返回 (删除文件数, 释放字节数)。"""
        count = 0
        size = 0
        try:
            for path in self.cache_dir.iterdir():
                if not path.is_file():
                    continue
                try:
                    size += path.stat().st_size
                    path.unlink()
                    count += 1
                except Exception:
                    continue
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 清空缓存目录失败: {exc}")
        return count, size

    def clear_data_cache(self) -> tuple[int, int]:
        """删除目录内的推文解析 JSON 缓存，返回 (删除文件数, 释放字节数)。"""
        count = 0
        size = 0
        try:
            for path in self.cache_dir.glob("*.json"):
                if not path.is_file():
                    continue
                try:
                    size += path.stat().st_size
                    path.unlink()
                    count += 1
                except Exception:
                    continue
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 清理推文解析缓存失败: {exc}")
        return count, size

    def cache_stats(self) -> tuple[int, int]:
        """返回 (文件数量, 占用字节数)。"""
        count = 0
        size = 0
        try:
            for path in self.cache_dir.iterdir():
                if path.is_file():
                    count += 1
                    size += path.stat().st_size
        except Exception:
            pass
        return count, size

    # ---------------------------- 图片下载 ---------------------------- #

    async def download_image(
        self, tweet_id: str, url: str, variant: str = ""
    ) -> Optional[Path]:
        """下载图片到本地缓存。超过大小上限或失败时返回 None。

        variant 一般传图片尺寸档位（orig / medium / ...），用于区分不同尺寸的缓存文件。
        """
        return await self._download_media(
            tweet_id=tweet_id,
            url=url,
            max_bytes=self.max_image_size * 1024 * 1024,
            kind="图片",
            variant=variant,
        )

    # ---------------------------- 视频下载 ---------------------------- #

    async def download_video(self, tweet_id: str, url: str) -> Optional[Path]:
        """
        下载视频到本地缓存。

        先尝试用 httpx 直接下载 MP4 直链；若直链不可用或体积超限，
        则改用 yt-dlp 下载。全部失败返回 None。
        """
        max_bytes = self.max_video_size * 1024 * 1024
        clean_url = strip_url_query(url)
        target = self.cache_dir / f"{tweet_id}_video_{stable_digest(clean_url)}.mp4"

        # 锁按 URL 而不是 tweet_id：同一条推文可能换过视频源 / 直链与 yt-dlp 走不同 URL，
        # 用 URL 做键才能既避免重复下载、又不把不同的下载串到一起。
        lock = self._lock_for(f"video:{clean_url}")
        async with lock:
            if target.exists() and target.stat().st_size > 0:
                os.utime(target, None)
                return target

            # 路径一：MP4 直链
            if clean_url.lower().endswith(".mp4") or "video.twimg.com" in clean_url:
                path = await self._download_media(
                    tweet_id=tweet_id,
                    url=clean_url,
                    max_bytes=max_bytes,
                    kind="视频",
                    explicit_path=target,
                )
                if path is not None:
                    return path

            # 路径二：yt-dlp
            return await self._download_with_ytdlp(tweet_id, url, target, max_bytes)

    async def _download_with_ytdlp(
        self, tweet_id: str, url: str, target: Path, max_bytes: int
    ) -> Optional[Path]:
        """通过异步子进程调用 yt-dlp 下载视频。"""
        cmd_prefix = await self._resolve_yt_dlp()
        if not cmd_prefix:
            logger.warning(
                f"[{PLUGIN_NAME}] 未找到 yt-dlp，无法下载视频。"
                "请安装 yt-dlp（pip install yt-dlp）或在插件配置中指定其路径。"
            )
            return None

        template = str(target.with_suffix("")) + ".%(ext)s"
        cmd = [
            *cmd_prefix,
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--retries",
            "3",
            "--socket-timeout",
            str(int(self.timeout)),
            "--max-filesize",
            str(max_bytes),
            "-f",
            "mp4/bv*+ba/b",
            "-o",
            template,
            "--",
            url,
        ]
        if self.cookie_file and Path(self.cookie_file).exists():
            cmd[1:1] = ["--cookies", self.cookie_file]
        if self.proxy:
            cmd[1:1] = ["--proxy", self.proxy]

        logger.info(f"[{PLUGIN_NAME}] 调用 yt-dlp 下载视频: {url}")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.download_timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] yt-dlp 下载超时（{self.download_timeout} 秒），已终止进程"
            )
            await self._kill_process(process)
            return None
        except FileNotFoundError:
            logger.warning(f"[{PLUGIN_NAME}] yt-dlp 可执行文件不存在：{cmd_prefix}")
            return None
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 调用 yt-dlp 失败: {exc}")
            await self._kill_process(process)
            return None

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="ignore").strip()
            detail = detail.splitlines()[-1] if detail else "无错误输出"
            logger.warning(f"[{PLUGIN_NAME}] yt-dlp 退出码 {process.returncode}：{detail}")
            return None

        # yt-dlp 可能因为容器封装产生不同的扩展名，这里定位实际产物
        produced: Optional[Path] = None
        if target.exists() and target.stat().st_size > 0:
            produced = target
        else:
            candidates = sorted(
                self.cache_dir.glob(f"{target.stem}.*"),
                key=lambda p: p.stat().st_size if p.exists() else 0,
                reverse=True,
            )
            for candidate in candidates:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    produced = candidate
                    break

        if produced is None:
            logger.warning(f"[{PLUGIN_NAME}] yt-dlp 执行成功但未找到下载产物：{target.name}")
            return None

        if produced.stat().st_size > max_bytes:
            logger.warning(
                f"[{PLUGIN_NAME}] 视频体积 {produced.stat().st_size / 1024 / 1024:.1f}MB "
                f"超过上限 {self.max_video_size}MB，已丢弃并降级为发送链接"
            )
            self._safe_unlink(produced)
            return None

        logger.info(
            f"[{PLUGIN_NAME}] 视频下载完成：{produced.name} "
            f"({produced.stat().st_size / 1024 / 1024:.1f}MB)"
        )
        self._schedule_cleanup()
        return produced

    async def _resolve_yt_dlp(self) -> Optional[list[str]]:
        """探测可用的 yt-dlp 调用方式，结果会被缓存。"""
        if self._yt_dlp_cmd is not None:
            return self._yt_dlp_cmd or None

        # 1) 配置中显式指定
        if self.yt_dlp_path:
            candidate = self.yt_dlp_path
            if Path(candidate).exists():
                if Path(candidate).stem.lower() in ("python", "python3", "py"):
                    self._yt_dlp_cmd = [candidate, "-m", "yt_dlp"]
                else:
                    self._yt_dlp_cmd = [candidate]
                return self._yt_dlp_cmd
            logger.warning(
                f"[{PLUGIN_NAME}] 配置的 yt-dlp 路径不存在（{candidate}），将尝试自动探测"
            )

        # 2) 从 PATH 中查找
        for name in ("yt-dlp", "yt-dlp.exe"):
            found = shutil.which(name)
            if found:
                self._yt_dlp_cmd = [found]
                return self._yt_dlp_cmd

        # 3) 退化为 python -m yt_dlp
        probe: Any = None
        try:
            probe = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "yt_dlp",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
            )
            await asyncio.wait_for(probe.communicate(), timeout=30)
            if probe.returncode == 0:
                self._yt_dlp_cmd = [sys.executable, "-m", "yt_dlp"]
                return self._yt_dlp_cmd
        except Exception as exc:
            # 超时 / 异常都必须回收子进程，否则探测进程会一直挂在后台
            logger.debug(f"[{PLUGIN_NAME}] yt-dlp 探测失败：{exc}")
            await self._kill_process(probe)

        self._yt_dlp_cmd = []
        return None

    def _subprocess_env(self) -> dict[str, str]:
        """为子进程准备环境变量，保留代理设置与 UTF-8 输出。"""
        env = os.environ.copy()
        if self.proxy:
            env["HTTP_PROXY"] = self.proxy
            env["HTTPS_PROXY"] = self.proxy
            env["ALL_PROXY"] = self.proxy
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    @staticmethod
    async def _kill_process(process: Any) -> None:
        """安全终止子进程。"""
        if process is None or getattr(process, "returncode", 0) is not None:
            return
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass

    # ---------------------------- 通用下载 ---------------------------- #

    async def _download_media(
        self,
        tweet_id: str,
        url: str,
        max_bytes: int,
        kind: str,
        explicit_path: Optional[Path] = None,
        variant: str = "",
    ) -> Optional[Path]:
        """流式下载媒体到本地，带大小上限与并发去重。"""
        if not url or not url.startswith("http"):
            return None

        target = explicit_path or self.build_cache_path(tweet_id, url, variant=variant)
        lock = self._lock_for(str(target))

        async with lock:
            if target.exists() and target.stat().st_size > 0:
                os.utime(target, None)
                return target

            temp = target.with_name(target.name + ".part")
            try:
                async with self.client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": self.user_agent, "Referer": "https://x.com/"},
                    timeout=self.timeout,
                    follow_redirects=True,
                ) as response:
                    if response.status_code >= 400:
                        logger.debug(
                            f"[{PLUGIN_NAME}] {kind}下载失败 HTTP {response.status_code}：{url}"
                        )
                        return None

                    # 先看响应头里的大小声明，尽早放弃超大文件
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        logger.info(
                            f"[{PLUGIN_NAME}] {kind}体积 {int(declared) / 1024 / 1024:.1f}MB "
                            f"超过上限，降级为发送原链接：{url}"
                        )
                        return None

                    downloaded = 0
                    with open(temp, "wb") as fp:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                raise _MediaTooLargeError
                            # 写盘放到线程里，避免大文件下载（最长 180s）阻塞事件循环
                            await asyncio.to_thread(fp.write, chunk)
            except _MediaTooLargeError:
                self._safe_unlink(temp)
                logger.info(
                    f"[{PLUGIN_NAME}] {kind}超过大小上限（{max_bytes // 1024 // 1024}MB），"
                    f"降级为发送原链接：{url}"
                )
                return None
            except httpx.TimeoutException:
                self._safe_unlink(temp)
                logger.warning(f"[{PLUGIN_NAME}] {kind}下载超时：{url}")
                return None
            except Exception as exc:
                self._safe_unlink(temp)
                logger.warning(f"[{PLUGIN_NAME}] {kind}下载失败（{exc}）：{url}")
                return None

            if not temp.exists() or temp.stat().st_size == 0:
                self._safe_unlink(temp)
                logger.warning(f"[{PLUGIN_NAME}] {kind}下载结果为空：{url}")
                return None

            try:
                os.replace(temp, target)
            except Exception as exc:
                self._safe_unlink(temp)
                logger.warning(f"[{PLUGIN_NAME}] {kind}写入缓存失败（{exc}）：{url}")
                return None

            # 触达更新时间，配合 LRU 清理
            os.utime(target, None)
            self._schedule_cleanup()
            return target

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def _schedule_cleanup(self) -> None:
        """在事件循环空闲时执行一次缓存清理，避免阻塞当前流程。"""
        if self.cache_max_files <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.cleanup_cache)
        except RuntimeError:
            self.cleanup_cache()





# 动态导出：`from .x_tweet_core import *` 要连 _ 前缀的常量、正则、私有工具函数一起带出去，
# 因此不能依赖 Python「import * 跳过下划线名字」的默认行为。
__all__ = [name for name in list(globals()) if not name.startswith("__")]
