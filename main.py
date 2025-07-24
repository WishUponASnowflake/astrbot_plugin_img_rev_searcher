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
from .ImgRevSearcher.model import BaseSearchModel
from pathlib import Path


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

    def _get_message_text(self, message) -> str:
        raw_message = getattr(message, 'raw_message', '')
        if isinstance(raw_message, str):
            return raw_message.strip()
        elif isinstance(raw_message, dict) and "message" in raw_message:
            texts = []
            for msg_part in raw_message.get("message", []):
                if msg_part.get("type") == "text":
                    texts.append(msg_part.get("data", {}).get("text", ""))
            return " ".join(texts).strip()
        return ''

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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        message_text = self._get_message_text(event.message_obj)
        img_urls = self._get_img_urls(event.message_obj)
        preloaded_img = None
        if img_urls:
            preloaded_img = await self._download_img(img_urls[0])

        if message_text.strip().startswith("以图搜图"):
            parts = message_text.strip().split()
            if user_id in self.user_states:
                return  # Avoid re-initializing if already in state

            if len(parts) > 1:
                engine = parts[1]
                if preloaded_img:
                    model = BaseSearchModel()
                    result_img = await model.search_and_draw(api=engine, file=preloaded_img.getvalue(), is_auto_save=False)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                        result_img.save(temp_file, format="JPEG", quality=85)
                        temp_file_path = temp_file.name
                    yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
                    if os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    event.stop_event()
                    return
                else:
                    self.user_states[user_id] = {
                        "step": "waiting_image",
                        "engine": engine,
                        "timestamp": time.time()
                    }
                    yield event.plain_result(f"使用引擎 {engine}。请在30秒内发送一张图片，我会进行搜索")
            else:
                self.user_states[user_id] = {
                    "step": "waiting_engine",
                    "timestamp": time.time(),
                    "preloaded_img": preloaded_img
                }
                workspace_root = Path(__file__).parent
                intro_path = workspace_root / "ImgRevSearcher" / "resource" / "img" / "engine_intro.jpg"
                with intro_path.open('rb') as f:
                    intro_content = f.read()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                    temp_file.write(intro_content)
                    temp_file_path = temp_file.name
                yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                if preloaded_img:
                    yield event.plain_result("图片已接收。请回复引擎名（如baidu），30秒内有效")
                else:
                    yield event.plain_result("请选择引擎（回复引擎名，如baidu），30秒内有效")
            event.stop_event()
            return

        if user_id not in self.user_states:
            return
        state = self.user_states[user_id]
        if time.time() - state["timestamp"] > 30:
            yield event.plain_result("等待超时，操作取消。")
            del self.user_states[user_id]
            event.stop_event()
            return

        if state["step"] == "waiting_engine":
            if message_text:
                state["engine"] = message_text
                if state.get("preloaded_img"):
                    preloaded_img = state["preloaded_img"]
                    model = BaseSearchModel()
                    result_img = await model.search_and_draw(api=state["engine"], file=preloaded_img.getvalue(), is_auto_save=False)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                        result_img.save(temp_file, format="JPEG", quality=85)
                        temp_file_path = temp_file.name
                    yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
                    if os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
                    del self.user_states[user_id]
                    event.stop_event()
                    return
                else:
                    state["step"] = "waiting_image"
                    state["timestamp"] = time.time()
                    yield event.plain_result(f"已选择引擎: {message_text}。请在30秒内发送一张图片，我会进行搜索")
            else:
                yield event.plain_result("请回复有效的引擎名。")
            event.stop_event()
            return

        if state["step"] != "waiting_image":
            return

        img_urls = self._get_img_urls(event.message_obj)
        if img_urls:
            img_buffer = await self._download_img(img_urls[0])
            if not img_buffer:
                yield event.plain_result("图片下载失败，请稍后重试。")
                del self.user_states[user_id]
                return
            model = BaseSearchModel()
            result_img = await model.search_and_draw(api=state["engine"], file=img_buffer.getvalue(), is_auto_save=False)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                result_img.save(temp_file, format="JPEG", quality=85)
                temp_file_path = temp_file.name
            yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
            del self.user_states[user_id]
            event.stop_event()
