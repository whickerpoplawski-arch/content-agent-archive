from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "http://127.0.0.1:8000/static/agent_console.html"
OUTPUT = Path("data/agent/ui-checks")


def check_viewport(page, name: str, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(URL, wait_until="networkidle")
    page.screenshot(path=str(OUTPUT / f"{name}.png"), full_page=True)

    assert page.locator("#goalInput").is_visible()
    assert page.locator("#tracePanel").is_visible()
    assert page.locator(".tool[data-tool='crawl_notes']").is_visible()
    assert page.locator(".tool[data-tool='sync_to_feishu']").is_visible()

    overflow = page.evaluate(
        """() => ({
          bodyX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          goalWidth: document.querySelector('#goalInput').scrollWidth > document.querySelector('#goalInput').clientWidth + 2,
          visibleText: document.body.innerText.includes('Agent Console') && document.body.innerText.includes('执行轨迹')
        })"""
    )
    assert overflow["bodyX"] is False
    assert overflow["goalWidth"] is False
    assert overflow["visibleText"] is True


def check_live_mock_run(page) -> None:
    run_detail_requests = []

    def record_request(request) -> None:
        if "/agent/runs/" in request.url and not request.url.endswith("/events"):
            run_detail_requests.append(request.url)

    page.on("request", record_request)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(URL, wait_until="networkidle")
    run_id = page.evaluate(
        """async () => {
          const response = await fetch('/agent/runs', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              goal: 'UI smoke test: create sample content without publishing',
              audience: 'college students',
              tone: 'practical',
              backend: 'mock',
              enabled_tools: ['load_notes','analyze_notes','generate_topics','generate_contents']
            })
          });
          const run = await response.json();
          localStorage.setItem('xhs-agent-last-run', run.run_id);
          return run.run_id;
        }"""
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "() => ['已完成','失败','已停止'].includes(document.querySelector('#runStatus').textContent)",
        timeout=15000,
    )
    page.wait_for_function(
        "() => document.querySelectorAll('#timeline .event').length >= 10",
        timeout=5000,
    )
    assert page.locator("#runId").inner_text().endswith(run_id)
    page.wait_for_timeout(800)
    requests_for_run = [url for url in run_detail_requests if url.endswith(f"/{run_id}")]
    assert len(requests_for_run) <= 3
    assert page.locator(".tool[data-tool='analyze_notes']").evaluate(
        "node => node.classList.contains('completed')"
    )
    page.locator(".tab[data-panel='artifactsPanel']").click()
    assert page.locator("#artifacts .artifact").count() >= 2
    page.screenshot(path=str(OUTPUT / "desktop-completed-run.png"), full_page=True)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        check_viewport(page, "desktop-1440x900", 1440, 900)
        check_viewport(page, "mobile-390x844", 390, 844)
        check_live_mock_run(page)
        browser.close()


if __name__ == "__main__":
    main()
