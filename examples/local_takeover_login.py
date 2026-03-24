import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PLATFORM_URLS = {
    'xiaohongshu': 'https://creator.xiaohongshu.com/',
    'weixin': 'https://channels.weixin.qq.com/',
    'douyin': 'https://creator.douyin.com/',
    'kuaishou': 'https://cp.kuaishou.com/'
}


def parse_args():
    parser = argparse.ArgumentParser(description='本机浏览器接管登录：手动登录后导出Cookie JSON')
    parser.add_argument('--platform', required=True, choices=PLATFORM_URLS.keys(), help='平台名称')
    parser.add_argument('--output', required=True, help='导出的Cookie JSON文件路径')
    parser.add_argument('--chrome-path', default='', help='本机Chrome路径（可选）')
    parser.add_argument('--headless', action='store_true', help='无头模式（默认关闭）')
    return parser.parse_args()


async def run(args):
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    launch_options = {
        'headless': bool(args.headless),
        'args': [
            '--disable-blink-features=AutomationControlled',
            '--lang=zh-CN',
            '--disable-infobars',
            '--start-maximized'
        ]
    }

    if args.chrome_path:
        launch_options['executable_path'] = args.chrome_path

    target_url = PLATFORM_URLS[args.platform]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context()
        page = await context.new_page()

        print(f'已打开 {target_url}')
        print('请在弹出的浏览器中完成登录（含扫码/短信验证等步骤）。')

        await page.goto(target_url)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, '\n登录完成后，回到终端按回车导出 Cookie...')

        await context.storage_state(path=str(output_path))
        await context.close()
        await browser.close()

    print(f'Cookie 已导出到: {output_path}')
    print('现在可在账号管理里选择“本机浏览器接管”，上传该 JSON 文件。')


if __name__ == '__main__':
    cli_args = parse_args()
    asyncio.run(run(cli_args))
