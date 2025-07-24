import asyncio
import io
import os
import re
import tempfile
import time
from typing import List
import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import Context, Star, register
from ImgRevSearcher.model import BaseSearchModel


@register("astrbot_plugin_img_rev_seacher", "drdon1234", "以图搜图，找出处", "1.0")
class EchoImagePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.client = httpx.AsyncClient()
        self.user_states = {}
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(600)
            now = time.time()
            to_delete = [user_id for user_id, state in list(self.user_states.items()) if now - state['timestamp'] > 30]
            for user_id in to_delete:
                del self.user_states[user_id]

    async def on_shutdown(self):
        await self.client.aclose()
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()

    def _get_img_urls(self, message) -> List[str]:
        img_urls = []
        for component_str in getattr(message, 'message', []):
            if "type='Image'" in str(component_str):
                url_match = re.search(r"url='([^']+)'", str(component_str))
                if url_match:
                    img_urls.append(url_match.group(1))
        raw_message = getattr(message, 'raw_message', '')
        if isinstance(raw_message, dict) and "message" in raw_message:
            for msg_part in raw_message.get("message", []):
                if msg_part.get("type") == "image":
                    data = msg_part.get("data", {})
                    url = data.get("url", "")
                    if url and url not in img_urls:
                        img_urls.append(url)
        return img_urls

    async def _download_img(self, url: str):
        try:
            r = await self.client.get(url, timeout=15)
            if r.status_code == 200:
                return io.BytesIO(r.content)
        except:
            pass
        return None

    async def get_imgs(self, img_urls: List[str]) -> List[io.BytesIO]:
        if not img_urls:
            return []
        imgs = await asyncio.gather(*[self._download_img(url) for url in img_urls])
        return [img for img in imgs if img is not None]

    @filter.command("以图搜图")
    async def start_search(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        self.user_states[user_id] = {
            "step": "waiting_image",
            "timestamp": time.time()
        }
        yield event.plain_result("请在30秒内发送一张图片，我会进行搜索")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        if user_id not in self.user_states:
            return
        state = self.user_states[user_id]
        if state["step"] != "waiting_image":
            return
        if time.time() - state["timestamp"] > 30:
            yield event.plain_result("等待图片超时，操作取消。")
            del self.user_states[user_id]
            event.stop_event()
            return
        img_urls = self._get_img_urls(event.message_obj)
        if img_urls:
            img_buffer = await self._download_img(img_urls[0])
            if not img_buffer:
                yield event.plain_result("图片下载失败，请稍后重试。")
                del self.user_states[user_id]
                return
            model = BaseSearchModel()
            result_img = await model.search_and_draw(api="baidu", file=img_buffer.getvalue(), is_auto_save=False)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                result_img.save(temp_file, format="JPEG", quality=85)
                temp_file_path = temp_file.name
            yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
            del self.user_states[user_id]
            event.stop_event()
