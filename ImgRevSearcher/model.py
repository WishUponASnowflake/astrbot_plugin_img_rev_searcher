import datetime
import io
from pathlib import Path
from typing import Any, Optional, Union
from PIL import Image, ImageDraw, ImageFont
from config_manager import DEFAULT_COOKIES, DEFAULT_PARAMS, ENGINE_MAP, PROXIES
from utils import Network
from utils.types import FileContent


class BaseSearchModel:
    """
    图像反向搜索基础模型类
    
    提供多种搜索引擎的统一接口，支持本地文件和URL搜索，
    可以输出文本结果或生成可视化图像结果，支持GIF格式自动转换
    """
    
    def __init__(self, proxies: Optional[str] = PROXIES, cookies: Optional[str] = None,
                 timeout: int = 60, **kwargs: Any):
        """
        初始化搜索模型
        
        参数:
            proxies: 代理服务器配置
            cookies: Cookie配置
            timeout: 请求超时时间(秒)
            **kwargs: 其他配置参数
        """
        self.proxies = proxies
        self.cookies = cookies
        self.timeout = timeout
        self.config = kwargs
    
    def _prepare_engine_params(self, api: str, search_params: dict) -> dict:
        """
        根据API类型准备引擎参数
        
        参数:
            api: 搜索引擎API名称
            search_params: 搜索参数字典
            
        返回:
            dict: 处理后的引擎参数字典
        """
        engine_params = {}
        
        if api == "anime_trace":
            engine_params = {
                "is_multi": search_params.pop("is_multi", None),
                "ai_detect": search_params.pop("ai_detect", None)
            }
        elif api == "ehentai":
            engine_params = {
                "is_ex": search_params.pop("is_ex", False),
                "covers": search_params.pop("covers", False),
                "similar": search_params.pop("similar", True),
                "exp": search_params.pop("exp", False)
            }
        elif api == "saucenao":
            engine_params = {
                "api_key": search_params.pop("api_key"),
                "hide": search_params.pop("hide", 3),
                "numres": search_params.pop("numres", 5),
                "minsim": search_params.pop("minsim", 30),
                "output_type": search_params.pop("output_type", 2),
                "testmode": search_params.pop("testmode", 0),
                "dbmask": search_params.pop("dbmask", None),
                "dbmaski": search_params.pop("dbmaski", None),
                "db": search_params.pop("db", 999),
                "dbs": search_params.pop("dbs", None)
            }
        elif api == "google_lens":
            engine_params = {
                "search_type": search_params.pop("search_type", "exact_matches"),
                "hl": search_params.pop("hl", "en"),
                "country": search_params.pop("country", "HK"),
                "q": search_params.get("q", None),
                "max_results": search_params.pop("max_results", 50)
            }
        
        return engine_params

    def _is_gif(self, file: FileContent) -> bool:
        """
        检查文件是否为GIF格式
        
        参数:
            file: 待检查的文件内容
            
        返回:
            bool: 如果是GIF格式返回True，否则返回False
        """
        if isinstance(file, (str, Path)):
            return str(file).lower().endswith('.gif')
        elif isinstance(file, bytes):
            return file.startswith((b'GIF87a', b'GIF89a'))
        return False

    def _convert_gif_to_jpeg(self, file: FileContent) -> bytes:
        """
        将GIF图像转换为JPEG格式
        
        参数:
            file: GIF格式的文件内容
            
        返回:
            bytes: 转换后的JPEG格式图像数据
        """
        if isinstance(file, bytes):
            img_data = file
        else:
            with open(file, 'rb') as f:
                img_data = f.read()
        img = Image.open(io.BytesIO(img_data))
        img.seek(0)
        jpeg_io = io.BytesIO()
        img.convert('RGB').save(jpeg_io, 'JPEG', quality=85)
        return jpeg_io.getvalue()

    async def search(self, api: str, file: FileContent = None,
                     url: Optional[str] = None, **kwargs: Any) -> str:
        """
        执行图像反向搜索
        
        参数:
            api: 搜索引擎API名称
            file: 本地文件内容
            url: 图像URL
            **kwargs: 其他搜索参数
            
        返回:
            str: 搜索结果文本
            
        异常:
            ValueError: 当API不支持或参数错误时抛出
        """
        if api not in ENGINE_MAP:
            available = ", ".join(ENGINE_MAP.keys())
            raise ValueError(f"不支持的引擎: {api}，支持的引擎: {available}")
        if not file and not url:
            raise ValueError("必须提供 file 或 url 参数")
        if file and url:
            raise ValueError("file 和 url 参数不能同时提供")
        if file and not url and self._is_gif(file):
            file = self._convert_gif_to_jpeg(file)
        try:
            engine_class = ENGINE_MAP[api]
            default_params = DEFAULT_PARAMS.get(api, {})
            search_params = {**default_params, **kwargs}
            
            network_kwargs = {}
            if self.proxies:
                network_kwargs["proxies"] = self.proxies
            
            effective_cookies = self.cookies or DEFAULT_COOKIES.get(api)
            if effective_cookies:
                network_kwargs["cookies"] = effective_cookies
                
            if self.timeout:
                network_kwargs["timeout"] = self.timeout
                
            async with Network(**network_kwargs) as client:
                engine_params = self._prepare_engine_params(api, search_params)
                engine_instance = engine_class(client=client, **engine_params)
                
                if api == "anime_trace" and search_params.get("base64"):
                    response = await engine_instance.search(
                        base64=search_params.pop("base64"), 
                        model=search_params.pop("model", None), 
                        **search_params
                    )
                else:
                    response = await engine_instance.search(file=file, url=url, **search_params)
                    
                return response.show_result()
        except Exception as e:
            return self._format_error(api, str(e))

    async def search_and_print(self, api: str, file: FileContent = None,
                               url: Optional[str] = None, **kwargs: Any) -> None:
        """
        执行搜索并打印结果到控制台
        
        参数:
            api: 搜索引擎API名称
            file: 本地文件内容
            url: 图像URL
            **kwargs: 其他搜索参数
            
        返回:
            None
        """
        try:
            result = await self.search(api=api, file=file, url=url, **kwargs)
            print(result)
        except Exception as e:
            print(f"❌ {api} 搜索失败: {e}")

    async def search_and_draw(self, api: str, file: FileContent = None,
                              url: Optional[str] = None, is_auto_save: bool = False, **kwargs: Any) -> Image.Image:
        """
        执行搜索并将结果渲染为图像
        
        参数:
            api: 搜索引擎API名称
            file: 本地文件内容
            url: 图像URL
            is_auto_save: 是否自动保存结果图像
            **kwargs: 其他搜索参数
            
        返回:
            Image.Image: 渲染后的结果图像
        """
        try:
            result = await self.search(api=api, file=file, url=url, **kwargs)
            margin = 20
            lines = result.split('\n')
            base_dir = Path(__file__).parent
            font_path = str(base_dir / "resource/font/arialuni.ttf")
            try:
                font = ImageFont.truetype(font_path, 18)
                title_font = ImageFont.truetype(font_path, 24)
            except IOError:
                font = ImageFont.load_default()
                title_font = ImageFont.load_default()
            source_image = None
            if file is not None:
                if isinstance(file, (str, Path)):
                    source_image = Image.open(file)
                elif isinstance(file, bytes):
                    source_image = Image.open(io.BytesIO(file))
            elif url is not None:
                network_kwargs = {}
                if self.proxies:
                    network_kwargs["proxies"] = self.proxies
                if self.timeout:
                    network_kwargs["timeout"] = self.timeout
                async with Network(**network_kwargs) as client:
                    response = await client.get(url)
                    img_data = await response.read()
                    source_image = Image.open(io.BytesIO(img_data))
            title_text = f"{api.upper()} 搜索结果"
            if hasattr(title_font, "getbbox"):
                title_width = title_font.getbbox(title_text)[2] + margin * 2
            else:
                title_width = title_font.getsize(title_text)[0] + margin * 2
            max_text_width = 0
            for line in lines:
                if hasattr(font, "getbbox"):
                    line_width = font.getbbox(line)[2] + margin * 2
                else:
                    line_width = font.getsize(line)[0] + margin * 2
                max_text_width = max(max_text_width, line_width)
            source_img_height = 0
            source_img_width = 0
            if source_image:
                max_source_width = 800
                orig_width, orig_height = source_image.size
                if orig_width > max_source_width:
                    ratio = max_source_width / orig_width
                    source_img_width = max_source_width
                    source_img_height = int(orig_height * ratio)
                    source_image = source_image.resize((source_img_width, source_img_height), Image.LANCZOS)
                else:
                    source_img_width = orig_width
                    source_img_height = orig_height
            width = max(800, title_width, max_text_width, source_img_width + margin * 2)
            if hasattr(font, "getbbox"):
                line_height = max(25, font.getbbox("Ay")[3] + 7)
            else:
                line_height = max(25, font.getsize("Ay")[1] + 7)
            header_height = 60
            content_height = margin + line_height * len(lines)
            source_area_height = source_img_height + margin * 2 if source_image else 0
            total_height = header_height + content_height + source_area_height
            img = Image.new('RGB', (width, total_height), color='white')
            draw = ImageDraw.Draw(img)
            draw.rectangle([(0, 0), (width, header_height)], fill='#4a6ea9')
            draw.text((margin, margin), title_text, font=title_font, fill='white')
            y_offset = header_height
            if source_image:
                x_center = (width - source_img_width) // 2
                img.paste(source_image, (x_center, y_offset + margin))
                y_offset += source_img_height + margin * 2
                draw.line([(margin, y_offset - margin//2), (width - margin, y_offset - margin//2)], 
                          fill='#cccccc', width=2)
            y_position = y_offset
            for line in lines:
                if line.startswith('='):
                    draw.line([(margin, y_position), (width - margin, y_position)], fill='#cccccc', width=1)
                else:
                    draw.text((margin, y_position), line, font=font, fill='black')
                y_position += line_height
                
            if is_auto_save:
                save_dir = Path("search_results")
                save_dir.mkdir(exist_ok=True)
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                file_name = f"{api}_{timestamp}.jpeg"
                save_path = save_dir / file_name
                img.save(save_path, "JPEG", quality=85)
            return img
        except Exception as e:
            width, height = 600, 200
            img = Image.new('RGB', (width, height), color='white')
            draw = ImageDraw.Draw(img)
            draw.rectangle([(0, 0), (width, 60)], fill='#e74c3c')
            try:
                font_path = str(Path(__file__).parent / "resource/font/arialuni.ttf")
                font = ImageFont.truetype(font_path, 18)
                title_font = ImageFont.truetype(font_path, 24)
            except IOError:
                font = ImageFont.load_default()
                title_font = ImageFont.load_default()
                
            margin = 20
            draw.text((margin, margin), f"{api.upper()} 搜索失败", font=title_font, fill='white')
            draw.text((margin, 80), f"错误信息: {str(e)}", font=font, fill='black')
            
            if is_auto_save:
                save_dir = Path("search_results")
                save_dir.mkdir(exist_ok=True)
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                file_name = f"{api}_error_{timestamp}.jpeg"
                save_path = save_dir / file_name
                img.save(save_path, "JPEG", quality=85)
            return img

    def _format_error(self, api: str, error_msg: str) -> str:
        """
        格式化错误信息
        
        参数:
            api: 搜索引擎API名称
            error_msg: 原始错误信息
            
        返回:
            str: 格式化后的错误信息
        """
        friendly_msg = "未搜索到相关信息" if "list index out of range" in error_msg.lower() else error_msg
        return f"""{'=' * 50}
{api.upper()} 搜索失败
{'=' * 50}
错误信息: {friendly_msg}
{'=' * 50}"""

    @classmethod
    def get_supported_engines(cls) -> list[str]:
        """
        获取所有支持的搜索引擎列表
        
        返回:
            list[str]: 支持的搜索引擎名称列表
        """
        return list(ENGINE_MAP.keys())
