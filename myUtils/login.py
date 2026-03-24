import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path

from playwright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from myUtils.auth import check_cookie
from utils.base_social_media import set_init_script


# 统一获取浏览器启动配置（防风控+引入本地浏览器）
def get_browser_options():
    options = {
        'headless': LOCAL_CHROME_HEADLESS,
        'args': [
            '--disable-blink-features=AutomationControlled',
            '--lang=zh-CN',
            '--disable-infobars',
            '--start-maximized'
        ]
    }
    if LOCAL_CHROME_PATH:
        options['executable_path'] = LOCAL_CHROME_PATH
    return options


def _emit_event(status_queue, event_type, **payload):
    data = {
        'type': event_type,
        'ts': int(time.time())
    }
    data.update(payload)
    status_queue.put(json.dumps(data, ensure_ascii=False))


def _emit_status(status_queue, stage, message, **extra):
    _emit_event(status_queue, 'status', stage=stage, message=message, **extra)


def _emit_qr(status_queue, qr_data):
    _emit_event(status_queue, 'qr', data=qr_data)


def _emit_result(status_queue, code, message):
    _emit_event(status_queue, 'result', code=str(code), message=message)


async def _wait_for_login_signal(page, context, original_url, status_queue, qr_locator=None, timeout=200):
    start = time.monotonic()
    last_progress = -1
    seen_popup_urls = set()
    qr_changed_at = None

    _emit_status(status_queue, 'wait_start', '已生成二维码，请扫码并在手机端确认授权')

    while (time.monotonic() - start) < timeout:
        elapsed = int(time.monotonic() - start)
        remain = max(0, timeout - elapsed)

        try:
            current_url = page.url
            if current_url and current_url != original_url:
                _emit_status(status_queue, 'url_changed', '检测到主页面跳转，继续校验登录状态', url=current_url)
                return 'url_changed'
        except Exception:
            pass

        try:
            for popup_page in context.pages[1:]:
                popup_url = popup_page.url or 'about:blank'
                if popup_url not in seen_popup_urls:
                    seen_popup_urls.add(popup_url)
                    _emit_status(status_queue, 'popup_detected', '检测到授权新窗口，等待完成授权', url=popup_url)
        except Exception:
            pass

        if qr_locator is not None:
            try:
                qr_visible = await qr_locator.is_visible()
                if not qr_visible and qr_changed_at is None:
                    qr_changed_at = time.monotonic()
                    _emit_status(status_queue, 'qr_changed', '二维码状态已变化，等待手机端确认授权完成')

                if qr_changed_at is not None:
                    post_scan_wait = int(time.monotonic() - qr_changed_at)
                    if post_scan_wait > 0 and post_scan_wait % 5 == 0:
                        _emit_status(status_queue, 'post_scan_waiting', '已检测到扫码动作，等待平台完成授权回写', waited_seconds=post_scan_wait)
                    if post_scan_wait >= 30:
                        _emit_status(status_queue, 'post_scan_wait_done', '扫码后等待完成，开始校验登录态')
                        return 'qr_changed'
            except Exception:
                pass

        if elapsed // 5 != last_progress // 5:
            last_progress = elapsed
            _emit_status(status_queue, 'waiting', '等待扫码授权中', remaining_seconds=remain)

        await asyncio.sleep(1)

    _emit_status(status_queue, 'timeout', '等待扫码授权超时，尝试直接校验 Cookie')
    return 'timeout'


async def _finalize_cookie_and_store(platform_type, user_name, context, status_queue):
    uuid_v1 = uuid.uuid1()
    cookies_dir = Path(BASE_DIR / 'cookiesFile')
    cookies_dir.mkdir(exist_ok=True)

    cookie_file_name = f'{uuid_v1}.json'
    cookie_file_path = cookies_dir / cookie_file_name

    _emit_status(status_queue, 'cookie_saved', '已保存登录态，正在校验 Cookie 有效性')

    result = False
    max_attempts = 10
    for attempt in range(1, max_attempts + 1):
        await context.storage_state(path=cookie_file_path)
        _emit_status(status_queue, 'cookie_checking', f'登录态校验中（第 {attempt}/{max_attempts} 次）')
        result = await check_cookie(platform_type, cookie_file_name)
        if result:
            break
        if attempt < max_attempts:
            _emit_status(status_queue, 'cookie_retry_wait', '登录态尚未稳定，等待后重试')
            await asyncio.sleep(5)

    if not result:
        _emit_result(status_queue, '500', 'Cookie 校验失败，请确认手机端已完成授权后重试')
        return False

    with sqlite3.connect(Path(BASE_DIR / 'db' / 'database.db')) as conn:
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO user_info (type, filePath, userName, status)
            VALUES (?, ?, ?, ?)
            ''',
            (platform_type, cookie_file_name, user_name, 1)
        )
        conn.commit()

    _emit_result(status_queue, '200', '账号添加成功')
    return True


# 抖音登录
async def douyin_cookie_gen(user_name, status_queue):
    async with async_playwright() as playwright:
        options = get_browser_options()
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()

        try:
            _emit_status(status_queue, 'open_login_page', '正在打开抖音创作者登录页')
            await page.goto('https://creator.douyin.com/')
            original_url = page.url

            img_locator = page.get_by_role('img', name='二维码')
            src = await img_locator.get_attribute('src')
            _emit_qr(status_queue, src)

            await _wait_for_login_signal(page, context, original_url, status_queue, img_locator)
            await _finalize_cookie_and_store(3, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'抖音登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 视频号登录
async def get_tencent_cookie(user_name, status_queue):
    async with async_playwright() as playwright:
        options = get_browser_options()
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()

        try:
            _emit_status(status_queue, 'open_login_page', '正在打开视频号登录页')
            await page.goto('https://channels.weixin.qq.com')
            original_url = page.url

            iframe_locator = page.frame_locator('iframe').first
            img_locator = iframe_locator.get_by_role('img').first
            src = await img_locator.get_attribute('src')
            _emit_qr(status_queue, src)

            await _wait_for_login_signal(page, context, original_url, status_queue, img_locator)
            await _finalize_cookie_and_store(2, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'视频号登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 快手登录
async def get_ks_cookie(user_name, status_queue):
    async with async_playwright() as playwright:
        options = get_browser_options()
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()

        try:
            _emit_status(status_queue, 'open_login_page', '正在打开快手登录页')
            await page.goto('https://cp.kuaishou.com')
            await page.get_by_role('link', name='立即登录').click()
            await page.get_by_text('扫码登录').click()
            original_url = page.url

            img_locator = page.get_by_role('img', name='qrcode')
            src = await img_locator.get_attribute('src')
            _emit_qr(status_queue, src)

            await _wait_for_login_signal(page, context, original_url, status_queue, img_locator)
            await _finalize_cookie_and_store(4, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'快手登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 小红书登录
async def xiaohongshu_cookie_gen(user_name, status_queue):
    async with async_playwright() as playwright:
        options = get_browser_options()
        browser = await playwright.chromium.launch(**options)
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()

        try:
            _emit_status(status_queue, 'open_login_page', '正在打开小红书登录页')
            await page.goto('https://creator.xiaohongshu.com/')
            await page.locator('img.css-wemwzq').click()
            original_url = page.url

            img_locator = page.get_by_role('img').nth(2)
            src = await img_locator.get_attribute('src')
            _emit_qr(status_queue, src)

            await _wait_for_login_signal(page, context, original_url, status_queue, img_locator)
            await _finalize_cookie_and_store(1, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'小红书登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()
