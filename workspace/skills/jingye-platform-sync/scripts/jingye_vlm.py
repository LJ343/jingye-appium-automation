#!/usr/bin/env python3
# coding: utf-8
"""Visual fallback for Jingye weighing-ticket extraction.

Importing this module is side-effect free. Network access only happens when
``extract_ticket_with_vlm`` is called explicitly after text parsing failed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import fitz
import requests
from PIL import Image


PROMPT_VERSION = "jingye-platform-weight-v1"
MODEL_NAME = os.getenv("RESULT_CHECK_MODEL_NAME", "qwen3-vl-plus").strip()
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "").strip()

VLM_IMAGE_MAX_SIDE = int(os.getenv("VLM_IMAGE_MAX_SIDE", "2200"))
VLM_RENDER_DPI = int(os.getenv("VLM_RENDER_DPI", "190"))
VLM_JPEG_QUALITY = int(os.getenv("VLM_JPEG_QUALITY", "92"))
VLM_MAX_PAGES = int(os.getenv("VLM_MAX_PAGES", "4"))
VLM_EXTRACTION_TEMPERATURE = float(os.getenv("VLM_EXTRACTION_TEMPERATURE", "0.1"))
VLM_MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS", "1800"))
VLM_CONNECT_TIMEOUT_SECONDS = int(os.getenv("VLM_CONNECT_TIMEOUT_SECONDS", "8"))
VLM_READ_TIMEOUT_SECONDS = int(os.getenv("VLM_READ_TIMEOUT_SECONDS", "120"))
VLM_REQUEST_RETRIES = int(os.getenv("VLM_REQUEST_RETRIES", "3"))

_THREAD_LOCAL = threading.local()

SYSTEM_PROMPT = (
    "你是敬业运输计量单的视觉信息抽取器。必须逐页观察PDF图片，严格按照字段标签读取，"
    "只返回单个JSON对象，不要解释、不要Markdown、不要推测看不清的值。"
)

USER_PROMPT = """请识别这份敬业运输计量单的所有页面，并抽取平台磅单需要的字段。

关键规则：
1. gross_weight、tare_weight、net_weight分别取“毛重”“皮重”“净重”标签后的吨数，只返回数字。
2. gross_time、tare_time、net_time分别取对应重量行斜杠后的14位紧凑时间，返回YYYYMMDDHHMMSS；不要使用页面底部较晚的打印时间。
3. document_time取页面底部标准格式打印时间，无法确认时返回空字符串。
4. business_code取“业务：”后的编码；plate_no取“车号：”；goods_name取“货名：”。
5. 不得根据毛重和皮重计算净重；三个重量都必须从图片中独立读取。看不清时返回null并在warnings说明。
6. confidence取0到1之间的整体识别置信度。

只返回以下JSON结构：
{
  "business_code": "",
  "plate_no": "",
  "goods_name": "",
  "gross_weight": null,
  "tare_weight": null,
  "net_weight": null,
  "gross_time": "YYYYMMDDHHMMSS或空字符串",
  "tare_time": "YYYYMMDDHHMMSS或空字符串",
  "net_time": "YYYYMMDDHHMMSS或空字符串",
  "document_time": "YYYY-MM-DD HH:MM:SS或空字符串",
  "confidence": 0.0,
  "warnings": []
}

PDF内嵌文本仅作辅助，图片内容为最终依据：
---BEGIN AUX TEXT---
{aux_text}
---END AUX TEXT---
"""


def _session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=4, max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
    return session


def _render_pdf_pages(path: Path) -> tuple[list[str], int]:
    data_urls: list[str] = []
    zoom = VLM_RENDER_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(path) as document:
        page_count = document.page_count
        if page_count < 1:
            raise RuntimeError("PDF没有页面")
        if page_count > VLM_MAX_PAGES:
            raise RuntimeError(f"PDF共{page_count}页，超过允许的{VLM_MAX_PAGES}页")
        for page_number in range(page_count):
            page = document.load_page(page_number)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
            max_side = max(image.size)
            if max_side > VLM_IMAGE_MAX_SIDE:
                ratio = VLM_IMAGE_MAX_SIDE / max_side
                image = image.resize(
                    (
                        max(1, round(image.width * ratio)),
                        max(1, round(image.height * ratio)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=VLM_JPEG_QUALITY,
                optimize=True,
            )
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            data_urls.append(f"data:image/jpeg;base64,{encoded}")
    return data_urls, page_count


def _message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                pieces.append(str(item.get("text") or item.get("content") or ""))
            else:
                pieces.append(str(item))
        return "".join(pieces).strip()
    return ""


def _parse_json_object(content: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(text[start : end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError(f"模型返回不是有效JSON对象: {text[:500]}")


def _payload(aux_text: str, image_urls: list[str], simple_mode: bool) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {"type": "text", "text": USER_PROMPT.replace("{aux_text}", aux_text[:12000])}
    ]
    for image_url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": VLM_EXTRACTION_TEMPERATURE,
        "max_tokens": VLM_MAX_TOKENS,
        "stream": False,
    }
    if not simple_mode:
        payload["response_format"] = {"type": "json_object"}
        payload["enable_thinking"] = False
    return payload


def _call_vlm(aux_text: str, image_urls: list[str]) -> dict[str, Any]:
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY为空")
    if not DASHSCOPE_BASE_URL:
        raise RuntimeError("DASHSCOPE_BASE_URL为空")
    url = DASHSCOPE_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    last_error: Optional[Exception] = None
    for request_attempt in range(1, VLM_REQUEST_RETRIES + 1):
        for simple_mode in (False, True):
            try:
                response = _session().post(
                    url,
                    headers=headers,
                    json=_payload(aux_text, image_urls, simple_mode),
                    timeout=(
                        VLM_CONNECT_TIMEOUT_SECONDS,
                        VLM_READ_TIMEOUT_SECONDS,
                    ),
                )
                if response.status_code == 200:
                    data = response.json()
                    content = _message_content(data)
                    if not content:
                        raise RuntimeError("模型content为空")
                    return _parse_json_object(content)
                if response.status_code == 400 and not simple_mode:
                    continue
                if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
                    raise RuntimeError(
                        f"VLM临时错误 HTTP {response.status_code}: {response.text[:800]}"
                    )
                raise RuntimeError(
                    f"VLM请求失败 HTTP {response.status_code}: {response.text[:1000]}"
                )
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if not simple_mode:
                    continue
        if request_attempt < VLM_REQUEST_RETRIES:
            time.sleep(min(12.0, 1.8**request_attempt))
    raise RuntimeError(f"VLM连续请求失败: {last_error}")


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def extract_ticket_with_vlm(
    path: Path,
    *,
    sha256: str,
    aux_text: str,
    cache_dir: Path,
) -> dict[str, Any]:
    cache_key = hashlib.sha256(
        f"{sha256}|{MODEL_NAME}|{PROMPT_VERSION}".encode("utf-8")
    ).hexdigest()
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                cached.get("model") == MODEL_NAME
                and cached.get("prompt_version") == PROMPT_VERSION
                and not cached.get("error")
                and isinstance(cached.get("result"), dict)
            ):
                result = dict(cached["result"])
                result["_from_cache"] = True
                result["_page_count"] = int(cached.get("page_count") or 0)
                return result
        except Exception:
            pass

    try:
        images, page_count = _render_pdf_pages(path)
        result = _call_vlm(aux_text, images)
        _write_cache(
            cache_path,
            {
                "model": MODEL_NAME,
                "prompt_version": PROMPT_VERSION,
                "page_count": page_count,
                "result": result,
                "error": "",
            },
        )
        result = dict(result)
        result["_from_cache"] = False
        result["_page_count"] = page_count
        return result
    except Exception as exc:
        try:
            _write_cache(
                cache_path,
                {
                    "model": MODEL_NAME,
                    "prompt_version": PROMPT_VERSION,
                    "page_count": 0,
                    "result": {},
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            pass
        raise
