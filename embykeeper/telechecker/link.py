import asyncio
import random
import time
from typing import Callable, Coroutine, List, Optional, Tuple, Union
import uuid

import tomli
from loguru import logger
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from pyrogram.raw.functions.account import GetNotifySettings
from pyrogram.raw.types import PeerNotifySettings, InputNotifyPeer
from pyrogram.errors.exceptions.bad_request_400 import YouBlockedUser
from pyrogram.errors import FloodWait

from ..utils import async_partial, truncate_str
from .lock import account_status, account_status_lock
from .tele import Client


class LinkError(Exception):
    pass


class Link:
    """云服务类, 用于认证和高级权限任务通讯."""

    bot = "embykeeper_auth_bot"

    def __init__(self, client: Client):
        self.client = client
        self.log = logger.bind(scheme="telelink", username=client.me.name)

    @property
    def instance(self):
        """当前设备识别码."""
        rd = random.Random()
        rd.seed(uuid.getnode())
        return uuid.UUID(int=rd.getrandbits(128))

    async def delete_messages(self, messages: List[Message]):
        """删除一系列消息."""

        async def delete(m: Message):
            try:
                await m.delete(revoke=True)
                text = m.text or m.caption or "图片或其他内容"
                text = truncate_str(text.replace("\n", ""), 30)
                self.log.debug(f"[gray50]删除了 API 消息记录: {text}[/]")
            except asyncio.CancelledError:
                pass

        return await asyncio.gather(*[delete(m) for m in messages])

    async def post(
        self,
        cmd,
        photo=None,
        condition: Callable = None,
        timeout: int = 20,
        retries=3,
        name: str = None,
        fail: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        向机器人发送请求.
        参数:
            cmd: 命令字符串
            condition: 布尔或函数, 参数为响应 toml 的字典形式, 决定该响应是否为有效响应.
            timeout: 超时 (s)
            retries: 最大重试次数
            name: 请求名称, 用于用户提示
            fail: 当出现错误时抛出错误, 而非发送日志
        """
        for r in range(retries):
            self.log.debug(f"[gray50]禁用提醒 {timeout} 秒: {self.bot}[/]")
            peer = InputNotifyPeer(peer=await self.client.resolve_peer(self.bot))
            settings: PeerNotifySettings = await self.client.invoke(GetNotifySettings(peer=peer))
            old_mute_until = settings.mute_until
            try:
                await self.client.mute_chat(self.bot, time.time() + timeout + 5)
            except FloodWait:
                self.log.debug(f"[gray50]设置禁用提醒因访问超限而失败: {self.bot}[/]")
            try:
                future = asyncio.Future()
                handler = MessageHandler(
                    async_partial(self._handler, cmd=cmd, future=future, condition=condition),
                    filters.text & filters.bot & filters.user(self.bot),
                )
                await self.client.add_handler(handler, group=1)
                try:
                    messages = []
                    messages.append(await self.client.send_message(self.bot, f"/start quiet"))
                    await asyncio.sleep(0.5)
                    if photo:
                        messages.append(await self.client.send_photo(self.bot, photo, cmd))
                    else:
                        messages.append(await self.client.send_message(self.bot, cmd))
                    self.log.debug(f"[gray50]-> {cmd}[/]")
                    results = await asyncio.wait_for(future, timeout=timeout)
                except asyncio.CancelledError:
                    try:
                        await asyncio.wait_for(self.delete_messages(messages), 1.0)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        raise
                except asyncio.TimeoutError:
                    await self.delete_messages(messages)
                    if r + 1 < retries:
                        self.log.info(f"{name}超时 ({r + 1}/{retries}), 将在 3 秒后重试.")
                        await asyncio.sleep(3)
                        continue
                    else:
                        msg = f"{name}超时 ({r + 1}/{retries})."
                        if fail:
                            raise LinkError(msg)
                        else:
                            self.log.warning(msg)
                            return None
                except YouBlockedUser:
                    msg = "您在账户中禁用了用于 API 信息传递的 Bot: @embykeeper_auth_bot, 这将导致 embykeeper 无法运行, 请尝试取消禁用."
                    if fail:
                        raise LinkError(msg)
                    else:
                        self.log.error(msg)
                        return None
                else:
                    await self.delete_messages(messages)
                    status, errmsg = [results.get(p, None) for p in ("status", "errmsg")]
                    if status == "error":
                        if fail:
                            raise LinkError(f"{errmsg}.")
                        else:
                            self.log.warning(f"{name}错误: {errmsg}.")
                            return False
                    elif status == "ok":
                        return results
                    else:
                        if fail:
                            raise LinkError("出现未知错误.")
                        else:
                            self.log.warning(f"{name}出现未知错误.")
                            return False
                finally:
                    await self.client.remove_handler(handler, group=1)
            finally:
                if old_mute_until:
                    try:
                        await self.client.mute_chat(self.bot, until=old_mute_until)
                    except asyncio.TimeoutError:
                        self.log.debug(f"[gray50]重新设置通知设置失败: {self.bot}[/]")
                    except FloodWait:
                        self.log.debug(f"[gray50]重新设置通知设置因访问超限而失败: {self.bot}[/]")
                    else:
                        self.log.debug(f"[gray50]重新设置通知设置成功: {self.bot}[/]")

    async def _handler(
        self,
        client: Client,
        message: Message,
        cmd: str,
        future: asyncio.Future,
        condition: Union[bool, Callable[..., Coroutine], Callable] = None,
    ):
        try:
            toml = tomli.loads(message.text)
        except tomli.TOMLDecodeError:
            self.delete_messages([message])
        else:
            try:
                if toml.get("command", None) == cmd:
                    if condition is None:
                        cond = True
                    elif asyncio.iscoroutinefunction(condition):
                        cond = await condition(toml)
                    elif callable(condition):
                        cond = condition(toml)
                    if cond:
                        future.set_result(toml)
                        await asyncio.sleep(0.5)
                        await self.delete_messages([message])
                        return
            except asyncio.CancelledError as e:
                try:
                    await asyncio.wait_for(self.delete_messages([message]), 1)
                except asyncio.TimeoutError:
                    pass
                finally:
                    future.set_exception(e)
            finally:
                message.continue_propagation()

    async def auth(self, service: str, log_func=None):
        """向机器人发送授权请求."""
        if not log_func:
            result = await self.post(f"/auth {service} {self.instance}", name=f"服务 {service.upper()} 认证")
            return bool(result)
        else:
            try:
                await self.post(
                    f"/auth {service} {self.instance}", name=f"服务 {service.upper()} 认证", fail=True
                )
            except LinkError as e:
                log_func(f"初始化错误: 使用 {service.upper()} 服务, 但{e}")
                if "权限不足" in str(e):
                    await self._show_super_ad()
                return False
            else:
                return True

    async def _show_super_ad(self):
        async with account_status_lock:
            super_ad_shown = account_status.get(self.client.me.id, {}).get("super_ad_shown", False)
            if not super_ad_shown:
                self.log.info("请访问 https://go.zetx.tech/eksuper 赞助项目以升级为高级用户, 尊享更多功能.")
                if self.client.me.id in account_status:
                    account_status[self.client.me.id]["super_ad_shown"] = True
                else:
                    account_status[self.client.me.id] = {"super_ad_shown": True}
                return True
            else:
                return False

    async def captcha(self, site: str):
        """向机器人发送验证码解析请求."""
        results = await self.post(f"/captcha {self.instance} {site}", timeout=240, name="请求跳过验证码")
        if results:
            return results.get("token", None)
        else:
            return None

    async def captcha_url(self, site: str, url: str):
        """向机器人发送带验证码的远程网页解析请求."""
        results = await self.post(
            f"/captcha_url {self.instance} {site} {url}", timeout=240, name="请求跳过验证码"
        )
        if results:
            return results.get("result", None)
        else:
            return None

    async def pornemby_answer(self, question: str):
        """向机器人发送问题回答请求."""
        results = await self.post(
            f"/pornemby_answer {self.instance} {question}", timeout=20, name="请求问题回答"
        )
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def terminus_answer(self, question: str):
        """向机器人发送问题回答请求."""
        results = await self.post(
            f"/terminus_answer {self.instance} {question}", timeout=20, name="请求问题回答"
        )
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def gpt(self, prompt: str):
        """向机器人发送智能回答请求."""
        results = await self.post(f"/gpt {self.instance} {prompt}", timeout=20, name="请求智能回答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def visual(self, photo, options: List[str], question=None):
        """向机器人发送视觉问题解答请求."""
        cmd = f"/visual {self.instance} {'/'.join(options)}"
        if question:
            cmd += f" {question}"
        results = await self.post(cmd, photo=photo, timeout=20, name="请求视觉问题解答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def ocr(self, photo):
        """向机器人发送 OCR 解答请求."""
        cmd = f"/ocr {self.instance}"
        results = await self.post(cmd, photo=photo, timeout=20, name="请求验证码解答")
        if results:
            return results.get("answer", None)
        else:
            return None

    async def send_log(self, message):
        """向机器人发送日志记录请求."""
        results = await self.post(f"/log {self.instance} {message}", name="发送日志到 Telegram")
        return bool(results)
