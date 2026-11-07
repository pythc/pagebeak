"""Loopback-only synthetic forms for browser readback and genuine WorkBuddy tests."""

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

from akb.config import ROOT

FIELDS = {
    "name": "姓名",
    "title": "当前职称",
    "degree": "学位",
    "amount": "2025年主持且已结题项目批准经费（万元）",
    "graduation": "毕业日期",
    "phone": "联系电话",
    "narrative": "个人事迹",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    folder = (ROOT / args.folder).resolve()
    assert folder.is_relative_to(ROOT / "work/local-documents-acceptance-20261026")
    auth = json.loads((folder / "private/model-session.json").read_text())
    kb = auth["knowledge_base_id"]
    link = "workbuddy://task?" + urlencode(
        {
            "action": "start",
            "expertId": "zhixu-" + kb + "-expert",
            "prompt": f"请读取 {folder / 'agent-space/验收任务.txt'} 并实际执行本次本地合成资料验收。仅使用本任务绑定的知序 MCP。",
            "cwd": str(folder / "agent-space"),
            "permissionMode": "default",
        }
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            if self.path in {"/model/saved", "/protocol/saved"}:
                kind = self.path.split("/")[1]
                path = folder / ("web-" + kind + "-readback.json")
                if not path.exists():
                    return self.send_error(404)
                return self.saved(json.loads(path.read_text()))
            if self.path == "/launch":
                return self.send(
                    f'<html lang="zh-CN"><meta charset="utf-8"><h1>本机文档合成验收</h1><a href="{html.escape(link)}">打开知序管家执行验收</a></html>'
                )
            if self.path not in {"/model", "/protocol"}:
                return self.send_error(404)
            inputs = "".join(
                f'<label>{label}<textarea name="{key}" id="{key}" rows="{4 if key == "narrative" else 1}"></textarea></label>'
                for key, label in FIELDS.items()
            )
            self.send(
                '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>教师个人资料登记表</title><style>'
                "body{font:16px system-ui;max-width:760px;margin:32px auto;padding:0 24px;color:#233348}label{display:block;margin:14px 0}textarea{display:block;width:100%;padding:8px;box-sizing:border-box;font:inherit}button{padding:10px 22px}</style>"
                "<h1>教师个人资料登记表</h1><p>经费仅统计2025年本人主持且已结题项目的批准额，按编号去重，单位万元。学位授予日期不能代替毕业日期；未知项留空。</p>"
                f'<form method="post" action="{self.path}">{inputs}<button>保存登记表</button></form></html>'
            )

        def do_POST(self):
            if self.path not in {"/model", "/protocol"}:
                return self.send_error(404)
            count = int(self.headers.get("Content-Length", "0"))
            if count > 50000:
                return self.send_error(413)
            body = parse_qs(self.rfile.read(count).decode(), keep_blank_values=True)
            if set(body) != set(FIELDS):
                return self.send_error(422)
            values = {k: body[k][0] for k in FIELDS}
            (folder / ("web-" + self.path[1:] + "-readback.json")).write_text(
                json.dumps(values, ensure_ascii=False, indent=2)
            )
            self.saved(values)

        def saved(self, values):
            table = "".join(
                f'<tr><th>{label}</th><td id="{key}">{html.escape(values[key])}</td></tr>'
                for key, label in FIELDS.items()
            )
            self.send(
                f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>已保存登记表</title><style>body{{font:16px system-ui;max-width:900px;margin:32px auto;padding:0 24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:12px;text-align:left;white-space:pre-wrap}}</style><h1>登记表已保存</h1><table>{table}</table></html>'
            )

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    (folder / "server.json").write_text(
        json.dumps(
            {
                "url": url,
                "model_url": url + "/model",
                "protocol_url": url + "/protocol",
                "launch_url": url + "/launch",
            }
        )
    )
    from docx import Document

    template = Document()
    from accept_local_documents import clean_title_borders

    clean_title_borders(template)
    template.add_heading("教师个人资料登记表", 0)
    table = template.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "项目", "填写内容"
    for label in FIELDS.values():
        table.add_row().cells[0].text = label
    if not (folder / "agent-space/空白登记表.docx").exists():
        template.save(folder / "agent-space/空白登记表.docx")
    task = folder / "agent-space/验收任务.txt"
    if "随后实际填写并保存" not in task.read_text():
        task.write_text(
            task.read_text()
            + f"\n随后实际填写并保存 {url}/model 的网页表单，将空白登记表.docx填写后另存为 模型填写结果.docx。"
            "网页和Word都须实际回读，用validate_filling分别核验；不要将草稿值当回读。模型结果.json使用"
            '{"receipt_id":"填写回执UUID","values":{"name":"","title":"","degree":"","amount":"","graduation":"","phone":"","narrative":""}}。'
            "amount仅2025年本人主持且已结题项目批准额，单位万元；narrative由compose生成，其他字段未知留空。"
            "网页与Word保存到本次合成验收，不对外提交。"
        )
    print(json.dumps({"url": url, "folder": str(folder)}, ensure_ascii=False), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
