"""
微博监控插件 v2
使用高级反爬策略，多API自动切换
"""

import asyncio
import json
import re
import os
import random
import hashlib
from pathlib import Path
from typing import Optional
from datetime import datetime

import aiohttp
from PIL import Image
from io import BytesIO

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image as ImageSeg


# 随机User-Agent池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


class WeiboMonitor(Star):
    """微博监控插件 v2"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.name = "微博监控"
        self.session: Optional[aiohttp.ClientSession] = None

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_weibo"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir

        self.state_file = data_dir / "state.json"
        self.last_post_ids: dict[str, str] = {}
        self._load_state()

        self._fetch_lock = False
        self._task: Optional[asyncio.Task] = None
        self._last_request_time = 0
        logger.info("微博监控插件v2已加载")

    def _load_state(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.last_post_ids = data.get("last_post_ids", {})
            except Exception as e:
                logger.error(f"加载状态失败: {e}")

    def _save_state(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump({"last_post_ids": self.last_post_ids}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建HTTP会话"""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            connector = aiohttp.TCPConnector(ssl=False, limit=10)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self.session

    async def on_astrbot_loaded(self):
        interval = self.config.get("check_interval", 10)
        self._task = asyncio.create_task(self._monitor_loop(interval * 60))
        logger.info(f"微博监控已启动，检查间隔: {interval} 分钟")

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()
        self._save_state()
        logger.info("微博监控插件已卸载")

    async def _monitor_loop(self, interval_seconds: int):
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self._check_new_posts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环出错: {e}")
                await asyncio.sleep(60)

    async def _rate_limit(self):
        """请求限速，避免被封"""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 2:
            await asyncio.sleep(2 - elapsed)
        self._last_request_time = time.time()

    async def _check_new_posts(self):
        if self._fetch_lock:
            logger.debug("上次爬取尚未完成，跳过本次")
            return

        self._fetch_lock = True
        try:
            cookies = self.config.get("cookies", "").strip()
            if not cookies:
                logger.warning("未配置微博Cookie，跳过检查")
                return

            watch_users = self._parse_multiline_config("watch_users")
            if not watch_users:
                logger.warning("未配置监控用户，跳过检查")
                return

            target_groups = self._parse_multiline_config("target_groups")
            if not target_groups:
                logger.warning("未配置目标群聊，跳过检查")
                return

            skip_top = self.config.get("skip_top_post", True)

            for uid in watch_users:
                uid = uid.strip()
                if not uid:
                    continue

                try:
                    await self._rate_limit()
                    new_posts = await self._fetch_user_posts(uid, cookies, skip_top)
                    if new_posts:
                        for post in new_posts:
                            await self._send_to_groups(post, target_groups)
                            await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"获取用户 {uid} 动态失败: {e}")

        finally:
            self._fetch_lock = False

    async def _fetch_user_posts(self, uid: str, cookies: str, skip_top: bool) -> list[dict]:
        """获取用户最新微博动态，尝试多个API"""

        # 策略1: 尝试移动版API
        posts = await self._try_mobile_api(uid, cookies, skip_top)
        if posts:
            return posts

        # 策略2: 尝试网页版API
        posts = await self._try_web_api(uid, cookies, skip_top)
        if posts:
            return posts

        # 策略3: 尝试搜索API
        posts = await self._try_search_api(uid, cookies, skip_top)
        if posts:
            return posts

        logger.warning(f"所有API策略均失败，用户: {uid}")
        return []

    async def _try_mobile_api(self, uid: str, cookies: str, skip_top: bool) -> list[dict]:
        """策略1: 移动版API"""
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, "https://m.weibo.cn/")

            url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}&page=1"

            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []

                text = await resp.text()
                if self._is_login_page(text):
                    logger.warning("移动API: Cookie无效")
                    return []

                try:
                    data = json.loads(text)
                except:
                    return []

                if data.get("ok") != 1:
                    return []

                return self._parse_cards(data.get("data", {}).get("cards", []), uid, skip_top)
        except Exception as e:
            logger.debug(f"移动API失败: {e}")
            return []

    async def _try_web_api(self, uid: str, cookies: str, skip_top: bool) -> list[dict]:
        """策略2: 网页版Ajax API"""
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, f"https://weibo.com/u/{uid}")
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["X-Requested-With"] = "fetch"

            url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0"

            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return []

                text = await resp.text()
                if self._is_login_page(text):
                    logger.warning("网页API: Cookie无效")
                    return []

                try:
                    data = json.loads(text)
                except:
                    return []

                if data.get("ok") != 1:
                    return []

                posts = data.get("data", {}).get("list", [])
                return self._parse_posts_list(posts, uid, skip_top)
        except Exception as e:
            logger.debug(f"网页API失败: {e}")
            return []

    async def _try_search_api(self, uid: str, cookies: str, skip_top: bool) -> list[dict]:
        """策略3: 搜索API，通过uid获取用户名后再搜索"""
        try:
            # 先获取用户名
            username = await self._get_username(uid, cookies)
            if not username:
                return []

            session = await self._get_session()
            headers = self._build_headers(cookies, "https://s.weibo.com")

            # 使用搜索API
            search_url = f"https://s.weibo.com/weibo?q=from%3A{username}&Refer=SWeibo_box"
            encoded_url = f"https://s.weibo.com/weibo?q=from%3A{username}&typeall=1&suball=1&timescope=custom:recent&Refer=index"

            async with session.get(encoded_url, headers=headers) as resp:
                text = await resp.text()
                if self._is_login_page(text):
                    logger.warning("搜索API: Cookie无效")
                    return []

                # 解析搜索结果页面
                return self._parse_search_page(text, uid, skip_top)
        except Exception as e:
            logger.debug(f"搜索API失败: {e}")
            return []

    async def _get_username(self, uid: str, cookies: str) -> Optional[str]:
        """获取微博用户名"""
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, f"https://weibo.com/u/{uid}")
            headers["Accept"] = "application/json"

            url = f"https://weibo.com/ajax/profile/info?uid={uid}"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    if data.get("ok") == 1:
                        return data.get("data", {}).get("user", {}).get("screen_name")
        except:
            pass
        return None

    def _build_headers(self, cookies: str, referer: str) -> dict:
        """构建完整的请求头"""
        return {
            "Cookie": cookies,
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
        }

    def _is_login_page(self, text: str) -> bool:
        """检测是否是登录页面"""
        text_lower = text.lower()
        login_indicators = ['登录', 'login', 'passport.weibo', 'sso.weibo', '请先登录']
        return any(indicator.lower() in text_lower for indicator in login_indicators)

    def _parse_cards(self, cards: list, uid: str, skip_top: bool) -> list[dict]:
        """解析移动API的cards"""
        new_posts = []
        seen_ids = set()

        for card in cards:
            if card.get("card_type") != 9:
                continue

            mblog = card.get("mblog", {})
            post_id = mblog.get("id", "")

            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            if skip_top and mblog.get("isTop"):
                continue

            if uid in self.last_post_ids and self.last_post_ids[uid] == post_id:
                break

            post_data = self._parse_weibo_post(mblog)
            if post_data:
                new_posts.append(post_data)

        if cards:
            for card in cards:
                if card.get("card_type") == 9:
                    self.last_post_ids[uid] = card.get("mblog", {}).get("id", "")
                    self._save_state()
                    break

        return new_posts

    def _parse_posts_list(self, posts: list, uid: str, skip_top: bool) -> list[dict]:
        """解析网页API的posts list"""
        new_posts = []

        for post in posts:
            post_id = post.get("id", "")

            if skip_top and post.get("isTop"):
                continue

            if uid in self.last_post_ids and self.last_post_ids[uid] == post_id:
                break

            post_data = self._parse_weibo_post_web(post)
            if post_data:
                new_posts.append(post_data)

        if posts:
            self.last_post_ids[uid] = posts[0].get("id", "")
            self._save_state()

        return new_posts

    def _parse_search_page(self, html: str, uid: str, skip_top: bool) -> list[dict]:
        """解析搜索页面HTML"""
        new_posts = []

        # 提取微博内容
        pattern = r'<div class="card-feed".*?>(.*?)</div>\s*</div>'
        cards = re.findall(pattern, html, re.DOTALL)

        for card in cards[:10]:
            try:
                # 提取用户名
                user_match = re.search(r'nick-name="([^"]+)"', card)
                username = user_match.group(1) if user_match else "未知"

                # 提取内容
                content_match = re.search(r'<p class="txt".*?>(.*?)</p>', card, re.DOTALL)
                if content_match:
                    text = re.sub(r'<[^>]+>', '', content_match.group(1))
                    text = text.strip()

                # 提取图片
                img_match = re.search(r'<img src="([^"]+)"[^>]*class="feed-img"', card)
                image_url = img_match.group(1) if img_match else None

                # 提取链接
                link_match = re.search(r'<a href="([^"]+)"[^>]*class="from"', card)
                post_url = "https://weibo.com" + link_match.group(1) if link_match else ""

                if text:
                    new_posts.append({
                        "username": username,
                        "uid": uid,
                        "text": text[:500],
                        "image_url": image_url,
                        "created_at": "",
                        "url": post_url,
                    })
            except Exception as e:
                logger.debug(f"解析搜索结果失败: {e}")

        if new_posts and uid not in self.last_post_ids:
            self.last_post_ids[uid] = "checked"
            self._save_state()

        return new_posts

    def _parse_weibo_post(self, mblog: dict) -> Optional[dict]:
        """解析微博动态（移动API格式）"""
        try:
            text = mblog.get("text", "")
            text = re.sub(r'<[^>]+>', '', text)
            text = text.strip()

            if not text:
                return None

            user = mblog.get("user", {})
            username = user.get("screen_name", "未知用户")

            pics = mblog.get("pics", [])
            image_url = None
            if pics:
                first_pic = pics[0]
                if isinstance(first_pic, dict):
                    image_url = first_pic.get("large", {}).get("url") or first_pic.get("url")
                elif isinstance(first_pic, str):
                    image_url = first_pic

            post_url = f"https://weibo.com/{user.get('id', '')}/{mblog.get('bid', '')}"

            return {
                "username": username,
                "uid": user.get("id", ""),
                "text": text[:500],
                "image_url": image_url,
                "created_at": mblog.get("created_at", ""),
                "url": post_url,
            }
        except Exception as e:
            logger.error(f"解析微博失败: {e}")
            return None

    def _parse_weibo_post_web(self, post: dict) -> Optional[dict]:
        """解析微博动态（网页API格式）"""
        try:
            text = post.get("text", "")
            text = re.sub(r'<[^>]+>', '', text)
            text = text.strip()

            if not text:
                return None

            user = post.get("user", {})
            username = user.get("screen_name", "未知用户")

            image_url = None
            pics = post.get("pic_ids", [])
            if pics:
                image_url = f"https://wx1.sinaimg.cn/large/{pics[0]}.jpg"

            post_url = f"https://weibo.com/{user.get('id', '')}/{post.get('bid', '')}"

            return {
                "username": username,
                "uid": user.get("id", ""),
                "text": text[:500],
                "image_url": image_url,
                "created_at": post.get("created_at", ""),
                "url": post_url,
            }
        except Exception as e:
            logger.error(f"解析微博失败: {e}")
            return None

    async def _send_to_groups(self, post: dict, groups: list):
        message = self._format_post_message(post)
        chain = [{"type": "Plain", "text": message}]

        if post.get("image_url"):
            try:
                image_path = await self._download_image(post["image_url"])
                if image_path:
                    chain.append({"type": "image", "data": {"file": image_path}})
            except Exception as e:
                logger.error(f"下载图片失败: {e}")

        from astrbot.api.event import MessageChain
        message_chain = MessageChain().from_dict(chain)

        for group_id in groups:
            group_id = group_id.strip()
            if not group_id:
                continue

            try:
                await self.context.send_message(f"group_{group_id}", message_chain)
                logger.info(f"已推送动态到群 {group_id}")
            except Exception as e:
                logger.error(f"发送到群 {group_id} 失败: {e}")

    async def _download_image(self, url: str) -> Optional[str]:
        try:
            session = await self._get_session()
            async with session.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}) as resp:
                if resp.status != 200:
                    return None

                content = await resp.read()
                img = Image.open(BytesIO(content))
                if img.mode == "RGBA":
                    img = img.convert("RGB")

                filename = f"weibo_{datetime.now().strftime('%H%M%S')}_{random.randint(1000,9999)}.jpg"
                save_path = self.data_dir / filename
                img.save(save_path, "JPEG", quality=85, optimize=True)
                return str(save_path)
        except Exception as e:
            logger.error(f"下载图片出错: {e}")
            return None

    def _format_post_message(self, post: dict) -> str:
        return (
            "【微博更新】\n\n"
            f"👤 {post['username']}\n\n"
            f"{post['text']}\n\n"
            f"🔗 {post['url']}"
        )

    def _parse_multiline_config(self, key: str) -> list[str]:
        value = self.config.get(key, "")
        if not value:
            return []
        return [line.strip() for line in value.strip().split('\n') if line.strip()]

    # ============ 指令 ============

    @filter.command("微博监控")
    async def weibo_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "【微博监控插件v2】使用说明\n\n"
            "📌 指令：\n"
            "/微博状态 - 查看监控状态\n"
            "/微博测试 - 测试微博连接\n"
            "/微博推送 - 手动触发检查\n"
            "/微博监控 - 显示此帮助\n\n"
            "⚙️ 配置：cookies、watch_users、target_groups"
        )

    @filter.command("微博状态")
    async def weibo_status(self, event: AstrMessageEvent):
        cookies = self.config.get("cookies", "").strip()
        watch_users = self._parse_multiline_config("watch_users")
        target_groups = self._parse_multiline_config("target_groups")
        interval = self.config.get("check_interval", 10)

        status = [
            "【微博监控状态】",
            f"📡 Cookie: {'已配置' if cookies else '未配置 ❌'}",
            f"👥 监控用户: {len(watch_users)}",
            f"💬 推送群聊: {len(target_groups)}",
            f"⏰ 检查间隔: {interval}分钟",
            "",
            "📋 监控用户:",
        ]

        for uid in watch_users[:5]:
            status.append(f"  • {uid}")
        if len(watch_users) > 5:
            status.append(f"  ... 还有{len(watch_users)-5}个")

        yield event.plain_result("\n".join(status))

    @filter.command("微博测试")
    async def weibo_test(self, event: AstrMessageEvent):
        cookies = self.config.get("cookies", "").strip()

        if not cookies:
            yield event.plain_result("❌ 未配置Cookie，请在插件设置中配置")
            return

        yield event.plain_result("🔄 正在测试微博连接（尝试多个API）...")

        watch_users = self._parse_multiline_config("watch_users")
        test_uid = watch_users[0].strip() if watch_users else "1195230310"

        # 尝试三个API
        for strategy_name, posts in [
            ("移动API", await self._try_mobile_api(test_uid, cookies, True)),
            ("网页API", await self._try_web_api(test_uid, cookies, True)),
            ("搜索API", await self._try_search_api(test_uid, cookies, True)),
        ]:
            if posts:
                post = posts[0]
                yield event.plain_result(
                    f"✅ 微博连接成功！\n"
                    f"📡 使用策略: {strategy_name}\n"
                    f"👤 用户: {post['username']}\n"
                    f"📝 最新微博:\n{post['text'][:100]}..."
                )
                return

            await asyncio.sleep(1)

        yield event.plain_result(
            "❌ 所有API策略均失败\n\n"
            "可能原因：\n"
            "1. Cookie已过期，请重新获取\n"
            "2. 微博账号被限制\n"
            "3. 服务器IP被限制\n\n"
            "建议：使用小号登录获取Cookie"
        )

    @filter.command("微博推送")
    async def weibo_push(self, event: AstrMessageEvent):
        if self._fetch_lock:
            yield event.plain_result("⚠️ 正在执行中...")
            return

        yield event.plain_result("🔄 开始检查...")
        asyncio.create_task(self._manual_push())
        yield event.plain_result("✅ 已开始检查")

    async def _manual_push(self):
        await self._check_new_posts()
        logger.info("手动推送完成")


import time
