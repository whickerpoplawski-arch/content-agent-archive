"""
image_service.py
调用 gpt-image-1 根据话题数据生成图片，保存到本地，返回文件路径列表。
"""

import base64
import time
import os
from pathlib import Path

from openai import AsyncOpenAI

from app.core.config import settings
from app.models.schemas import ContentItem, TopicItem


def _build_image_prompt(topic: TopicItem, content: ContentItem) -> str:
    """
    将话题信息和内容中的 image_suggestion 整合成图片生成 prompt。
    用中文描述转英文风格指令，因为 gpt-image-1 对英文 prompt 效果更好。
    """
    # 把中文 image_suggestion 直接拼进 prompt，模型能理解中文
    return (
        f"Create a high-quality, vibrant social media image for Xiaohongshu (Little Red Book). "
        f"Topic: {topic.title}. "
        f"Visual concept: {content.image_suggestion}. "
        f"Style: warm, lifestyle, authentic, bright colors, suitable for a Chinese female audience aged 18-28. "
        f"No text overlay. Square composition 1:1."
    )


async def generate_images(
    topic: TopicItem,
    content: ContentItem,
    image_count: int = 1,
) -> list[str]:
    """
    调用 gpt-image-1 生成图片，保存到本地，返回绝对路径列表。

    Args:
        topic: 话题信息，提供主题背景
        content: 生成的内容，其中 image_suggestion 作为视觉参考
        image_count: 生成图片数量，1-4 张

    Returns:
        本地图片文件的绝对路径列表
    """
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
    )

    prompt = _build_image_prompt(topic, content)

    # 确保输出目录存在
    output_dir = Path(settings.image_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    response = await client.images.generate(
        model=settings.image_model,
        prompt=prompt,
        size=settings.image_size,  # type: ignore[arg-type]
        n=image_count,
    )

    saved_paths: list[str] = []
    ts = int(time.time())

    for idx, image_data in enumerate(response.data):
        # gpt-image-1 默认返回 b64_json
        if image_data.b64_json:
            img_bytes = base64.b64decode(image_data.b64_json)
            file_path = output_dir / f"{ts}_{idx}.png"
            file_path.write_bytes(img_bytes)
            saved_paths.append(str(file_path.resolve()))
        elif image_data.url:
            # 如果返回的是 url（备用），用 httpx 下载
            import httpx
            async with httpx.AsyncClient() as http:
                resp = await http.get(image_data.url, timeout=60)
                resp.raise_for_status()
            file_path = output_dir / f"{ts}_{idx}.png"
            file_path.write_bytes(resp.content)
            saved_paths.append(str(file_path.resolve()))
        else:
            raise ValueError(f"图片数据为空，index={idx}")

    return saved_paths