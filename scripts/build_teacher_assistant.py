"""Build credential-free WorkBuddy expert and connector ZIPs, never publish them."""
import argparse
import hashlib
import json
from pathlib import Path

from akb.assistant_package import NAME, build_assistant, package_version
from akb.config import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--author-email", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/teacher-assistant")
    parser.add_argument("--engine", choices=["tencent", "weknora"], default="tencent")
    args = parser.parse_args()
    version = package_version(args.engine)
    expert, connector = build_assistant(args.origin, args.author_email,
        (ROOT / "integrations/workbuddy/assets/zhixu-512.png").read_bytes(), engine=args.engine)
    args.output.mkdir(parents=True, exist_ok=True)
    files = {}
    for label, payload in [("expert", expert), ("connector", connector)]:
        name = NAME + "-" + version + "-" + label + ".zip"
        target = args.output / name
        if target.exists() and target.read_bytes() != payload:
            raise SystemExit("目标已有不同内容，请选择新的输出目录；保留原文件")
        target.write_bytes(payload)
        files[name] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
    report = {"version": version, "engine": args.engine, "origin": args.origin, "user_independent": True,
              "published": False, "workbuddy_model_acceptance": "NOT_RUN", "files": files}
    (args.output / "build.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
