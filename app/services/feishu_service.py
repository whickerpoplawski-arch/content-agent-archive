"""
feishu_service.py
将 AI 生成的小红书笔记（含本地图片路径）同步到飞书多维表格。
字段与 XHSMCPToolArgs 对齐，方便核对实际发布参数。

飞书表字段（需在多维表格中提前建好同名列）：
  标题        文本
  正文        文本
  标签        文本
  图片路径    文本（多张用 | 分隔）
  是否原创    文本（是 / 否）
  可见性      文本
  内容类型    文本
  生成时间    文本
"""

import httpx
from datetime import datetime
from typing import List

from app.core.config import settings
from app.models.schemas import XHSMCPToolArgs, NoteItem


async def _get_tenant_access_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json={
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret,
        })
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    return data["tenant_access_token"]


async def _create_record(token: str, table_id: str, fields: dict) -> dict:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps"
        f"/{settings.feishu_app_token}/tables/{table_id}/records"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"fields": fields},
        )
    return resp.json()


def _build_fields(args: XHSMCPToolArgs, content_type: str = "") -> dict:
    """
    将 XHSMCPToolArgs 转成飞书多维表格字段。
    字段名须与飞书表头完全一致。
    """
    fields: dict = {
        "标题":     args.title,
        "正文":     args.content,
        "标签":     " | ".join(args.tags) if args.tags else "",
        "图片路径": " | ".join(args.images),
        "是否原创": "是" if args.is_original else "否",
        "可见性":   args.visibility,
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if content_type:
        fields["内容类型"] = content_type
    # 去掉空字符串字段
    return {k: v for k, v in fields.items() if v != ""}


async def sync_to_feishu(
    args: XHSMCPToolArgs,
    content_type: str = "",
) -> dict:
    """
    把一条 AI 生成的笔记同步到飞书多维表格。

    Args:
        args:         XHSMCPToolArgs，与发布到小红书的参数完全一致
        content_type: 内容类型（测评/清单/教程/避雷/分享），来自 ContentItem

    Returns:
        {"success": bool, "message": str}
    """
    if not settings.feishu_app_id or not settings.feishu_publish_table_id:
        return {"success": False, "message": "飞书配置未填写（FEISHU_APP_ID / FEISHU_PUBLISH_TABLE_ID）"}

    try:
        token = await _get_tenant_access_token()
        fields = _build_fields(args, content_type)
        result = await _create_record(token, settings.feishu_publish_table_id, fields)
    except Exception as e:
        return {"success": False, "message": f"飞书同步异常: {e}"}

    if result.get("code") == 0:
        return {"success": True, "message": "飞书同步成功"}
    print(f"[Feishu] 同步失败，完整响应: {result}")
    return {"success": False, "message": f"飞书同步失败: {result}"}


async def sync_crawled_notes_to_feishu(notes: List[NoteItem]) -> dict:
    """
    将爬取到的笔记批量同步到飞书爬虫数据表（FEISHU_TABLE_ID）。
    字段与 CrawlData_to_FeishiList.py 保持一致。
    """
    if not settings.feishu_app_id or not settings.feishu_table_id:
        return {"success": False, "message": "飞书配置未填写（FEISHU_APP_ID / FEISHU_TABLE_ID）"}

    def _build_crawl_fields(note: NoteItem) -> dict:
        tags_text = " | ".join(note.tags) if note.tags else ""
        fields: dict = {
            "标题": note.title or "",
            "作者": note.author or "",
            "正文": note.content or "",
            "链接": note.url or "",
            "点赞数": note.likes,
            "评论数": note.comments,
            "收藏数": note.favorites,
            "标签": tags_text,
            "发布时间": note.publish_time or "",
            "内容类型": note.content_type or "",
        }
        return {k: v for k, v in fields.items() if v != "" and v is not None}

    try:
        token = await _get_tenant_access_token()
    except Exception as e:
        return {"success": False, "message": f"获取飞书 token 失败: {e}"}

    success_count, fail_count = 0, 0
    for note in notes:
        try:
            fields = _build_crawl_fields(note)
            result = await _create_record(token, settings.feishu_table_id, fields)
            if result.get("code") == 0:
                success_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1

    return {
        "success": fail_count == 0,
        "message": f"同步完成：成功 {success_count} 条，失败 {fail_count} 条",
    }