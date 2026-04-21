"""
微博监控插件 v3
功能：
1. 监控指定微博用户的新动态
2. 支持转发内容爬取
3. 图片下载发送
4. 支持私聊和群聊转发
5. 自动获取和刷新Cookie
"""

import asyncio
import json
import platform
import random
import re
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

from dataclasses import asdict, dataclass

import aiohttp
from PIL import Image

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Image as ImageSeg


@dataclass
class CookieManager:
    """Cookie管理器"""
    cookies: str = ""
    last_updated: str = ""
    expires_at: str = ""


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


class WeiboMonitor(Star):
    """微博监控插件 v3"""

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
        self.cookie_file = data_dir / "cookies.json"
        self.last_post_ids: dict[str, str] = {}
        self._load_state()
        self._cookie_manager = CookieManager()
        self._load_cookies()

        self._fetch_lock = False
        self._task: Optional[asyncio.Task] = None
        self._cookie_refresh_task: Optional[asyncio.Task] = None
        self._last_request_time = 0
        self._playwright_browser = None
        logger.info("微博监控插件v3已加载")

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

    def _load_cookies(self):
        """加载保存的Cookie"""
        if self.cookie_file.exists():
            try:
                with open(self.cookie_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._cookie_manager = CookieManager(**data)
                    logger.info(f"已加载Cookie，上次更新: {self._cookie_manager.last_updated}")
            except Exception as e:
                logger.error(f"加载Cookie失败: {e}")

    def _save_cookies(self):
        """保存Cookie到文件"""
        try:
            with open(self.cookie_file, 'w', encoding='utf-8') as f:
                json.dump(asdict(self._cookie_manager), f, ensure_ascii=False, indent=2)
            logger.info("Cookie已保存")
        except Exception as e:
            logger.error(f"保存Cookie失败: {e}")

    def _get_active_cookies(self) -> str:
        """获取当前可用的Cookie（优先使用自动获取的，其次使用配置的）"""
        # 优先使用自动获取的Cookie
        if self._cookie_manager.cookies:
            return self._cookie_manager.cookies
        # 备用：使用配置的静态Cookie
        return self.config.get("cookies", "").strip()

    def _is_cookie_expired(self) -> bool:
        """检查Cookie是否需要刷新"""
        if not self._cookie_manager.expires_at:
            return True
        try:
            expires = datetime.fromisoformat(self._cookie_manager.expires_at)
            # 提前1小时刷新
            return datetime.now() >= (expires - timedelta(hours=1))
        except:
            return True

    async def _init_playwright(self):
        """初始化Playwright浏览器"""
        if self._playwright_browser is not None:
            return self._playwright_browser

        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            self._playwright_browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-setuid-sandbox',
                    '--disable-web-security',
                    '--user-data-dir=/tmp/chrome-data',
                ]
            )
            logger.info(f"Playwright 浏览器初始化成功 (平台: {platform.system()})")
            return self._playwright_browser
        except ImportError:
            logger.error("未安装playwright，请运行: pip install playwright && playwright install chromium")
            return None
        except Exception as e:
            logger.error(f"Playwright初始化失败: {e}")
            return None

    async def _auto_login_weibo(self) -> Optional[str]:
        """
        使用Playwright自动登录微博获取Cookie
        返回Cookie字符串或None
        """
        browser = await self._init_playwright()
        if not browser:
            return None

        page = None
        try:
            logger.info("开始自动登录微博...")

            # 设置更长的超时
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            page = await browser.new_page(viewport={"width": 1280, "height": 720})

            # 先尝试访问移动端登录（更容易处理）
            try:
                await page.goto(
                    "https://passport.weibo.com/visitor/visitor",
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                await asyncio.sleep(2)
            except Exception:
                # 备用：访问主站
                await page.goto("https://weibo.com", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)

            # 等待用户扫码登录
            logger.info("请在浏览器中扫码登录微博（60秒内）...")

            # 等待登录成功标志（检查是否有用户信息）
            try:
                await page.wait_for_function(
                    """() => document.cookie.includes('SUB') || document.cookie.includes('ALF')""",
                    timeout=60000
                )
                cookies = await page.context.cookies()
                if cookies:
                    cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])

                    if await self._verify_cookies(cookie_str):
                        logger.info("自动获取Cookie成功")
                        return cookie_str
            except PlaywrightTimeoutError:
                logger.info("等待登录超时，请手动扫码")

            # 获取当前cookies（可能部分有效）
            cookies = await page.context.cookies()
            if cookies:
                cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                if await self._verify_cookies(cookie_str):
                    logger.info("获取到Cookie")
                    return cookie_str

            logger.info("检测到需要登录，请手动扫码...")
            return None

        except Exception as e:
            logger.error(f"自动登录失败: {e}")
            return None
        finally:
            if page:
                await page.close()

    async def _verify_cookies(self, cookies: str) -> bool:
        """验证Cookie是否有效"""
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, "https://weibo.com/")
            url = "https://weibo.com/ajax/statuses/mymblog?uid=1195230310&page=1"

            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if not self._is_login_page(text):
                        return True
            return False
        except:
            return False

    async def _refresh_cookies_auto(self):
        """自动刷新Cookie"""
        if not self._is_cookie_expired():
            return

        logger.info("正在刷新Cookie...")

        # 尝试使用已保存的会话刷新
        if self._cookie_manager.cookies:
            new_cookies = await self._refresh_session(self._cookie_manager.cookies)
            if new_cookies:
                self._cookie_manager.cookies = new_cookies
                self._cookie_manager.last_updated = datetime.now().isoformat()
                self._cookie_manager.expires_at = (datetime.now() + timedelta(days=7)).isoformat()
                self._save_cookies()
                logger.info("Cookie刷新成功")
                return

        # 尝试自动登录
        new_cookies = await self._auto_login_weibo()
        if new_cookies:
            self._cookie_manager.cookies = new_cookies
            self._cookie_manager.last_updated = datetime.now().isoformat()
            self._cookie_manager.expires_at = (datetime.now() + timedelta(days=7)).isoformat()
            self._save_cookies()
            logger.info("自动登录获取Cookie成功")

    async def _refresh_session(self, cookies: str) -> Optional[str]:
        """通过刷新会话获取新Cookie"""
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, "https://weibo.com/")
            headers['X-Requested-With'] = 'XMLHttpRequest'

            # 访问个人主页刷新会话
            async with session.get("https://weibo.com/u/1195230310/home", headers=headers) as resp:
                if resp.status == 200:
                    return cookies
            return None
        except:
            return None

    def _get_cookie_from_file(self) -> Optional[str]:
        """从文件读取Cookie"""
        cookie_path = self.data_dir / "weibo_cookies.txt"
        if cookie_path.exists():
            try:
                with open(cookie_path, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            except:
                pass
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            connector = aiohttp.TCPConnector(ssl=False, limit=10)
            self.session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self.session

    async def on_astrbot_loaded(self):
        interval = self.config.get("check_interval", 10)
        self._task = asyncio.create_task(self._monitor_loop(interval * 60))
        # 启动Cookie刷新定时任务（每6小时检查一次）
        self._cookie_refresh_task = asyncio.create_task(self._cookie_refresh_loop())
        logger.info(f"微博监控已启动，检查间隔: {interval} 分钟")

    async def _cookie_refresh_loop(self):
        """Cookie刷新定时循环"""
        while True:
            try:
                # 每6小时检查一次
                await asyncio.sleep(6 * 60 * 60)
                await self._refresh_cookies_auto()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cookie刷新循环出错: {e}")
                await asyncio.sleep(300)

    async def terminate(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._cookie_refresh_task:
            self._cookie_refresh_task.cancel()
            try:
                await self._cookie_refresh_task
            except asyncio.CancelledError:
                pass
        if self._playwright_browser:
            try:
                await self._playwright_browser.close()
            except:
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
            # 获取Cookie（优先自动获取的，其次配置的）
            cookies = self._get_active_cookies()
            if not cookies:
                logger.warning("未配置微博Cookie，跳过检查")
                return

            watch_users = self._parse_multiline_config("watch_users")
            if not watch_users:
                logger.warning("未配置监控用户，跳过检查")
                return

            # 获取所有目标（群聊和私聊）
            target_groups = self._parse_multiline_config("target_groups")
            target_users = self._parse_multiline_config("target_users")  # 私聊用户
            targets = self._build_targets(target_groups, target_users)

            if not targets:
                logger.warning("未配置目标，跳过检查")
                return

            skip_top = self.config.get("skip_top_post", True)
            include_retweet = self.config.get("include_retweet", True)  # 新增：是否包含转发

            for uid in watch_users:
                uid = uid.strip()
                if not uid:
                    continue

                try:
                    await self._rate_limit()
                    new_posts = await self._fetch_user_posts(uid, cookies, skip_top, include_retweet)
                    if new_posts:
                        for post in new_posts:
                            await self._send_to_targets(post, targets)
                            await asyncio.sleep(3)
                except Exception as e:
                    logger.error(f"获取用户 {uid} 动态失败: {e}")

        finally:
            self._fetch_lock = False

    def _build_targets(self, groups: list, users: list) -> list[dict]:
        """构建目标列表"""
        targets = []
        for g in groups:
            if g.strip():
                targets.append({"type": "group", "id": g.strip()})
        for u in users:
            if u.strip():
                targets.append({"type": "private", "id": u.strip()})
        return targets

    async def _fetch_user_posts(self, uid: str, cookies: str, skip_top: bool, include_retweet: bool) -> list[dict]:
        """获取用户最新微博动态"""
        posts = await self._try_mobile_api(uid, cookies, skip_top, include_retweet)
        if posts:
            return posts

        posts = await self._try_web_api(uid, cookies, skip_top, include_retweet)
        if posts:
            return posts

        return []

    async def _try_mobile_api(self, uid: str, cookies: str, skip_top: bool, include_retweet: bool) -> list[dict]:
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

                return self._parse_cards_v3(data.get("data", {}).get("cards", []), uid, skip_top, include_retweet)
        except Exception as e:
            logger.debug(f"移动API失败: {e}")
            return []

    async def _try_web_api(self, uid: str, cookies: str, skip_top: bool, include_retweet: bool) -> list[dict]:
        try:
            session = await self._get_session()
            headers = self._build_headers(cookies, f"https://weibo.com/u/{uid}")
            headers["X-Requested-With"] = "XMLHttpRequest"
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
                return self._parse_posts_list_v3(posts, uid, skip_top, include_retweet)
        except Exception as e:
            logger.debug(f"网页API失败: {e}")
            return []

    def _build_headers(self, cookies: str, referer: str) -> dict:
        return {
            "Cookie": cookies,
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": referer,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }

    def _is_login_page(self, text: str) -> bool:
        text_lower = text.lower()
        return any(x in text_lower for x in ['登录', 'login', 'passport.weibo', '请先登录'])

    def _parse_cards_v3(self, cards: list, uid: str, skip_top: bool, include_retweet: bool) -> list[dict]:
        """解析移动API的cards，支持转发"""
        new_posts = []
        seen_ids = set()

        for card in cards:
            card_type = card.get("card_type", 0)

            # 普通微博: card_type=9
            # 转发微博: card_type=9, mblog里有retweet
            if card_type != 9:
                continue

            mblog = card.get("mblog", {})
            post_id = mblog.get("id", "")

            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            if skip_top and mblog.get("isTop"):
                continue

            # 检查是否已推送过
            if uid in self.last_post_ids and self.last_post_ids[uid] == post_id:
                break

            # 解析原微博（可能被转发的）
            post_data = self._parse_weibo_post_v3(mblog)

            # 处理转发内容
            retweet = mblog.get("retweeted_status")
            if retweet and include_retweet:
                retweet_data = self._parse_weibo_post_v3(retweet)
                post_data["is_retweet"] = True
                post_data["original_user"] = retweet_data["username"]
                post_data["original_text"] = retweet_data["text"]
                post_data["original_url"] = retweet_data["url"]
                # 如果原微博有图片，用原微博的图片
                if retweet_data["image_url"]:
                    post_data["image_url"] = retweet_data["image_url"]

            if post_data:
                new_posts.append(post_data)

        if cards:
            for card in cards:
                if card.get("card_type") == 9:
                    self.last_post_ids[uid] = card.get("mblog", {}).get("id", "")
                    self._save_state()
                    break

        return new_posts

    def _parse_posts_list_v3(self, posts: list, uid: str, skip_top: bool, include_retweet: bool) -> list[dict]:
        """解析网页API的posts list，支持转发"""
        new_posts = []

        for post in posts:
            post_id = post.get("id", "")

            if skip_top and post.get("isTop"):
                continue

            if uid in self.last_post_ids and self.last_post_ids[uid] == post_id:
                break

            post_data = self._parse_weibo_post_web_v3(post)

            # 处理转发
            retweet = post.get("retweeted_status")
            if retweet and include_retweet:
                retweet_data = self._parse_weibo_post_web_v3(retweet)
                post_data["is_retweet"] = True
                post_data["original_user"] = retweet_data["username"]
                post_data["original_text"] = retweet_data["text"]
                post_data["original_url"] = retweet_data["url"]
                if retweet_data["image_url"]:
                    post_data["image_url"] = retweet_data["image_url"]

            if post_data:
                new_posts.append(post_data)

        if posts:
            self.last_post_ids[uid] = posts[0].get("id", "")
            self._save_state()

        return new_posts

    def _parse_weibo_post_v3(self, mblog: dict) -> Optional[dict]:
        """解析微博动态（移动API格式）"""
        try:
            text = mblog.get("text", "")
            text = re.sub(r'<[^>]+>', '', text)
            text = text.strip()

            if not text:
                return None

            user = mblog.get("user", {})
            username = user.get("screen_name", "未知用户")

            # 获取图片
            image_url = None
            pics = mblog.get("pics", [])
            if pics:
                first_pic = pics[0]
                if isinstance(first_pic, dict):
                    image_url = first_pic.get("large", {}).get("url") or first_pic.get("url")
                elif isinstance(first_pic, str):
                    image_url = first_pic

            post_url = f"https://weibo.com/{user.get('id', '')}/{mblog.get('bid', '')}"
            created_at = mblog.get("created_at", "")

            return {
                "username": username,
                "uid": user.get("id", ""),
                "text": text[:500],
                "image_url": image_url,
                "created_at": created_at,
                "url": post_url,
                "is_retweet": False,
            }
        except Exception as e:
            logger.error(f"解析微博失败: {e}")
            return None

    def _parse_weibo_post_web_v3(self, post: dict) -> Optional[dict]:
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
                picid = pics[0]
                # 尝试多种图片尺寸
                for size in ['large', 'mw2000', 'mw1080', 'mw690', 'orj360', 'square']:
                    image_url = f"https://wx1.sinaimg.cn/{size}/{picid}.jpg"
                    break

            post_url = f"https://weibo.com/{user.get('id', '')}/{post.get('bid', '')}"
            created_at = post.get("created_at", "")

            return {
                "username": username,
                "uid": user.get("id", ""),
                "text": text[:500],
                "image_url": image_url,
                "created_at": created_at,
                "url": post_url,
                "is_retweet": False,
            }
        except Exception as e:
            logger.error(f"解析微博失败: {e}")
            return None

    async def _send_to_targets(self, post: dict, targets: list):
        """发送动态到所有目标（群聊和私聊）"""
        message = self._format_post_message_v3(post)

        chain = [{"type": "Plain", "text": message}]

        # 如果有图片，下载并添加
        if post.get("image_url"):
            try:
                image_path = await self._download_image(post["image_url"])
                if image_path:
                    chain.append({"type": "image", "data": {"file": image_path}})
            except Exception as e:
                logger.error(f"下载图片失败: {e}")

        from astrbot.api.event import MessageChain
        message_chain = MessageChain().from_dict(chain)

        for target in targets:
            target_type = target["type"]
            target_id = target["id"]

            try:
                if target_type == "group":
                    identifier = f"group_{target_id}"
                else:
                    identifier = f"private_{target_id}"

                await self.context.send_message(identifier, message_chain)
                logger.info(f"已推送动态到 {target_type} {target_id}")
            except Exception as e:
                logger.error(f"发送到 {target_type} {target_id} 失败: {e}")

            await asyncio.sleep(1)

    async def _download_image(self, url: str) -> Optional[str]:
        """下载并压缩图片"""
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

    def _format_post_message_v3(self, post: dict) -> str:
        """格式化推送消息 v3"""
        username = post["username"]

        # 如果是转发，显示原文作者
        if post.get("is_retweet"):
            original_user = post.get("original_user", "未知")
            original_text = post.get("original_text", "")[:200]
            original_url = post.get("original_url", "")

            msg = (
                f"🔄 {username} 转发了一条微博！\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📝 转发言论:\n{post['text'][:200]}\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🔁 原文来自 @{original_user}:\n"
                f"{original_text}\n"
                f"🔗 {original_url}\n"
                f"━━━━━━━━━━━━━━━━"
            )
        else:
            msg = (
                f"🔔 {username} 更新微博啦！\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{post['text']}\n\n"
                f"🔗 {post['url']}\n"
                f"━━━━━━━━━━━━━━━━"
            )

        return msg

    def _parse_multiline_config(self, key: str) -> list[str]:
        value = self.config.get(key, "")
        if not value:
            return []
        return [line.strip() for line in value.strip().split('\n') if line.strip()]

    # ============ 指令 ============

    @filter.command("微博监控")
    async def weibo_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "【微博监控插件v3】使用说明\n\n"
            "📌 指令：\n"
            "/微博状态 - 查看监控状态\n"
            "/微博测试 - 测试微博连接\n"
            "/微博推送 - 手动触发检查\n"
            "/微博登录 - 自动登录获取Cookie\n"
            "/微博刷新Cookie - 刷新Cookie\n"
            "/微博监控 - 显示此帮助\n\n"
            "⚙️ 配置项：\n"
            "• cookies - 微博Cookie（可选，支持自动获取）\n"
            "• watch_users - 监控用户UID\n"
            "• target_groups - 推送群聊ID\n"
            "• target_users - 推送私聊用户ID\n"
            "• check_interval - 检查间隔(分钟)\n"
            "• skip_top_post - 是否跳过置顶\n"
            "• include_retweet - 是否包含转发动态\n\n"
            "🔧 自动Cookie功能：\n"
            "• 自动获取Cookie无需手动配置\n"
            "• 每6小时自动刷新Cookie\n"
            "• 也支持手动配置的静态Cookie"
        )

    @filter.command("微博状态")
    async def weibo_status(self, event: AstrMessageEvent):
        static_cookies = self.config.get("cookies", "").strip()
        auto_cookies = self._cookie_manager.cookies
        watch_users = self._parse_multiline_config("watch_users")
        target_groups = self._parse_multiline_config("target_groups")
        target_users = self._parse_multiline_config("target_users")
        interval = self.config.get("check_interval", 10)
        include_retweet = self.config.get("include_retweet", True)

        # 确定使用的是哪种Cookie
        active_cookie = self._get_active_cookies()
        cookie_source = "自动获取" if auto_cookies == active_cookie else "手动配置"

        status = [
            "【微博监控状态 v3】",
            f"📡 Cookie: {'已配置' if active_cookie else '未配置 ❌'}",
            f"   来源: {cookie_source}",
        ]

        if self._cookie_manager.last_updated:
            status.append(f"   上次更新: {self._cookie_manager.last_updated[:19]}")

        status.extend([
            f"👥 监控用户: {len(watch_users)}",
            f"💬 推送群聊: {len(target_groups)}",
            f"👤 推送私聊: {len(target_users)}",
            f"🔄 包含转发: {'是' if include_retweet else '否'}",
            f"⏰ 检查间隔: {interval}分钟",
            "",
            "📋 监控用户:",
        ])

        for uid in watch_users[:5]:
            status.append(f"  • {uid}")
        if len(watch_users) > 5:
            status.append(f"  ... 还有{len(watch_users)-5}个")

        yield event.plain_result("\n".join(status))

    @filter.command("微博测试")
    async def weibo_test(self, event: AstrMessageEvent):
        cookies = self._get_active_cookies()

        if not cookies:
            yield event.plain_result("❌ 未配置Cookie，请先使用 /微博登录 获取Cookie")
            return

        yield event.plain_result("🔄 正在测试微博连接...")

        watch_users = self._parse_multiline_config("watch_users")
        test_uid = watch_users[0].strip() if watch_users else "1195230310"

        posts = await self._fetch_user_posts(test_uid, cookies, True, True)
        if posts:
            post = posts[0]
            msg = f"✅ 微博连接成功！\n\n👤 用户: {post['username']}\n📝 最新微博:\n{post['text'][:150]}..."
            if post.get("is_retweet"):
                msg += f"\n🔄 (转发自 @{post.get('original_user', '')})"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(
                "❌ 所有API策略均失败\n\n"
                "可能原因：\n"
                "1. Cookie已过期，请使用 /微博登录 重新获取\n"
                "2. 微博账号被限制\n"
                "3. 服务器IP被限制"
            )

    @filter.command("微博登录")
    async def weibo_login(self, event: AstrMessageEvent):
        """触发自动登录获取Cookie"""
        yield event.plain_result("🔄 正在尝试自动登录微博获取Cookie...\n(需要服务器有playwright支持)")

        try:
            cookies = await self._auto_login_weibo()
            if cookies:
                self._cookie_manager.cookies = cookies
                self._cookie_manager.last_updated = datetime.now().isoformat()
                self._cookie_manager.expires_at = (datetime.now() + timedelta(days=7)).isoformat()
                self._save_cookies()

                yield event.plain_result("✅ Cookie获取成功！已自动保存。")
            else:
                yield event.plain_result(
                    "❌ 自动登录失败\n\n"
                    "可能原因：\n"
                    "1. 服务器未安装playwright: pip install playwright\n"
                    "2. 未安装浏览器: playwright install chromium\n"
                    "3. 需要手动配置Cookie\n\n"
                    "请检查日志获取更多信息。"
                )
        except Exception as e:
            logger.error(f"登录失败: {e}")
            yield event.plain_result(f"❌ 登录过程出错: {str(e)}")

    @filter.command("微博刷新Cookie")
    async def weibo_refresh_cookie(self, event: AstrMessageEvent):
        """手动刷新Cookie"""
        yield event.plain_result("🔄 正在刷新Cookie...")

        await self._refresh_cookies_auto()

        if self._cookie_manager.cookies:
            yield event.plain_result(
                f"✅ Cookie刷新成功！\n"
                f"更新时间: {self._cookie_manager.last_updated[:19] if self._cookie_manager.last_updated else '未知'}"
            )
        else:
            yield event.plain_result(
                "❌ Cookie刷新失败\n\n"
                "请使用 /微博登录 手动触发登录"
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
