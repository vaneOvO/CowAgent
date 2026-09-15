import sys
import json
import traceback
from datetime import datetime
from playwright.sync_api import sync_playwright


def create_user(amount: str):
    username = "user" + datetime.now().strftime("%Y%m%d%H%M")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        page = context.new_page()
        page.set_default_timeout(15000)

        # 1. 登录
        page.goto("https://system.gravitex.ai/", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        if "/login" in page.url:
            page.wait_for_selector("input", state="visible")
            inputs = page.locator("input:visible")
            if inputs.count() >= 2:
                inputs.nth(0).fill("vane")
                inputs.nth(1).fill("Gravitex@2805")
            page.locator("button:visible").last.click()
            page.wait_for_url("**/analytics**", timeout=15000)

        # 2. 进入用户管理
        page.locator("text=用户管理").first.click()
        page.wait_for_url("**/user**", timeout=10000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        # 3. 点击新增（按钮文本是"新 增"，中间有空格）
        page.locator(".ant-btn-primary:has-text('新')").last.click()
        page.wait_for_selector("[data-state='open'] input[name='userName']", state="visible", timeout=5000)
        page.wait_for_timeout(300)

        # 4. 填写表单
        page.locator("[data-state='open'] input[name='userName']").fill(username)
        page.locator("[data-state='open'] input[name='nickName']").fill(username)
        page.locator("[data-state='open'] input[name='amount']").fill(amount)

        # 5. 读取系统生成的密码（必须在点击确认之前，确认后dialog会关闭）
        password = ""
        pwd_input = page.locator("[data-state='open'] input[name='password']")
        if pwd_input.count() > 0:
            password = pwd_input.input_value()

        # 6. 点击确认
        page.locator("[data-state='open'] .ant-btn-primary:has-text('确')").click()
        page.wait_for_timeout(2000)

        # 7. 获取结果
        result_text = ""
        for selector in [".ant-message-notice", ".ant-notification", ".ant-modal-body"]:
            els = page.locator(selector)
            if els.count() > 0:
                for j in range(els.count()):
                    t = els.nth(j).inner_text()
                    if t.strip():
                        result_text += t.strip() + "\n"

        if not result_text:
            result_text = "操作成功"

        browser.close()

    print(json.dumps({
        "success": True,
        "username": username,
        "password": password,
        "amount": amount,
        "page_result": result_text.strip()[:2000]
    }, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "Usage: python create_user.py <amount>"}))
        sys.exit(1)
    try:
        create_user(sys.argv[1])
    except Exception as e:
        print(json.dumps({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }, ensure_ascii=False))
