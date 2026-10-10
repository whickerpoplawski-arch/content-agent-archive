"""
debug_feishu.py — 飞书权限逐步诊断
"""
import os
import json
import requests
from dotenv import load_dotenv


def main():
    load_dotenv()

    APP_ID         = os.getenv("FEISHU_APP_ID")
    APP_SECRET     = os.getenv("FEISHU_APP_SECRET")
    APP_TOKEN      = os.getenv("FEISHU_APP_TOKEN")
    TABLE_CRAWL    = os.getenv("FEISHU_TABLE_ID")
    TABLE_PUBLISH  = os.getenv("FEISHU_PUBLISH_TABLE_ID")

    print("=" * 50)
    print("📋 配置检查")
    print(f"  APP_ID        : {APP_ID}")
    print(f"  APP_TOKEN     : {APP_TOKEN}")
    print(f"  TABLE_CRAWL   : {TABLE_CRAWL}")
    print(f"  TABLE_PUBLISH : {TABLE_PUBLISH}")
    print()

    # Step 1: 获取 token
    print("Step 1: 获取 tenant_access_token …")
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=20,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"❌ 获取 token 失败: {data}")
        return
    token = data["tenant_access_token"]
    print("✅ token 获取成功\n")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Step 1.5: 列出所有表
    print("Step 1.5: 列出该文档所有表（确认 table_id）…")
    r = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables",
        headers=headers, timeout=20,
    )
    tables = r.json().get("data", {}).get("items", [])
    for t in tables:
        print(f"  table_id={t.get('table_id')}  name={t.get('name')}")
    print()

    # Step 2: 读爬虫表
    print("Step 2: 读取爬虫表（GET）…")
    r = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_CRAWL}/records",
        headers=headers, params={"page_size": 1}, timeout=20,
    )
    print(f"  HTTP {r.status_code}  code={r.json().get('code')}  msg={r.json().get('msg')}")

    # Step 3: 读发布表
    print("\nStep 3: 读取发布表（GET）…")
    r = requests.get(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_PUBLISH}/records",
        headers=headers, params={"page_size": 1}, timeout=20,
    )
    print(f"  HTTP {r.status_code}  code={r.json().get('code')}  msg={r.json().get('msg')}")

    # Step 4: 写入发布表
    print("\nStep 4: 向发布表写入测试记录（POST）…")
    r = requests.post(
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_PUBLISH}/records",
        headers=headers,
        json={"fields": {"标题": "debug测试", "正文": "debug正文"}},
        timeout=20,
    )
    print(f"  HTTP {r.status_code}")
    print(json.dumps(r.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
