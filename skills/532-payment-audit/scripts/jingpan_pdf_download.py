import os
import sys
import time
import uuid
import json
import asyncio
import hashlib
import shutil
import requests
from urllib.parse import quote
from typing import List

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from playwright.async_api import async_playwright, Page
# 注意：仍需安装 playwright-stealth
from playwright_stealth import Stealth

# ==================== 配置区 ====================
# 建议通过 shell 环境变量传入，避免硬编码
SSA_USERNAME = os.getenv("SSA_USERNAME", "你的用户名")
SSA_PASSWORD = os.getenv("SSA_PASSWORD", "你的密码")
SSA_APP_KEY = os.getenv("SSA_APP_KEY", "你的APP_KEY")
SSA_TOKEN = os.getenv("SSA_TOKEN", "你的TOKEN")


# ==================== SSA 安全登录逻辑 ====================
def align(key: str) -> bytes:
    key_bytes = key.encode('utf-8')
    return key_bytes[:16] if len(key_bytes) > 16 else key_bytes.ljust(16, b'\0')


def encrypt_aes_cbc(plain_text: str, key_str: str) -> str:
    key_bytes = align(key_str)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=key_bytes)
    plain_bytes = plain_text.encode('utf-8')
    cipher_text = cipher.encrypt(pad(plain_bytes, AES.block_size))
    return cipher_text.hex()


def get_ssa_cookie(return_url: str) -> str:
    encrypted_pwd = encrypt_aes_cbc(SSA_PASSWORD, SSA_TOKEN)
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())

    parameters = {"password": encrypted_pwd, "encoder": "aes", "username": SSA_USERNAME}
    json_str = json.dumps(parameters, separators=(',', ':'))
    sign_str = SSA_TOKEN + json_str + timestamp + nonce
    signature = hashlib.md5(sign_str.encode('utf-8')).hexdigest()

    params = {
        "password": encrypted_pwd, "signature": signature, "appkey": SSA_APP_KEY,
        "encoder": "aes", "nonce": nonce, "username": SSA_USERNAME, "timestamp": timestamp
    }

    try:
        resp = requests.post("http://ssa.jd.com/api/pwd/code", data=params, timeout=10)
        data = resp.json()
        if not data.get("success"):
            print(f"[ERROR] 获取验证码失败: {resp.text}")
            return None

        login_params = {
            "username": SSA_USERNAME, "password": SSA_PASSWORD,
            "appKey": SSA_APP_KEY, "verificationCode": data["data"]
        }
        login_url = f"http://ssa.jd.com/sso/login?ReturnUrl={quote(return_url)}"
        r = requests.post(login_url, data=login_params, allow_redirects=False, timeout=10)

        cookies = r.cookies.get_dict(domain=".jd.com")
        sso_cookie = cookies.get("sso.jd.com")
        if not sso_cookie:
            print("[ERROR] SSA 登录失败，未获取到 sso.jd.com cookie")
            return None
        return sso_cookie
    except Exception as e:
        print(f"[ERROR] SSA 认证流程异常: {e}")
        return None


# ==================== 鲸盘自动化操作 ====================
async def click_folder(page: Page, folder_name: str):
    locator = page.locator(f'#fileList span[title="{folder_name}"]').first
    await locator.scroll_into_view_if_needed()
    await locator.click(timeout=10000)
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(1)


async def download_contract(url: str, year: str, keyword: str, base_dir: str = "./downloads"):
    task_id = str(uuid.uuid4())[:8]
    unique_task_dir = os.path.abspath(os.path.join(base_dir, f"task_{task_id}"))
    os.makedirs(unique_task_dir, exist_ok=True)

    print(f"[INFO][Task {task_id}] 启动任务 | 目录: {unique_task_dir}")

    sso_cookie = get_ssa_cookie(url)
    if not sso_cookie:
        return None

    browser = None
    try:
        async with async_playwright() as p:
            # 针对 bash/headless 环境优化的启动参数
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context()
            await Stealth().apply_stealth_async(context)

            await context.add_cookies([{"name": "sso.jd.com", "value": sso_cookie, "domain": ".jd.com", "path": "/"}])
            page = await context.new_page()

            print(f"[INFO][Task {task_id}] 正在访问页面...")
            await page.goto(url)
            await page.wait_for_selector("#fileList", timeout=30000)

            # 导航路径
            target_folders = ["应付审批文件", "客商签约合同", f"{year}年合同"]
            for folder in target_folders:
                print(f"[INFO][Task {task_id}] 进入目录: {folder}")
                await click_folder(page, folder)

            # 搜索
            print(f"[INFO][Task {task_id}] 搜索关键词: {keyword}")
            search_input = page.locator('div.search-frame input.search-put')
            await search_input.type(keyword, delay=50)
            await page.locator('div.search-frame span').click()
            await asyncio.sleep(2)

            # 下载逻辑
            file_item = page.locator('#fileList li.line.shareLine').first
            if await file_item.is_visible():
                async with page.expect_download(timeout=60000) as download_info:
                    await file_item.hover()
                    await file_item.locator('i.behavior.download').click()

                download = await download_info.value
                save_path = os.path.join(unique_task_dir, download.suggested_filename)
                await download.save_as(save_path)
                print(f"[SUCCESS][Task {task_id}] 下载完成: {save_path}")
                return save_path
            else:
                print(f"[WARN][Task {task_id}] 未找到匹配文件: {keyword}")
                return None

    except Exception as e:
        print(f"[ERROR][Task {task_id}] 运行时异常: {e}")
        return None
    finally:
        if browser:
            await browser.close()


async def main():
    if len(sys.argv) < 4:
        print('Usage: python script.py [URL] [YEAR] [KEYWORD]')
        sys.exit(1)

    jp_url = sys.argv[1]
    year = sys.argv[2]
    key = sys.argv[3]

    await download_contract(jp_url, year, key)


if __name__ == "__main__":
    asyncio.run(main())