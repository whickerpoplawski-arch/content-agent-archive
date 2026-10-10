"""Drive the Agent Console UI end-to-end with Playwright (浏览器实战, 2026-09-15).

Fills the console form, starts a run through the real UI (same code path as a
human user: form -> POST /agent/runs -> SSE), watches the live trajectory,
screenshots key moments, and dumps the rendered timeline/metrics/artifacts for
the systematic evaluator.

Usage:
  python scripts/console_live_run.py [--headed] [--timeout 900]

Outputs to data/agent/console_shots/<ts>/: form.png, midrun.png, final.png,
page_export.json (run_id, status, timeline text, metrics text, artifacts).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

CONSOLE_URL = "http://127.0.0.1:8000/static/agent_console.html"
GOAL = (
    "围绕「西电毕业去向」调研西电学生毕业去向情况（就业、考研、保研、出国、"
    "实习等方向的比例与经验分享），并生成3篇小红书分享文案。不要发布，不要同步飞书。"
)
AUDIENCE = "西电学生"
TONE = "真实分享"
TERMINAL = ("completed", "failed", "stopped", "已完成", "失败", "已停止")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--timeout", type=int, default=900, help="max seconds to wait for terminal state")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("data/agent/console_shots") / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(CONSOLE_URL, wait_until="networkidle")

        # 表单填写 (与人工操作同一代码路径)
        page.fill("#goalInput", GOAL)
        page.fill("#audienceInput", AUDIENCE)
        page.fill("#toneInput", TONE)
        # 控制台按钮语义: 发布/飞书=默认禁用的开关, 其余=工具详情弹窗。
        # 这里按普通用户操作: 不碰开关 (发布/飞书保持禁用), 配图保留给
        # planner 自主决定 (目标未要求图, 预期不会调用; 评估器单独监控)。
        page.evaluate("document.getElementById('toolDialog')?.close()")
        page.screenshot(path=str(out_dir / "01_form.png"))

        page.click("#createRunBtn")
        # 等 run_id 出现
        page.wait_for_function(
            "document.getElementById('runId').textContent.trim() !== '新任务'",
            timeout=30000,
        )
        run_id = page.inner_text("#runId").strip()
        print(f"[console] run started: {run_id}")

        # 监听: 状态 + 时间线, 每 10s 采样一次
        deadline = time.time() + args.timeout
        samples = []
        midrun_shot = False
        status = ""
        while time.time() < deadline:
            time.sleep(10)
            status = page.inner_text("#runStatus").strip()
            timeline_n = page.locator("#timeline .event").count()
            samples.append({"t": round(time.time()), "status": status, "events": timeline_n})
            print(f"[monitor] status={status} timeline_events={timeline_n}")
            if not midrun_shot and timeline_n >= 3:
                page.screenshot(path=str(out_dir / "02_midrun.png"), full_page=True)
                midrun_shot = True
            if any(t in status for t in TERMINAL):
                break

        time.sleep(2)  # 留最后一帧 SSE 渲染
        page.screenshot(path=str(out_dir / "03_final.png"), full_page=True)

        # 切到产物与指标页签导出渲染结果
        export = {"run_id": run_id, "final_status": status, "samples": samples}
        try:
            page.click("button.tab[data-panel='artifactsPanel']")
            time.sleep(1)
            export["artifacts_text"] = page.inner_text("#artifacts")
            page.click("button.tab[data-panel='metricsPanel']")
            time.sleep(1)
            export["metrics_text"] = page.inner_text("#metrics")
            page.screenshot(path=str(out_dir / "04_metrics.png"), full_page=True)
        except Exception as exc:  # noqa: BLE001
            export["export_error"] = str(exc)
        page.click("button.tab[data-panel='tracePanel']")
        time.sleep(1)
        export["timeline_text"] = page.inner_text("#timeline")
        (out_dir / "page_export.json").write_text(
            json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        browser.close()

    print(f"[done] run_id={run_id} status={status} -> {out_dir}")
    return 0 if any(t in status for t in ("completed", "已完成")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
