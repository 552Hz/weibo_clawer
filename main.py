"""
微博监控插件
功能：
1. 监控指定微博用户的新动态
2. 定时爬取并推送到指定群聊
3. 支持图片预览
"""

import asyncio
import json
import re
import os
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


class WeiboMonitor(Star):
    """微博监控插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.name = "微博监控"

        # 数据存储路径
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_weibo"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir

        # 存储上次爬取的状态
        self.state_file = data_dir / "state.json"
        self.last_post_ids: dict[str, str] = {}  # {uid: last_post_id}
        self._load_state()

        # 爬取锁，防止并发
        self._fetch_lock = False

        # 启动定时任务
        self._task: Optional[asyncio.Task] = None
        logger.info("微博监控插件已加载")

    def _load_state(self):
        """加载上次状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.last_post_ids = data.get("last_post_ids", {})
                logger.info(f"已加载 {len(self.last_post_ids)} 个用户的监控状态")
            except Exception as e:
                logger.error(f"加载状态失败: {e}")

    def _save_state(self):
        """保存状态"""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump({"last_post_ids": self.last_post_ids}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    async def on_astrbot_loaded(self):
        """AstrBot加载完成后启动定时任务"""
        interval = self.config.get("check_interval", 10)
        self._task = asyncio.create_task(self._monitor_loop(interval * 60))
        logger.info(f"微博监控已启动，检查间隔: {interval} 分钟")

    async def terminate(self):
        """插件卸载时停止任务"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save_state()
        logger.info("微博监控插件已卸载")

    async def _monitor_loop(self, interval_seconds: int):
        """定时监控循环"""
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await self._check_new_posts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"监控循环出错: {e}")
                await asyncio.sleep(60)  # 出错后等待1分钟再试

    async def _check_new_posts(self):
        """检查所有监控用户的新动态"""
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
                    new_posts = await self._fetch_user_posts(uid, cookies, skip_top)
                    if new_posts:
                        for post in new_posts:
                            await self._send_to_groups(post, target_groups)
                            await asyncio.sleep(2)  # 避免发送太快
                except Exception as e:
                    logger.error(f"获取用户 {uid} 动态失败: {e}")

        finally:
            self._fetch_lock = False

    async def _fetch_user_posts(self, uid: str, cookies: str, skip_top: bool) -> list[dict]:
        """获取用户最新微博动态"""
        headers = {
            "Cookie": cookies,
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "MWeibo-Pwa": "1",
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

        async with aiohttp.ClientSession() as session:
            # 使用微博API获取用户最新微博
            url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}&page=1"

            async with session.get(url, headers=headers, timeout=30, allow_redirects=True) as resp:
                content_type = resp.headers.get('Content-Type', '')

                # 检查是否是登录页面
                if 'text/html' in content_type or resp.status == 200:
                    text = await resp.text()
                    if '登录' in text or 'login' in text.lower() or '<!DOCTYPE html>' in text[:50]:
                        logger.warning("Cookie无效，微博返回登录页面")
                        return []

                if resp.status != 200:
                    logger.error(f"请求失败，状态码: {resp.status}")
                    return []

                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    logger.error(f"JSON解析失败，响应: {text[:200]}")
                    return []

                if data.get("ok") != 1:
                    logger.warning(f"获取用户 {uid} 动态失败: {data}")
                    return []

                cards = data.get("data", {}).get("cards", [])
                new_posts = []

                for card in cards:
                    if card.get("card_type") != 9:  # 9 = 微博动态卡片
                        continue

                    mblog = card.get("mblog", {})
                    post_id = mblog.get("id", "")

                    # 跳过置顶微博
                    if skip_top and mblog.get("isTop"):
                        continue

                    # 检查是否已推送过
                    if uid in self.last_post_ids and self.last_post_ids[uid] == post_id:
                        break  # 之后的都是更旧的

                    # 解析微博内容
                    post_data = self._parse_weibo_post(mblog)
                    if post_data:
                        new_posts.append(post_data)

                # 更新状态
                if cards:
                    first_mblog = None
                    for card in cards:
                        if card.get("card_type") == 9:
                            first_mblog = card.get("mblog", {})
                            break

                    if first_mblog:
                        self.last_post_ids[uid] = first_mblog.get("id", "")
                        self._save_state()

                return new_posts

    def _parse_weibo_post(self, mblog: dict) -> Optional[dict]:
        """解析微博内容"""
        try:
            # 获取文本内容
            text = mblog.get("text", "")
            # 去除HTML标签
            text = re.sub(r'<[^>]+>', '', text)
            text = text.strip()

            if not text:
                return None

            # 获取用户信息
            user = mblog.get("user", {})
            username = user.get("screen_name", "未知用户")

            # 获取图片
            pics = mblog.get("pics", [])
            image_url = None
            if pics:
                # 获取第一张图片
                first_pic = pics[0]
                if isinstance(first_pic, dict):
                    image_url = first_pic.get("large", {}).get("url") or first_pic.get("url")
                elif isinstance(first_pic, str):
                    image_url = first_pic

            # 获取发布时间
            created_at = mblog.get("created_at", "")
            post_url = f"https://weibo.com/{user.get('id', '')}/{mblog.get('bid', '')}"

            return {
                "username": username,
                "uid": user.get("id", ""),
                "text": text[:500],  # 限制长度
                "image_url": image_url,
                "created_at": created_at,
                "url": post_url,
            }
        except Exception as e:
            logger.error(f"解析微博失败: {e}")
            return None

    async def _send_to_groups(self, post: dict, groups: list):
        """发送动态到群聊"""
        message = self._format_post_message(post)

        # 先发送文字
        chain = [{"type": "Plain", "text": message}]

        # 如果有图片，下载并发送
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
                await self.context.send_message(
                    f"group_{group_id}",
                    message_chain
                )
                logger.info(f"已推送动态到群 {group_id}")
            except Exception as e:
                logger.error(f"发送到群 {group_id} 失败: {e}")

    async def _download_image(self, url: str) -> Optional[str]:
        """下载图片到本地"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as resp:
                    if resp.status != 200:
                        return None

                    content = await resp.read()

                    # 压缩图片
                    img = Image.open(BytesIO(content))
                    if img.mode == "RGBA":
                        img = img.convert("RGB")

                    # 保存到数据目录
                    filename = f"weibo_image_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
                    save_path = self.data_dir / filename

                    # 压缩并保存
                    img.save(save_path, "JPEG", quality=85, optimize=True)

                    return str(save_path)
        except Exception as e:
            logger.error(f"下载图片出错: {e}")
            return None

    def _format_post_message(self, post: dict) -> str:
        """格式化推送消息"""
        msg = [
            "【微博更新】",
            "",
            f"👤 {post['username']}",
            "",
            f"{post['text']}",
            "",
            f"🔗 {post['url']}",
        ]
        return "\n".join(msg)

    def _parse_multiline_config(self, key: str) -> list[str]:
        """解析多行配置"""
        value = self.config.get(key, "")
        if not value:
            return []
        lines = [line.strip() for line in value.strip().split('\n')]
        return [line for line in lines if line]

    # ============ 指令 ============

    @filter.command("微博监控")
    async def weibo_help(self, event: AstrMessageEvent):
        """微博监控帮助"""
        help_text = (
            "【微博监控插件】使用说明\n\n"
            "📌 基础指令：\n"
            "/微博状态 - 查看当前监控状态\n"
            "/微博测试 - 测试微博连接\n"
            "/微博推送 - 手动触发一次推送检查\n"
            "/微博监控 - 显示此帮助信息\n\n"
            "⚙️ 配置说明：\n"
            "在插件设置中配置以下参数：\n"
            "• cookies - 微博登录Cookie\n"
            "• watch_users - 要监控的用户UID\n"
            "• target_groups - 推送目标群ID\n"
            "• check_interval - 检查间隔(分钟)"
        )
        yield event.plain_result(help_text)

    @filter.command("微博状态")
    async def weibo_status(self, event: AstrMessageEvent):
        """查看监控状态"""
        cookies = self.config.get("cookies", "").strip()
        watch_users = self._parse_multiline_config("watch_users")
        target_groups = self._parse_multiline_config("target_groups")
        interval = self.config.get("check_interval", 10)
        skip_top = self.config.get("skip_top_post", True)

        status = [
            "【微博监控状态】",
            "",
            f"📡 Cookie状态: {'已配置' if cookies else '未配置 ❌'}",
            f"👥 监控用户数: {len(watch_users)}",
            f"💬 推送群聊数: {len(target_groups)}",
            f"⏰ 检查间隔: {interval} 分钟",
            f"🔝 跳过置顶: {'是' if skip_top else '否'}",
            "",
            "📋 监控用户:",
        ]

        if watch_users:
            for uid in watch_users[:10]:  # 最多显示10个
                status.append(f"  • {uid}")
            if len(watch_users) > 10:
                status.append(f"  ... 还有 {len(watch_users) - 10} 个")
        else:
            status.append("  暂无")

        yield event.plain_result("\n".join(status))

    @filter.command("微博测试")
    async def weibo_test(self, event: AstrMessageEvent):
        """测试微博连接"""
        cookies = self.config.get("cookies", "").strip()

        if not cookies:
            yield event.plain_result("❌ 未配置微博Cookie，请先在插件设置中配置")
            return

        yield event.plain_result("🔄 正在测试微博连接...")

        try:
            watch_users = self._parse_multiline_config("watch_users")
            test_uid = watch_users[0].strip() if watch_users else "1195230310"

            headers = {
                "Cookie": cookies,
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                "MWeibo-Pwa": "1",
                "Referer": "https://m.weibo.cn/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            async with aiohttp.ClientSession() as session:
                url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={test_uid}&containerid=107603{test_uid}"
                async with session.get(url, headers=headers, timeout=15, allow_redirects=True) as resp:
                    content_type = resp.headers.get('Content-Type', '')

                    # 检查是否是登录页面
                    if 'text/html' in content_type:
                        text = await resp.text()
                        if '登录' in text or 'login' in text.lower():
                            yield event.plain_result("❌ Cookie无效！微博返回登录页面，请重新获取Cookie")
                            return

                    if resp.status != 200:
                        yield event.plain_result(f"❌ 请求失败，状态码: {resp.status}")
                        return

                    try:
                        data = await resp.json()
                    except Exception:
                        yield event.plain_result("❌ 响应格式错误，无法解析JSON")
                        return

                    if data.get("ok") == 1:
                        cards = data.get("data", {}).get("cards", [])
                        for card in cards:
                            if card.get("card_type") == 9:
                                user = card.get("mblog", {}).get("user", {})
                                text_preview = card.get("mblog", {}).get("text", "")[:100]
                                text_preview = re.sub(r'<[^>]+>', '', text_preview)
                                yield event.plain_result(
                                    f"✅ 微博连接成功！\n\n"
                                    f"👤 用户: {user.get('screen_name', '未知')}\n"
                                    f"📝 最新微博预览:\n{text_preview}..."
                                )
                                return

                        yield event.plain_result("⚠️ 用户暂无微博动态")
                    else:
                        error_msg = data.get("msg", "未知错误")
                        yield event.plain_result(f"⚠️ API返回错误: {error_msg}")

        except asyncio.TimeoutError:
            yield event.plain_result("❌ 连接超时，请检查网络或Cookie")
        except Exception as e:
            yield event.plain_result(f"❌ 测试失败: {e}")

    @filter.command("微博推送")
    async def weibo_push(self, event: AstrMessageEvent):
        """手动触发推送检查"""
        if self._fetch_lock:
            yield event.plain_result("⚠️ 正在执行中，请稍后...")
            return

        yield event.plain_result("🔄 正在检查新动态...")

        # 在新任务中执行，不阻塞
        asyncio.create_task(self._manual_push())

        yield event.plain_result("✅ 已开始检查，结果将陆续推送")

    async def _manual_push(self):
        """手动推送执行"""
        await self._check_new_posts()
        logger.info("手动推送检查完成")


def get_astrbot_data_path():
    """获取AstrBot数据路径的兼容函数"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path as _get
        return _get()
    except ImportError:
        return Path("./data")
