import asyncio
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from queue import Empty

from playwright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from myUtils.auth import check_cookie
from utils.base_social_media import set_init_script


SMS_INPUT_SELECTORS = [
    'input[placeholder*="验证码"]',
    'input[placeholder*="短信"]',
    'input[aria-label*="验证码"]',
    'input[name*="verify" i]',
    'input[name*="code" i]',
    'input[id*="verify" i]',
    'input[id*="code" i]',
    'input[data-testid*="code" i]',
    'input[inputmode="numeric"]'
]

SMS_SUBMIT_SELECTORS = [
    'button:has-text("确定")',
    'button:has-text("提交")',
    'button:has-text("验证")',
    'button:has-text("下一步")',
    'button:has-text("继续")',
    'button:has-text("确认")',
    'button:has-text("登录")',
    'button:has-text("Verify")',
    'button:has-text("Submit")',
    '[role="button"]:has-text("确定")',
    '[role="button"]:has-text("提交")',
    '[role="button"]:has-text("验证")'
]

SMS_SEND_SELECTORS = [
    'button:has-text("发送验证码")',
    'button:has-text("获取验证码")',
    'button:has-text("获取短信验证码")',
    'button:has-text("重新发送")',
    'button:has-text("发送")',
    'button:has-text("获取")',
    '[role="button"]:has-text("发送验证码")',
    '[role="button"]:has-text("获取验证码")',
    '[role="button"]:has-text("获取短信验证码")',
    '[role="button"]:has-text("重新发送")'
]

SMS_HINT_KEYWORDS = [
    '短信验证码',
    '请输入验证码',
    '手机验证',
    '安全验证',
    '二次验证',
    'verification code',
    'otp'
]

PHONE_MASK_PATTERNS = [
    r'1\d{2}\*{2,}\d{2,4}',
    r'\d{3}\*{2,}\d{2,4}'
]


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


async def _extract_masked_phone(frame):
    try:
        body_text = await frame.locator('body').inner_text()
    except Exception:
        return ''

    if not body_text:
        return ''

    for pattern in PHONE_MASK_PATTERNS:
        match = re.search(pattern, body_text)
        if match:
            return match.group(0)

    return ''


async def _frame_has_sms_hint(frame):
    for keyword in SMS_HINT_KEYWORDS:
        try:
            locator = frame.get_by_text(keyword, exact=False).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue

    try:
        body_text = await frame.locator('body').inner_text()
        if body_text:
            lower_text = body_text.lower()
            return any(keyword.lower() in lower_text for keyword in SMS_HINT_KEYWORDS)
    except Exception:
        pass

    return False


async def _find_sms_input_target(context):
    pages = [candidate_page for candidate_page in context.pages if not candidate_page.is_closed()]

    for candidate_page in reversed(pages):
        frames = [candidate_page.main_frame] + [frame for frame in candidate_page.frames if frame != candidate_page.main_frame]

        for frame in frames:
            frame_has_hint = await _frame_has_sms_hint(frame)

            for selector in SMS_INPUT_SELECTORS:
                try:
                    locator = frame.locator(selector)
                    count = await locator.count()
                    if count <= 0:
                        continue

                    for index in range(min(count, 3)):
                        input_locator = locator.nth(index)
                        if await input_locator.is_visible():
                            input_type = await input_locator.get_attribute('type')
                            has_sms_shape = selector != 'input[inputmode="numeric"]' or frame_has_hint
                            if has_sms_shape or (input_type and input_type.lower() in {'tel', 'number'} and frame_has_hint):
                                return {
                                    'page': candidate_page,
                                    'frame': frame,
                                    'input_locator': input_locator,
                                    'url': candidate_page.url,
                                    'masked_phone': await _extract_masked_phone(frame)
                                }
                except Exception:
                    continue

            if frame_has_hint:
                try:
                    fallback_input = frame.locator('input[type="tel"], input[type="number"], input[type="text"], input:not([type])').first
                    if await fallback_input.count() > 0 and await fallback_input.is_visible():
                        return {
                            'page': candidate_page,
                            'frame': frame,
                            'input_locator': fallback_input,
                            'url': candidate_page.url,
                            'masked_phone': await _extract_masked_phone(frame)
                        }
                except Exception:
                    continue

    return None


async def _trigger_sms_send(context, status_queue, emit_fail=True):
    pages = [candidate_page for candidate_page in context.pages if not candidate_page.is_closed()]

    for candidate_page in reversed(pages):
        frames = [candidate_page.main_frame] + [frame for frame in candidate_page.frames if frame != candidate_page.main_frame]

        for frame in frames:
            for selector in SMS_SEND_SELECTORS:
                try:
                    button = frame.locator(selector).first
                    if await button.count() <= 0 or not await button.is_visible():
                        continue

                    disabled = await button.get_attribute('disabled')
                    aria_disabled = await button.get_attribute('aria-disabled')
                    class_name = (await button.get_attribute('class') or '').lower()
                    if disabled is not None or aria_disabled in {'true', '1'} or 'disabled' in class_name:
                        if emit_fail:
                            _emit_event(status_queue, 'sms_send_cooldown', message='发送按钮暂不可用，请稍后重试')
                        return False

                    await button.click()
                    _emit_event(status_queue, 'sms_send_submitted', message='已点击发送验证码，请查看手机短信')
                    return True
                except Exception:
                    continue

            for keyword in ['发送验证码', '获取验证码', '获取短信验证码', '重新发送', '发送', '获取']:
                try:
                    fallback_button = frame.get_by_text(keyword, exact=False).first
                    if await fallback_button.count() > 0 and await fallback_button.is_visible():
                        await fallback_button.click()
                        _emit_event(status_queue, 'sms_send_submitted', message='已点击发送验证码，请查看手机短信')
                        return True
                except Exception:
                    continue

    if emit_fail:
        _emit_event(status_queue, 'sms_send_failed', message='未找到发送验证码按钮，等待官方短信弹窗出现后自动重试')
    return False


async def _submit_sms_code(context, sms_code, status_queue):
    target = await _find_sms_input_target(context)
    if not target:
        _emit_event(status_queue, 'sms_invalid', message='未找到验证码输入框，请稍后重试')
        return False

    frame = target['frame']
    input_locator = target['input_locator']

    try:
        await input_locator.click()
        await input_locator.fill('')
        await input_locator.type(sms_code, delay=80)
    except Exception:
        _emit_event(status_queue, 'sms_invalid', message='验证码输入失败，请重试')
        return False

    submitted = False

    for selector in SMS_SUBMIT_SELECTORS:
        try:
            button = frame.locator(selector).first
            if await button.count() <= 0 or not await button.is_visible():
                continue

            disabled = await button.get_attribute('disabled')
            aria_disabled = await button.get_attribute('aria-disabled')
            if disabled is not None or aria_disabled in {'true', '1'}:
                continue

            await button.click()
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        try:
            await input_locator.press('Enter')
            submitted = True
        except Exception:
            pass

    if submitted:
        _emit_event(status_queue, 'sms_submitted', message='验证码已提交，等待平台校验')

    return submitted


async def _wait_and_submit_sms_code(context, status_queue, session_context, timeout=180):
    wait_start = time.monotonic()
    last_progress = -1

    session_context['pending_sms_send'] = True

    while (time.monotonic() - wait_start) < timeout:
        elapsed = int(time.monotonic() - wait_start)
        remain = max(0, timeout - elapsed)

        sms_action = ''
        sms_action_queue = session_context.get('sms_action_queue')
        if sms_action_queue is not None:
            try:
                sms_action = str(sms_action_queue.get_nowait()).strip().lower()
            except Empty:
                sms_action = ''

        if sms_action in {'send', 'resend'}:
            session_context['pending_sms_send'] = True

        pending_sms_send = bool(session_context.get('pending_sms_send', False))
        last_sms_send_try_ts = int(session_context.get('last_sms_send_try_ts', 0) or 0)
        now_ts = int(time.time())
        if pending_sms_send and (now_ts - last_sms_send_try_ts >= 2):
            session_context['last_sms_send_try_ts'] = now_ts
            emit_fail = elapsed % 8 == 0
            if await _trigger_sms_send(context, status_queue, emit_fail=emit_fail):
                session_context['pending_sms_send'] = False
                session_context['sms_send_attempted'] = True
                session_context['last_sms_send_ts'] = now_ts

        sms_code = ''
        try:
            sms_code = str(session_context['sms_code_queue'].get_nowait()).strip()
        except Empty:
            sms_code = ''

        if sms_code:
            _emit_status(status_queue, 'sms_received', '已收到验证码，正在提交')
            if await _submit_sms_code(context, sms_code, status_queue):
                session_context['expecting_sms'] = False
                session_context['pending_sms_send'] = False
                session_context['last_sms_submit_ts'] = int(time.time())
                return True

            _emit_status(status_queue, 'sms_retry', '验证码提交失败，请重新输入')
            session_context['expecting_sms'] = True

        if elapsed // 5 != last_progress // 5:
            last_progress = elapsed
            _emit_status(status_queue, 'sms_waiting', '请先发送验证码，再输入短信验证码', remaining_seconds=remain)

        await asyncio.sleep(0.5)

    session_context['expecting_sms'] = False
    session_context['pending_sms_send'] = False
    _emit_result(status_queue, '500', '短信验证码输入超时，请重新发起登录')
    return False


async def _wait_for_login_signal(page, context, original_url, status_queue, qr_locator=None, timeout=200, session_context=None):
    start = time.monotonic()
    last_progress = -1
    seen_popup_urls = set()
    qr_changed_at = None
    url_changed_detected_at = None

    _emit_status(status_queue, 'wait_start', '已生成二维码，请扫码并在手机端确认授权')

    while (time.monotonic() - start) < timeout:
        elapsed = int(time.monotonic() - start)
        remain = max(0, timeout - elapsed)
        now_ts = int(time.time())

        if session_context is not None:
            sms_action_queue = session_context.get('sms_action_queue')
            sms_action = ''
            if sms_action_queue is not None:
                try:
                    sms_action = str(sms_action_queue.get_nowait()).strip().lower()
                except Empty:
                    sms_action = ''

            if sms_action in {'send', 'resend'}:
                session_context['pending_sms_send'] = True
                _emit_status(status_queue, 'sms_send_requested', '已收到发送验证码请求，等待官方短信弹窗出现后自动触发')

            pending_sms_send = bool(session_context.get('pending_sms_send', False))
            last_sms_send_try_ts = int(session_context.get('last_sms_send_try_ts', 0) or 0)
            if pending_sms_send and (now_ts - last_sms_send_try_ts >= 2):
                session_context['last_sms_send_try_ts'] = now_ts
                if await _trigger_sms_send(context, status_queue, emit_fail=False):
                    session_context['pending_sms_send'] = False
                    session_context['sms_send_attempted'] = True
                    session_context['expecting_sms'] = True
                    session_context['last_sms_send_ts'] = now_ts
                else:
                    last_sms_send_hint_ts = int(session_context.get('last_sms_send_hint_ts', 0) or 0)
                    if now_ts - last_sms_send_hint_ts >= 6:
                        session_context['last_sms_send_hint_ts'] = now_ts
                        _emit_status(status_queue, 'sms_send_pending', '暂未检测到官方短信弹窗按钮，检测到后会自动点击')

        try:
            for popup_page in context.pages[1:]:
                popup_url = popup_page.url or 'about:blank'
                if popup_url not in seen_popup_urls:
                    seen_popup_urls.add(popup_url)
                    _emit_status(status_queue, 'popup_detected', '检测到授权新窗口，等待完成授权', url=popup_url)
        except Exception:
            pass

        sms_target = await _find_sms_input_target(context)
        if sms_target and session_context is not None:
            last_sms_submit_ts = int(session_context.get('last_sms_submit_ts', 0) or 0)
            if now_ts - last_sms_submit_ts >= 8:
                session_context['expecting_sms'] = True
                session_context['sms_send_attempted'] = False
                session_context['pending_sms_send'] = True
                _emit_event(
                    status_queue,
                    'sms_required',
                    sessionId=session_context.get('session_id'),
                    message='检测到短信验证，请先发送验证码，再输入短信验证码',
                    maskedPhone=sms_target.get('masked_phone', ''),
                    timeoutSeconds=180,
                    canSendSms=True,
                    url=sms_target.get('url', '')
                )

                ok = await _wait_and_submit_sms_code(context, status_queue, session_context, timeout=180)
                if not ok:
                    return 'sms_timeout'

                _emit_status(status_queue, 'sms_continue', '短信验证码提交完成，继续等待授权完成')
                await asyncio.sleep(1)
                continue

        try:
            current_url = page.url
            if current_url and current_url != original_url:
                if url_changed_detected_at is None:
                    url_changed_detected_at = time.monotonic()
                    _emit_status(status_queue, 'url_changed', '检测到主页面跳转，继续观察是否出现短信验证弹窗', url=current_url)

                wait_after_url_change = int(time.monotonic() - url_changed_detected_at)
                no_pending_sms = session_context is None or not bool(session_context.get('pending_sms_send', False))
                if wait_after_url_change >= 8 and sms_target is None and no_pending_sms:
                    _emit_status(status_queue, 'url_changed_finalize', '页面跳转稳定，开始校验登录态', url=current_url)
                    return 'url_changed'
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
                        no_pending_sms = session_context is None or not bool(session_context.get('pending_sms_send', False))
                        if sms_target is None and no_pending_sms:
                            _emit_status(status_queue, 'post_scan_wait_done', '扫码后等待完成，开始校验登录态')
                            return 'qr_changed'
                        _emit_status(status_queue, 'post_scan_wait_hold', '扫码后检测到待处理短信验证，继续等待')
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
async def douyin_cookie_gen(user_name, status_queue, session_context=None):
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

            login_signal = await _wait_for_login_signal(
                page,
                context,
                original_url,
                status_queue,
                img_locator,
                session_context=session_context
            )
            if login_signal == 'sms_timeout':
                return

            await _finalize_cookie_and_store(3, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'抖音登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 视频号登录
async def get_tencent_cookie(user_name, status_queue, session_context=None):
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

            login_signal = await _wait_for_login_signal(
                page,
                context,
                original_url,
                status_queue,
                img_locator,
                session_context=session_context
            )
            if login_signal == 'sms_timeout':
                return

            await _finalize_cookie_and_store(2, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'视频号登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 快手登录
async def get_ks_cookie(user_name, status_queue, session_context=None):
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

            login_signal = await _wait_for_login_signal(
                page,
                context,
                original_url,
                status_queue,
                img_locator,
                session_context=session_context
            )
            if login_signal == 'sms_timeout':
                return

            await _finalize_cookie_and_store(4, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'快手登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()


# 小红书登录
async def xiaohongshu_cookie_gen(user_name, status_queue, session_context=None):
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

            login_signal = await _wait_for_login_signal(
                page,
                context,
                original_url,
                status_queue,
                img_locator,
                session_context=session_context
            )
            if login_signal == 'sms_timeout':
                return

            await _finalize_cookie_and_store(1, user_name, context, status_queue)
        except Exception as error:
            _emit_result(status_queue, '500', f'小红书登录流程异常: {error}')
        finally:
            await page.close()
            await context.close()
            await browser.close()
