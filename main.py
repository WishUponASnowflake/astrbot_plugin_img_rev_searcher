import asyncio
import io
import os
import re
import tempfile
import time
from typing import List
import httpx
from PIL import Image
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as AstrImage, Nodes, Node, Plain
from astrbot.api.star import Context, Star, register
from pathlib import Path
from .ImgRevSearcher.model import BaseSearchModel
from PIL import ImageDraw, ImageFont


@register("astrbot_plugin_img_rev_seacher", "drdon1234", "以图搜图，找出处", "1.0")
class ImgRevSearcherPlugin(Star):
    def __init__(self, context: Context, config: dict):
        """
        初始化插件

        参数:
            context: Astrbot上下文对象
            config: 配置文件中的配置
        """
        super().__init__(context)
        self.client = httpx.AsyncClient()
        self.user_states = {}
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())
        available_apis_config = config.get("available_apis", {})
        self.available_engines = []
        all_engines = ["animetrace", "baidu", "bing", "copyseeker", "ehentai", "google", "saucenao", "tineye"]
        for engine in all_engines:
            if available_apis_config.get(engine, True):
                self.available_engines.append(engine)
        proxies = config.get("proxies", "")
        default_params = config.get("default_params", {})
        default_cookies = config.get("default_cookies", {})
        self.search_model = BaseSearchModel(
            proxies=proxies,
            timeout=60,
            default_params=default_params,
            default_cookies=default_cookies
        )

    def _split_text_by_length(self, text: str, max_length: int = 4000) -> List[str]:
        """
        将文本按最大长度分割，尽量在50个连续短横线处截断

        参数:
            text: 要分割的文本
            max_length: 每段最大长度

        返回:
            List[str]: 分割后的文本片段列表
        """
        if len(text) <= max_length:
            return [text]
        separator = "-" * 50
        result = []
        while text:
            if len(text) <= max_length:
                result.append(text)
                break
            cut_index = max_length
            separator_index = text.rfind(separator, 0, max_length)
            if separator_index != -1 and separator_index > max_length // 2:
                cut_index = separator_index + len(separator)
            result.append(text[:cut_index])
            text = text[cut_index:]
        return result

    async def cleanup_loop(self):
        """
        清理循环任务

        定期清理过期的用户状态
        """
        while True:
            await asyncio.sleep(600)
            now = time.time()
            to_delete = [user_id for user_id, state in list(self.user_states.items()) if now - state['timestamp'] > 30]
            for user_id in to_delete:
                del self.user_states[user_id]

    async def terminate(self):
        """
        终止插件

        关闭HTTP客户端并取消清理任务
        """
        await self.client.aclose()
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()

    def _get_img_urls(self, message) -> List[str]:
        """
        从消息中提取图像URL

        参数:
            message: 消息对象

        返回:
            List[str]: 图像URL列表
        """
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
        """
        从消息中提取文本内容

        参数:
            message: 消息对象

        返回:
            str: 提取的文本
        """
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

    def _is_image_url(self, text: str) -> bool:
        """
        检查文本是否为图像URL

        参数:
            text: 要检查的文本

        返回:
            bool: 如果是图像URL返回True，否则False
        """
        return text.startswith("https://") and text.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))

    async def _download_img(self, url: str):
        """
        下载图像

        参数:
            url: 图像URL

        返回:
            io.BytesIO: 下载的图像数据，或None如果失败
        """
        try:
            r = await self.client.get(url, timeout=15)
            if r.status_code == 200:
                return io.BytesIO(r.content)
        except:
            pass
        return None

    async def get_imgs(self, img_urls: List[str]) -> List[io.BytesIO]:
        """
        下载多个图像

        参数:
            img_urls: 图像URL列表

        返回:
            List[io.BytesIO]: 下载的图像数据列表
        """
        if not img_urls:
            return []
        imgs = await asyncio.gather(*[self._download_img(url) for url in img_urls])
        return [img for img in imgs if img is not None]

    async def _send_image(self, event: AstrMessageEvent, content: bytes):
        """
        发送图像

        参数:
            event: 消息事件
            content: 图像内容

        产生:
            图像消息结果
        """
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(content)
            temp_file_path = temp_file.name
        yield event.chain_result([AstrImage.fromFileSystem(temp_file_path)])
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)

    async def _send_engine_intro(self, event: AstrMessageEvent):
        """
        发送搜索引擎介绍图像

        参数:
            event: 消息事件

        产生:
            介绍图像消息结果
        """
        engine_info = {
            "animetrace": {
                "url": "https://www.animetrace.com/",
                "anime": True
            },
            "baidu": {
                "url": "https://graph.baidu.com/",
                "anime": False
            },
            "bing": {
                "url": "https://www.bing.com/images/search",
                "anime": False
            },
            "copyseeker": {
                "url": "https://copyseeker.net/",
                "anime": False
            },
            "ehentai": {
                "url": "https://e-hentai.org/",
                "anime": True
            },
            "google": {
                "url": "https://lens.google.com/",
                "anime": False
            },
            "saucenao": {
                "url": "https://saucenao.com/",
                "anime": True
            },
            "tineye": {
                "url": "https://tineye.com/search/",
                "anime": False
            }
        }
        
        colors = {
            "bg": (255, 255, 255),
            "header_bg": (67, 99, 216),
            "header_text": (255, 255, 255),
            "table_header": (240, 242, 245),
            "cell_bg_even": (250, 250, 252),
            "cell_bg_odd": (255, 255, 255),
            "border": (180, 185, 195), # 使用与外边框相同的颜色
            "text": (50, 50, 50),
            "url": (41, 98, 255),
            "success": (76, 175, 80),
            "fail": (244, 67, 54),
            "shadow": (0, 0, 0, 30),
            "hint": (100, 100, 100)
        }
        width = 800
        cell_height = 50
        header_height = 60
        title_height = 70
        table_height = header_height + cell_height * len(self.available_engines)
        height = title_height + table_height + 25
        border_width = 2
        def rounded_rectangle(draw, xy, radius, fill=None, outline=None, width=1):
            x1, y1, x2, y2 = xy
            diameter = 2 * radius
            draw.rectangle([x1+radius, y1, x2-radius, y2], fill=fill, outline=outline, width=width)
            draw.rectangle([x1, y1+radius, x2, y2-radius], fill=fill, outline=outline, width=width)
            draw.pieslice([x1, y1, x1+diameter, y1+diameter], 180, 270, fill=fill, outline=outline, width=width)
            draw.pieslice([x2-diameter, y1, x2, y1+diameter], 270, 360, fill=fill, outline=outline, width=width)
            draw.pieslice([x1, y2-diameter, x1+diameter, y2], 90, 180, fill=fill, outline=outline, width=width)
            draw.pieslice([x2-diameter, y2-diameter, x2, y2], 0, 90, fill=fill, outline=outline, width=width)
        img = Image.new('RGB', (width, height), colors["bg"])
        draw = ImageDraw.Draw(img)
        workspace_root = Path(__file__).parent
        try:
            font_path = str(workspace_root / "ImgRevSearcher/resource/font/arialuni.ttf")
            title_font = ImageFont.truetype(font_path, 24)
            header_font = ImageFont.truetype(font_path, 18)
            body_font = ImageFont.truetype(font_path, 16)
        except Exception:
            title_font = ImageFont.load_default()
            header_font = ImageFont.load_default()
            body_font = ImageFont.load_default()
        rounded_rectangle(draw, [20, 15, width-20, title_height-5], 10, fill=colors["header_bg"])
        title = "可用搜索引擎"
        title_width = draw.textlength(title, font=title_font) if hasattr(draw, 'textlength') else title_font.getsize(title)[0]
        title_x = (width - title_width) // 2
        draw.text((title_x, 25), title, font=title_font, fill=colors["header_text"])
        table_x = 20
        table_width = width - 40
        col_widths = [int(table_width * 0.20), int(table_width * 0.50), int(table_width * 0.30)]
        table_y = title_height + 10
        table_bottom = table_y + header_height + cell_height * len(self.available_engines)
        draw.rectangle([table_x, table_y, table_x + sum(col_widths), table_y + header_height], 
                     fill=colors["table_header"])
        y = table_y + header_height
        for idx, engine in enumerate(self.available_engines):
            if engine not in engine_info:
                continue
            row_bg = colors["cell_bg_even"] if idx % 2 == 0 else colors["cell_bg_odd"]
            draw.rectangle([table_x, y, table_x + sum(col_widths), y + cell_height], 
                         fill=row_bg)
            y += cell_height
        headers = ["引擎", "网址", "二次元图片专用"]
        x = table_x
        for i, header in enumerate(headers):
            text_width = draw.textlength(header, font=header_font) if hasattr(draw, 'textlength') else header_font.getsize(header)[0]
            text_x = x + (col_widths[i] - text_width) // 2
            draw.text((text_x, table_y + (header_height - 18) // 2), header, font=header_font, fill=colors["text"])
            x += col_widths[i]
        y = table_y + header_height
        for idx, engine in enumerate(self.available_engines):
            if engine not in engine_info:
                continue
            info = engine_info[engine]
            x = table_x
            draw.text((x + 15, y + (cell_height - 16) // 2), engine, font=body_font, fill=colors["text"])
            x += col_widths[0]
            draw.text((x + 15, y + (cell_height - 16) // 2), info["url"], font=body_font, fill=colors["url"])
            x += col_widths[1]
            mark = "✓" if info["anime"] else "✗"
            mark_color = colors["success"] if info["anime"] else colors["fail"]
            mark_width = draw.textlength(mark, font=header_font) if hasattr(draw, 'textlength') else header_font.getsize(mark)[0]
            draw.text((x + (col_widths[2] - mark_width) // 2, y + (cell_height - 18) // 2), mark, font=header_font, fill=mark_color)
            y += cell_height
        draw.rectangle([table_x, table_y, table_x + sum(col_widths), table_bottom], 
                      outline=colors["border"], width=border_width)
        for i in range(1, len(self.available_engines) + 1):
            line_y = table_y + header_height + cell_height * i
            if i < len(self.available_engines):  # 不是最后一行
                draw.line([(table_x, line_y), (table_x + sum(col_widths), line_y)], 
                         fill=colors["border"], width=border_width)
        draw.line([(table_x, table_y + header_height), (table_x + sum(col_widths), table_y + header_height)], 
                 fill=colors["border"], width=border_width)
        col_x = table_x
        for i in range(len(col_widths) - 1):
            col_x += col_widths[i]
            draw.line([(col_x, table_y), (col_x, table_bottom)], 
                     fill=colors["border"], width=border_width)
        with io.BytesIO() as output:
            img.save(output, format="JPEG", quality=95)
            output.seek(0)
            async for result in self._send_image(event, output.getvalue()):
                yield result

    async def _perform_search(self, event: AstrMessageEvent, engine: str, img_buffer: io.BytesIO):
        """
        执行图像搜索

        参数:
            event: 消息事件
            engine: 搜索引擎名称
            img_buffer: 图像数据

        产生:
            搜索结果消息
        """
        file_bytes = img_buffer.getvalue()
        result_text = await self.search_model.search(api=engine, file=file_bytes)
        img_buffer.seek(0)
        try:
            source_image = Image.open(img_buffer)
            result_img = self.search_model.draw_results(engine, result_text, source_image)
        except Exception as e:
            result_img = self.search_model.draw_error(engine, str(e))
        with io.BytesIO() as output:
            result_img.save(output, format="JPEG", quality=85)
            output.seek(0)
            async for result in self._send_image(event, output.getvalue()):
                yield result
        yield event.plain_result("需要文本格式的结果吗？回复\"是\"以获取，10秒内有效")
        user_id = event.get_sender_id()
        self.user_states[user_id] = {
            "step": "waiting_text_confirm",
            "timestamp": time.time(),
            "result_text": result_text
        }

    async def _send_engine_prompt(self, event: AstrMessageEvent, state: dict):
        """
        发送引擎选择提示

        参数:
            event: 消息事件
            state: 用户状态字典

        产生:
            提示消息结果
        """
        if not self.available_engines:
            yield event.plain_result("当前没有可用的搜索引擎，请联系管理员在配置中启用至少一个引擎")
            return
        example_engine = self.available_engines[0] if self.available_engines else "none"
        if not state.get('engine'):
            async for result in self._send_engine_intro(event):
                yield result
        if state.get('preloaded_img'):
            yield event.plain_result(f"图片已接收，请回复引擎名（如{example_engine}），30秒内有效")
        elif state.get('engine'):
            yield event.plain_result(f"已选择引擎: {state['engine']}，请发送图片或图片URL，30秒内有效")
        else:
            yield event.plain_result(f"请选择引擎（回复引擎名，如{example_engine}）并发送图片，30秒内有效")

    async def _handle_timeout(self, event: AstrMessageEvent, user_id: str):
        """
        处理用户状态超时

        参数:
            event: 消息事件
            user_id: 用户ID
        
        产生:
            超时提示消息
        """
        yield event.plain_result("等待超时，操作取消")
        if user_id in self.user_states:
            del self.user_states[user_id]
        event.stop_event()

    async def _handle_waiting_text_confirm(self, event: AstrMessageEvent, state: dict, user_id: str):
        """
        处理等待用户确认是否需要文本结果的状态

        参数:
            event: 消息事件
            state: 用户状态
            user_id: 用户ID

        产生:
            处理结果消息
        """
        message_text = self._get_message_text(event.message_obj)
        if time.time() - state["timestamp"] > 10:
            del self.user_states[user_id]
            event.stop_event()
            return
        elif message_text.strip().lower() == "是":
            text_parts = self._split_text_by_length(state["result_text"])
            sender_name = "图片搜索bot"
            sender_id = event.get_self_id()
            try:
                sender_id = int(sender_id)
            except:
                sender_id = 10000
            for i, part in enumerate(text_parts):
                node = Node(
                    name=sender_name,
                    uin=sender_id,
                    content=[Plain(f"【搜索结果 {i+1}/{len(text_parts)}】\n{part}")]
                )
                nodes = Nodes([node])
                try:
                    await event.send(event.chain_result([nodes]))
                except Exception as e:
                    yield event.plain_result(f"发送搜索结果失败: {str(e)}")
            del self.user_states[user_id]
            event.stop_event()

    async def _handle_waiting_engine(self, event: AstrMessageEvent, state: dict, user_id: str):
        """
        处理等待用户提供引擎名的状态

        参数:
            event: 消息事件
            state: 用户状态
            user_id: 用户ID

        产生:
            处理结果消息
        """
        example_engine = self.available_engines[0] if self.available_engines else "none"
        message_text = self._get_message_text(event.message_obj).lower()
        if not message_text:
            yield event.plain_result(f"请回复有效的引擎名（如{example_engine}）")
            state["timestamp"] = time.time()
            event.stop_event()
            return
        if message_text in self.available_engines:
            state["engine"] = message_text
            if state.get("preloaded_img"):
                try:
                    async for result in self._perform_search(event, state["engine"], state["preloaded_img"]):
                        yield result
                except Exception as e:
                    yield event.plain_result(f"搜索失败: {str(e)}")
            else:
                state["step"] = "waiting_image"
                state["timestamp"] = time.time()
                yield event.plain_result(f"已选择引擎: {message_text}，请在30秒内发送一张图片，我会进行搜索")
        else:
            all_engines = ["animetrace", "baidu", "bing", "copyseeker", "ehentai", "google", "saucenao", "tineye"]
            if message_text in all_engines and message_text not in self.available_engines:
                yield event.plain_result(f"引擎 '{message_text}' 已被禁用，请在配置中启用或选择其他引擎（如{example_engine}）")
                state["timestamp"] = time.time()
                async for result in self._send_engine_prompt(event, state):
                    yield result
            else:
                state.setdefault("invalid_attempts", 0)
                state["invalid_attempts"] += 1
                if state["invalid_attempts"] >= 2:
                    yield event.plain_result("连续两次输入错误的引擎名，已取消操作")
                    del self.user_states[user_id]
                else:
                    yield event.plain_result(f"引擎 '{message_text}' 不存在，请回复有效的引擎名（如{example_engine}）")
                    state["timestamp"] = time.time()
                    async for result in self._send_engine_prompt(event, state):
                        yield result
        event.stop_event()

    async def _handle_waiting_both(self, event: AstrMessageEvent, state: dict, user_id: str):
        """
        处理等待用户同时提供引擎名和图片的状态

        参数:
            event: 消息事件
            state: 用户状态
            user_id: 用户ID

        产生:
            处理结果消息
        """
        example_engine = self.available_engines[0] if self.available_engines else "none"
        updated = False
        message_text = self._get_message_text(event.message_obj).lower()
        img_urls = self._get_img_urls(event.message_obj)
        if message_text and message_text in self.available_engines and not state.get('engine'):
            state["engine"] = message_text
            updated = True
        img_buffer = None
        if img_urls:
            img_buffer = await self._download_img(img_urls[0])
        elif self._is_image_url(message_text):
            img_buffer = await self._download_img(message_text)
        if img_buffer and not state.get('preloaded_img'):
            state["preloaded_img"] = img_buffer
            updated = True
        if state.get("engine") and state.get("preloaded_img"):
            try:
                async for result in self._perform_search(event, state["engine"], state["preloaded_img"]):
                    yield result
            except Exception as e:
                yield event.plain_result(f"搜索失败: {str(e)}")
            event.stop_event()
            return
        if updated:
            state["timestamp"] = time.time()
            async for result in self._send_engine_prompt(event, state):
                yield result
        else:
            state["timestamp"] = time.time()
            is_invalid_engine_attempt = message_text and not self._is_image_url(message_text) and not state.get('engine')
            if is_invalid_engine_attempt:
                all_engines = ["animetrace", "baidu", "bing", "copyseeker", "ehentai", "google", "saucenao", "tineye"]
                if message_text in all_engines and message_text not in self.available_engines:
                    yield event.plain_result(f"引擎 '{message_text}' 已被禁用，请在配置中启用或选择其他引擎（如{example_engine}）")
                    async for result in self._send_engine_prompt(event, state):
                        yield result
                else:
                    state.setdefault("invalid_attempts", 0)
                    state["invalid_attempts"] += 1
                    if state["invalid_attempts"] >= 2:
                        yield event.plain_result("连续两次输入错误的引擎名，已取消操作")
                        del self.user_states[user_id]
                    else:
                        yield event.plain_result(f"引擎 '{message_text}' 不存在，请回复有效的引擎名（如{example_engine}）")
                        async for result in self._send_engine_prompt(event, state):
                            yield result
            else:
                if not state.get('engine') and not state.get('preloaded_img'):
                    yield event.plain_result(f"请提供引擎名（如{example_engine}）和图片")
                elif not state.get('engine'):
                    yield event.plain_result(f"请提供引擎名（如{example_engine}）")
                elif not state.get('preloaded_img'):
                    yield event.plain_result("请提供图片")
        event.stop_event()

    async def _handle_waiting_image(self, event: AstrMessageEvent, state: dict, user_id: str):
        """
        处理等待用户提供图片的状态

        参数:
            event: 消息事件
            state: 用户状态
            user_id: 用户ID

        产生:
            处理结果消息
        """
        img_urls = self._get_img_urls(event.message_obj)
        message_text = self._get_message_text(event.message_obj)
        img_buffer = None
        if img_urls:
            img_buffer = await self._download_img(img_urls[0])
        elif self._is_image_url(message_text):
            img_buffer = await self._download_img(message_text)
        if img_buffer:
            if not img_buffer:
                yield event.plain_result("图片下载失败，请稍后重试")
                del self.user_states[user_id]
                return
            async for result in self._perform_search(event, state["engine"], img_buffer):
                yield result
            event.stop_event()
        else:
            yield event.plain_result("请发送一张图片或图片链接")

    async def _handle_initial_search_command(self, event: AstrMessageEvent, user_id: str):
        """
        处理初始搜索命令

        参数:
            event: 消息事件
            user_id: 用户ID

        产生:
            处理结果消息
        """
        if not self.available_engines:
            yield event.plain_result("当前没有可用的搜索引擎，请联系管理员在配置中启用至少一个引擎")
            event.stop_event()
            return
        example_engine = self.available_engines[0]
        message_text = self._get_message_text(event.message_obj)
        img_urls = self._get_img_urls(event.message_obj)
        parts = message_text.strip().split()
        if user_id in self.user_states:
            del self.user_states[user_id]
        engine = None
        url_from_text = None
        invalid_engine = False
        disabled_engine = False
        potential_engine = None
        if len(parts) > 1:
            if self._is_image_url(parts[1]):
                url_from_text = parts[1]
            else:
                potential_engine = parts[1].lower()
                if potential_engine in self.available_engines:
                    engine = potential_engine
                else:
                    all_engines = ["animetrace", "baidu", "bing", "copyseeker", "ehentai", "google", "saucenao", "tineye"]
                    if potential_engine in all_engines:
                        disabled_engine = True
                    else:
                        invalid_engine = True
                if len(parts) > 2 and self._is_image_url(parts[2]):
                    url_from_text = parts[2]
        preloaded_img = None
        if img_urls:
            preloaded_img = await self._download_img(img_urls[0])
        elif url_from_text:
            preloaded_img = await self._download_img(url_from_text)
        if disabled_engine:
            state = {
                "step": "waiting_both",
                "timestamp": time.time(),
                "preloaded_img": preloaded_img,
                "engine": None
            }
            self.user_states[user_id] = state
            yield event.plain_result(f"引擎 '{potential_engine}' 已被禁用，请在配置中启用或选择其他引擎（如{example_engine}）")
            async for result in self._send_engine_prompt(event, state):
                yield result
            event.stop_event()
            return
        if invalid_engine:
            state = {
                "step": "waiting_both",
                "timestamp": time.time(),
                "preloaded_img": preloaded_img,
                "engine": None,
                "invalid_attempts": 1
            }
            self.user_states[user_id] = state
            yield event.plain_result(f"引擎 '{potential_engine}' 不存在，请提供有效的引擎名（如{example_engine}）")
            async for result in self._send_engine_prompt(event, state):
                yield result
            event.stop_event()
            return
        if engine and preloaded_img:
            try:
                async for result in self._perform_search(event, engine, preloaded_img):
                    yield result
            except Exception as e:
                yield event.plain_result(f"搜索失败: {str(e)}")
            event.stop_event()
            return
        state = {
            "step": "waiting_both",
            "timestamp": time.time(),
            "preloaded_img": preloaded_img,
            "engine": engine
        }
        self.user_states[user_id] = state
        async for result in self._send_engine_prompt(event, state):
            yield result
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """
        处理传入消息

        参数:
            event: 消息事件

        产生:
            处理结果消息
        """
        user_id = event.get_sender_id()
        message_text = self._get_message_text(event.message_obj)
        if message_text.strip().startswith("以图搜图"):
            async for result in self._handle_initial_search_command(event, user_id):
                yield result
            return
        if user_id not in self.user_states:
            return
        state = self.user_states[user_id]
        if state.get("step") == "waiting_text_confirm":
            async for result in self._handle_waiting_text_confirm(event, state, user_id):
                yield result
            return
        if time.time() - state["timestamp"] > 30:
            async for result in self._handle_timeout(event, user_id):
                yield result
            return
        if state["step"] == "waiting_engine":
            async for result in self._handle_waiting_engine(event, state, user_id):
                yield result
        elif state["step"] == "waiting_both":
            async for result in self._handle_waiting_both(event, state, user_id):
                yield result
        elif state["step"] == "waiting_image":
            async for result in self._handle_waiting_image(event, state, user_id):
                yield result
