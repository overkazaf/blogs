import sys
from playwright.sync_api import sync_playwright
d=sys.argv[1]
with sync_playwright() as p:
    b=p.chromium.launch(channel="chrome")
    pg=b.new_page(viewport={'width':1264,'height':800},device_scale_factor=2)
    for n in sys.argv[2:]:
        pg.goto(f'file://{d}/{n}.html'); pg.wait_for_load_state('networkidle')
        pg.evaluate("document.fonts.ready")
        pg.add_style_tag(content=".toolbar{display:none!important}.pulse-dot{animation:none!important}")
        pg.screenshot(path=f'{d}/{n}.png',full_page=True)
        print(n)
    b.close()
