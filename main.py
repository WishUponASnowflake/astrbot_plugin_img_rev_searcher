import asyncio
import io
import os
import re
import tempfile
import time
from typing import List, Optional, Dict, Any
from pathlib import Path
import httpx
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import Context, Star, register
from .ImgRevSearcher.model import BaseSearchModel

@register("astrbot_plugin_img_rev_seacher", "drdon1234", "以图搜图，找出处", "1.0")
class EchoImagePlugin(Star):
    TIMEOUT_SECONDS = 30
    CLEANUP_INTERVAL = 600
    SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
    AVAILABLE_ENGINES = ["anime_trace", "baidu", "base", "bing", "copyseeker", "ehentai", "google_lens", "saucenao", "tineye"]
    
    def __init__(self, context: Context):
        super().__init__(context)
        self.client = httpx.AsyncClient()
        self.user_states: Dict[str, Dict[str, Any]] = {}
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
    
    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            current_time = time.time()
            expired_users = [user_id for user_id, state in list(self.user_states.items()) if current_time - state['timestamp'] > self.TIMEOUT_SECONDS]
            for user_id in expired_users:
                del self.user_states[user_id]
    
    async def on_shutdown(self):
        await self.client.aclose()
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
    
    def _extract_image_urls(self, message) -> List[str]:
        img_urls = []
        for component in getattr(message, 'message', []):
            if "type='Image'" in str(component):
                url_match = re.search(r"url='([^']+)'", str(component))
                if url_match:
                    img_urls.append(url_match.group(1))
        raw_message = getattr(message, 'raw_message', '')
        if isinstance(raw_message, dict) and "message" in raw_message:
            for msg_part in raw_message.get("message", []):
                if msg_part.get("type") == "image":
                    url = msg_part.get("data", {}).get("url", "")
                    if url and url not in img_urls:
                        img_urls.append(url)
        return img_urls
    
    def _extract_text_content(self, message) -> str:
        raw_message = getattr(message, 'raw_message', '')
        if isinstance(raw_message, str):
            return raw_message.strip()
        if isinstance(raw_message, dict) and "message" in raw_message:
            texts = [msg_part.get("data", {}).get("text", "") for msg_part in raw_message.get("message", []) if msg_part.get("type") == "text"]
            return " ".join(texts).strip()
        return ''
    
    def _is_image_url(self, text: str) -> bool:
        return text.startswith("https://") and text.lower().endswith(self.SUPPORTED_EXTENSIONS)
    
    async def _download_image(self, url: str) -> Optional[io.BytesIO]:
        try:
            response = await self.client.get(url, timeout=15)
            if response.status_code == 200:
                return io.BytesIO(response.content)
        except Exception:
            pass
        return None
    
    async def _send_engine_intro(self, event: AstrMessageEvent):
        intro_path = Path(__file__).parent / "ImgRevSearcher/resource/img/engine_intro.jpg"
        with intro_path.open('rb') as f:
            intro_content = f.read()
        temp_file_path = await self._create_temp_file(intro_content)
        yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
        self._cleanup_temp_file(temp_file_path)
    
    async def _create_temp_file(self, content: bytes, suffix: str = ".jpg") -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(content)
            return temp_file.name
    
    def _cleanup_temp_file(self, file_path: str):
        if os.path.exists(file_path):
            os.unlink(file_path)
    
    async def _perform_search_and_respond(self, event: AstrMessageEvent, engine: str, image_data: bytes):
        model = BaseSearchModel()
        result_img = await model.search_and_draw(api=engine, file=image_data, is_auto_save=False)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            result_img.save(temp_file, format="JPEG", quality=85)
            temp_file_path = temp_file.name
        yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
        self._cleanup_temp_file(temp_file_path)
    
    def _is_state_expired(self, user_id: str) -> bool:
        if user_id not in self.user_states:
            return True
        return time.time() - self.user_states[user_id]["timestamp"] > self.TIMEOUT_SECONDS
    
    def _create_user_state(self, user_id: str, step: str, **kwargs) -> Dict[str, Any]:
        state = {"step": step, "timestamp": time.time(), **kwargs}
        self.user_states[user_id] = state
        return state
    
    def _parse_search_command(self, message_text: str) -> tuple[Optional[str], Optional[str]]:
        parts = message_text.strip().split()
        engine = None
        url_from_text = None
        if len(parts) > 1:
            if self._is_image_url(parts[1]):
                url_from_text = parts[1]
            elif parts[1] in self.AVAILABLE_ENGINES:
                engine = parts[1]
                if len(parts) > 2 and self._is_image_url(parts[2]):
                    url_from_text = parts[2]
            else:
                return parts[1], None
        return engine, url_from_text
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        message_text = self._extract_text_content(event.message_obj)
        img_urls = self._extract_image_urls(event.message_obj)
        if message_text.strip().startswith("以图搜图"):
            await self._handle_new_search_request(event, user_id, message_text, img_urls)
            return
        await self._handle_existing_state(event, user_id, message_text, img_urls)
    
    async def _handle_new_search_request(self, event: AstrMessageEvent, user_id: str, message_text: str, img_urls: List[str]):
        self.user_states.pop(user_id, None)
        engine, url_from_text = self._parse_search_command(message_text)
        if isinstance(engine, str) and engine not in self.AVAILABLE_ENGINES:
            self._create_user_state(user_id, "waiting_engine", preloaded_img=None)
            yield event.plain_result(f"引擎 '{engine}' 不存在，请提供有效的引擎名")
            async for result in self._send_engine_intro(event):
                yield result
            yield event.plain_result("请选择有效的引擎（回复引擎名，如baidu），30秒内有效")
            event.stop_event()
            return
        preloaded_img = None
        if img_urls:
            preloaded_img = await self._download_image(img_urls[0])
        elif url_from_text:
            preloaded_img = await self._download_image(url_from_text)
        if engine and preloaded_img:
            async for result in self._perform_search_and_respond(event, engine, preloaded_img.getvalue()):
                yield result
        else:
            self._create_user_state(user_id, "waiting_engine", preloaded_img=preloaded_img)
            async for result in self._send_engine_intro(event):
                yield result
            if preloaded_img:
                yield event.plain_result("图片已接收，请回复引擎名（如baidu），30秒内有效")
            else:
                yield event.plain_result("请选择引擎（回复引擎名，如baidu），30秒内有效")
        event.stop_event()
    
    async def _handle_existing_state(self, event: AstrMessageEvent, user_id: str, message_text: str, img_urls: List[str]):
        if user_id not in self.user_states:
            return
        if self._is_state_expired(user_id):
            yield event.plain_result("等待超时，操作取消")
            del self.user_states[user_id]
            event.stop_event()
            return
        state = self.user_states[user_id]
        if state["step"] == "waiting_engine":
            await self._handle_engine_selection(event, user_id, message_text, state)
        elif state["step"] == "waiting_image":
            await self._handle_image_input(event, user_id, message_text, img_urls, state)
    
    async def _handle_engine_selection(self, event: AstrMessageEvent, user_id: str, message_text: str, state: Dict[str, Any]):
        if not message_text or message_text not in self.AVAILABLE_ENGINES:
            yield event.plain_result(f"引擎 '{message_text}' 不存在，请回复有效的引擎名")
            event.stop_event()
            return
        state["engine"] = message_text
        if state.get("preloaded_img"):
            preloaded_img = state["preloaded_img"]
            async for result in self._perform_search_and_respond(event, message_text, preloaded_img.getvalue()):
                yield result
            del self.user_states[user_id]
        else:
            state["step"] = "waiting_image"
            state["timestamp"] = time.time()
            yield event.plain_result(f"已选择引擎: {message_text}，请在30秒内发送一张图片，我会进行搜索")
        event.stop_event()
    
    async def _handle_image_input(self, event: AstrMessageEvent, user_id: str, message_text: str, img_urls: List[str], state: Dict[str, Any]):
        img_buffer = None
        if img_urls:
            img_buffer = await self._download_image(img_urls[0])
        elif self._is_image_url(message_text):
            img_buffer = await self._download_image(message_text)
        if img_buffer:
            async for result in self._perform_search_and_respond(event, state["engine"], img_buffer.getvalue()):
                yield result
            del self.user_states[user_id]
            event.stop_event()
        else:
            yield event.plain_result("请发送一张图片或图片URL")
