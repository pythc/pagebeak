"""Render our fixed, small Markdown tutorial subset without runtime dependencies."""

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inline(text):
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"https?://[^\s<，。；]+", lambda m: f'<a href="{m[0]}">{m[0]}</a>', text)


def build(source_name="新手使用教程.md", target_name="zhixu-guide.html", footer="本机版 0.8.0"):
    source = (ROOT / "docs" / source_name).read_text()
    out, navigation = [], []
    list_open, section_open, section = False, False, 0
    for line in source.splitlines():
        if not line.startswith("- ") and list_open:
            out.append("</ul>")
            list_open = False
        if line.startswith("# "):
            out.append('<span class="eyebrow">知序 · 从第一份资料开始</span><h1>从打开 WorkBuddy<br>开始使用知序</h1>')
        elif line.startswith("## "):
            if section_open:
                out.append("</section>")
            section += 1
            title = line[3:]
            navigation.append(f'<a href="#step{section}">{html.escape(title)}</a>')
            out.append(f'<section id="step{section}"><h2>{html.escape(title)}</h2>')
            section_open = True
        elif line.startswith("> "):
            out.append('<div class="sample"><blockquote>' + inline(line[2:]) +
                       '</blockquote><button type="button" class="copy">复制这段话</button></div>')
        elif line.startswith("- "):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append("<li>" + inline(line[2:]) + "</li>")
        elif line.strip():
            out.append("<p>" + inline(line) + "</p>")
    if list_open:
        out.append("</ul>")
    if section_open:
        out.append("</section>")
    template = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>新手教程 · 知序</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#233348;background:#f6f8fb;line-height:1.85;font-size:16px;scroll-behavior:smooth}*{box-sizing:border-box}body{margin:0}a{color:#325ed6;text-underline-offset:4px;overflow-wrap:anywhere}header{background:#fff;border-bottom:1px solid #e4e9f1;padding:16px max(22px,calc((100vw - 1160px)/2));display:flex;justify-content:space-between;gap:16px;align-items:center}header a{text-decoration:none}header img{width:32px;vertical-align:middle;margin-right:10px}header strong{color:#233348}header nav{display:flex;gap:22px}.layout{max-width:1160px;display:grid;grid-template-columns:220px minmax(0,1fr);gap:36px;margin:34px auto;padding:0 22px}aside{align-self:start;position:sticky;top:22px;font-size:14px}aside a{display:block;color:#526176;text-decoration:none;padding:7px 0}aside a:hover{color:#325ed6}main{background:white;border:1px solid #e4e9f1;border-radius:18px;padding:38px 44px;min-width:0}.eyebrow{color:#5374b2;font-weight:600;font-size:14px}h1{line-height:1.3;font-size:36px;letter-spacing:-1px;margin:14px 0 24px}h2{font-size:23px;line-height:1.5;margin:0 0 18px}p{margin:14px 0}section{padding:30px 0 16px;border-top:1px solid #edf0f5;scroll-margin-top:24px}ul{padding-left:22px}li{padding:6px 0}.sample{background:#f1f5ff;border:1px solid #dce6ff;border-radius:12px;padding:18px 20px;margin:20px 0}blockquote{margin:0}button{border:1px solid #c4d4fb;background:white;color:#325ed6;border-radius:7px;padding:7px 12px;font:inherit;font-size:13px;cursor:pointer;margin-top:12px}button:focus-visible,a:focus-visible{outline:3px solid #a7bffa;outline-offset:4px}code{background:#eef1f5;padding:2px 5px;border-radius:4px;overflow-wrap:anywhere}footer{text-align:center;color:#6d798a;font-size:13px;padding:0 22px 32px}.notice{font-size:13px;color:#69778d}#copy-status{min-height:24px;color:#456326}
@media(max-width:760px){.layout{display:block;margin:18px auto;padding:0 14px}aside{position:static;margin:0 0 20px;padding:12px 18px;background:#edf2f9;border-radius:12px}aside a{display:inline-block;margin-right:16px;font-size:13px}main{padding:25px 20px}h1{font-size:29px}h2{font-size:21px}header{padding:14px 18px}header nav{gap:14px;font-size:14px}}
@media print{aside,header,.copy,footer{display:none}.layout{display:block;margin:0;padding:0}main{border:0;padding:0}section{break-inside:avoid}a{color:inherit}body{background:white}}
</style></head><body><header><a href="/start"><img src="/static/img/zhixu-mark-v1.svg" alt=""><strong>知序</strong></a><nav><a href="/start">开始使用</a><a href="/home">我的文件</a><a href="/knowledge">知识目录</a></nav></header><div class="layout"><aside aria-label="教程目录">{{NAV}}</aside><main>{{CONTENT}}<div id="copy-status" role="status" aria-live="polite"></div></main></div><footer>本机版 0.8.0 · 2026-10-31 · 语义整理由当前 WorkBuddy 对话执行</footer>
<script>document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{const text=button.previousElementSibling.innerText;try{await navigator.clipboard.writeText(text);button.textContent='已复制';document.querySelector('#copy-status').textContent='已复制，请切回 WorkBuddy 粘贴并发送。'}catch{document.querySelector('#copy-status').textContent='浏览器没有允许复制，请选中上方文字手动复制。'}}));</script></body></html>'''
    target = ROOT / "integrations/cloudreve/overlay/public" / target_name
    target.write_text(template.replace("{{NAV}}", "".join(navigation)).replace("{{CONTENT}}", "\n".join(out))
                      .replace("本机版 0.8.0", html.escape(footer)))
    print("新手教程已生成：" + str(target.relative_to(ROOT)))


if __name__ == "__main__":
    build()
    build("教师助理试点指南.md", "teacher-assistant-guide.html", "校园助理预览版 0.13.3 · 尚未正式发布")
