"""Synthetic local roots only. No home-directory inventory or model invocation."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from akb.local_index import LocalIndex, open_original


@pytest.fixture
def local(tmp_path):
    root = tmp_path.resolve() / "教师资料"
    root.mkdir()
    index = LocalIndex(str(uuid4()), tmp_path.resolve() / "cache")
    return index, root


def scan(index, config):
    started = index.start(config["revision"], spawn=False)
    index.scan(started["job_id"])
    return index.status(started["job_id"])["job"]


def test_cjk_short_words_exact_raw_locators_and_cross_session(local):
    index, root = local
    (root / "聘任记录.txt").write_text(
        "李明获得工程硕士学位。\n经核定，现聘任为副教授。\n项目 P26-A 批准经费 128431.25元。"
    )
    config = index.configure(enabled=True, roots=[str(root)])
    first = scan(index, config)
    assert first["status"] == "completed" and first["parsed"] == 1 and first["model_calls"] == 0
    for query in ("李明", "硕士", "副教授", "P26-A", "批准 经费"):
        assert len(index.search(query)["items"]) == 1
    hit = index.search("硕士")["items"][0]
    read = index.read(hit["id"], hit["sha256"])
    assert read["blocks"][0]["text"] == "李明获得工程硕士学位。"
    assert read["blocks"][0]["locator"]["line"] == 1 and not read["formal_evidence"]
    other_session = LocalIndex(index.kb, index.directory.parent)
    assert other_session.search("副教授")["items"][0]["id"] == hit["id"]
    assert scan(other_session, config)["unchanged"] == 1


def test_changes_rename_copy_delete_and_reject_stale_read(local):
    index, root = local
    original = root / "任职.txt"
    original.write_text("张澄目前任讲师。")
    config = index.configure(enabled=True, roots=[str(root)])
    scan(index, config)
    hit = index.search("张澄")["items"][0]
    original.rename(root / "新任职.txt")
    assert scan(index, config)["renamed"] == 1
    assert index.catalog()["items"][0]["id"] == hit["id"]
    changed = root / "新任职.txt"
    changed.write_text("张澄现任副教授。")
    with pytest.raises(ValueError, match="已变化"):
        index.read(hit["id"], hit["sha256"])
    assert index.search("讲师")["items"] == []
    assert scan(index, config)["parsed"] == 1
    (root / "复印件.txt").write_bytes(changed.read_bytes())
    scan(index, config)
    assert len(index.search("副教授")["items"]) == 2
    changed.unlink()
    scan(index, config)
    assert len(index.search("副教授")["items"]) == 1
    ops = [x["operation"] for x in index.changes()["items"]]
    assert {"added", "renamed", "modified", "changed_since_scan", "missing"} <= set(ops)


def test_no_links_hidden_secrets_outside_roots_and_scope_revocation(local, tmp_path):
    index, root = local
    outside = tmp_path / "外部.txt"
    outside.write_text("外部敏感内容")
    (root / "正常.txt").write_text("本人的教学记录")
    (root / ".hidden.txt").write_text("隐藏敏感内容")
    (root / "secrets").mkdir()
    (root / "secrets" / "token.txt").write_text("凭据敏感内容")
    (root / "链接.txt").symlink_to(outside)
    os.link(outside, root / "硬链接.txt")
    config = index.configure(enabled=True, roots=[str(root)])
    scan(index, config)
    assert index.catalog()["total"] == 1
    hit = index.search("教学")["items"][0]
    (root / "正常.txt").unlink()
    (root / "正常.txt").symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        index.read(hit["id"], hit["sha256"])
    index.configure(enabled=False)
    with pytest.raises(ValueError, match="尚未启用"):
        index.search("教学")
    with pytest.raises(ValueError):
        with open_original(root, "../外部.txt"):
            pass


def test_parent_symlink_replacement_and_tenant_isolation(local, tmp_path):
    index, root = local
    sub = root / "科研"
    sub.mkdir()
    (sub / "项目.txt").write_text("科研项目金额五万元")
    config = index.configure(enabled=True, roots=[str(root)])
    scan(index, config)
    hit = index.search("科研")["items"][0]
    sub.rename(root / "原科研")
    other = tmp_path / "另一目录"
    other.mkdir()
    (other / "项目.txt").write_text("科研项目金额五万元")
    sub.symlink_to(other, target_is_directory=True)
    with pytest.raises(OSError):
        index.read(hit["id"], hit["sha256"])
    isolated = LocalIndex(str(uuid4()), index.directory.parent)
    isolated.configure(enabled=True, roots=[str(root)])
    assert isolated.catalog()["total"] == 0
    with pytest.raises(ValueError):
        isolated.read(hit["id"], hit["sha256"])


def test_supported_office_ocr_deferred_corrupt_and_budget(local):
    from docx import Document
    from openpyxl import Workbook
    from PIL import Image
    from pptx import Presentation

    index, root = local
    doc = Document()
    doc.add_paragraph("陈婉清任教研室主任，参与青岚项目。")
    doc.save(root / "履历.docx")
    workbook = Workbook()
    workbook.active.append(["项目", "金额（元）"])
    workbook.active.append(["青岚", 128431.25])
    workbook.save(root / "台账.xlsx")
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[1])
    slide.shapes.title.text = "课程建设"
    slide.placeholders[1].text = "陈婉清承担教学80学时。"
    ppt.save(root / "教学.pptx")
    Image.new("RGB", (50, 50), "white").save(root / "扫描.png")
    (root / "旧资料.doc").write_bytes(b"legacy")
    (root / "损坏.docx").write_bytes(b"broken")
    with (root / "巨型.txt").open("wb") as stream:
        stream.truncate(129 * 1024**2)
    config = index.configure(enabled=True, roots=[str(root)])
    result = scan(index, config)
    assert result["status"] == "partial" and result["parsed"] == 3 and result["deferred"] == 3
    items = {Path(i["path"]).suffix: i for i in index.catalog()["items"]}
    assert items[".png"]["status"] == "deferred"
    assert index.search("128431")["items"]
    assert index.search("80")["items"]
    second = scan(index, config)
    assert second["unchanged"] == 6 and second["parsed"] == 0


def test_offline_and_partial_walk_do_not_claim_deleted(local):
    index, root = local
    (root / "教学.txt").write_text("教育教学奖励")
    conf = index.configure(enabled=True, roots=[str(root)])
    scan(index, conf)
    root.rename(root.parent / "暂时离线")
    assert scan(index, conf)["status"] == "partial"
    assert index.catalog()["items"][0]["status"] == "offline"
    assert not index.search("教学")["items"]


def test_all_documents_only_selected_home_and_no_default_scan(local, monkeypatch):
    index, root = local
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
    conf = index.configure(enabled=True, all_documents=True)
    assert conf["roots"] == [str(root)] and not conf["auto_import"] and not conf["share_catalog"]
    assert index.status()["job"] is None


def test_metadata_sync_opt_in_no_body_absolute_path_or_excerpt(local):
    index, root = local
    (root / "资料.txt").write_text("正文不应随清单上传")
    conf = index.configure(enabled=True, roots=[str(root)])
    scan(index, conf)
    called = []

    def api(method, path, body=None):
        called.append((method, path, body))
        return {"revision": 1}

    assert index.publish_catalog(api)["status"] == "local_only" and not called
    conf = index.configure(enabled=True, roots=[str(root)], share_catalog=True)
    assert index.publish_catalog(api)["status"] == "synced"
    snapshot = called[0][2]
    assert "正文不应" not in json.dumps(snapshot, ensure_ascii=False) and str(root) not in json.dumps(
        snapshot
    )
    assert snapshot["items"][0]["path"] == "资料.txt"
    index.configure(enabled=False)
    index.publish_catalog(api)
    assert called[-1][2]["items"] == []


def test_index_read_hash_changes_even_same_size_and_mtime(local):
    index, root = local
    path = root / "聘任.txt"
    path.write_text("现任讲师")
    config = index.configure(enabled=True, roots=[str(root)])
    scan(index, config)
    hit = index.search("讲师")["items"][0]
    before = path.stat()
    path.write_text("现任教授")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="已变化"):
        index.read(hit["id"], hit["sha256"])


def test_scan_completion_waits_for_catalog_publication(local):
    index, root = local
    (root / "教学.txt").write_text("教学成果")
    conf = index.configure(enabled=True, roots=[str(root)], share_catalog=True)
    job = index.start(conf["revision"], spawn=False)

    def api(*_args):
        assert index.status(job["job_id"])["job"]["status"] == "syncing"
        return {"revision": 1}

    index.scan(job["job_id"], api)
    done = index.status(job["job_id"])["job"]
    assert done["status"] == "completed" and done["catalog_sync"]["status"] == "synced"


def test_import_requires_authorization_budget_and_binds_dedup_request(local):
    index, root = local
    (root / "资料.txt").write_text("教学信息")
    conf = index.configure(enabled=True, roots=[str(root)], import_budget_bytes=1)
    scan(index, conf)
    entry = index.catalog()["items"][0]
    args = (entry["id"], entry["sha256"], str(uuid4()))

    def api(_method, path, _body=None):
        assert path == "/v2/local/match"
        return {"source": None}

    with pytest.raises(ValueError, match="只授权本机检索"):
        index.import_document(api, *args)
    with pytest.raises(ValueError, match="预算不足"):
        index.import_document(api, *args, user_authorized=True)
    source_id = str(uuid4())

    def dedup(_method, path, _body=None):
        assert path == "/v2/local/match"
        return {"source": {"id": source_id}}

    imported = index.import_document(dedup, *args, user_authorized=True)
    assert imported["deduplicated"] and imported["source"]["id"] == source_id
    assert index.import_document(dedup, *args, user_authorized=True)["replayed"]
    with pytest.raises(ValueError, match="不同文件、版本或归属"):
        index.import_document(dedup, *args, subject_scope="self", user_authorized=True)


def test_unreadable_original_is_not_reported_as_deleted(local, monkeypatch):
    import akb.local_index as module

    index, root = local
    (root / "资料.txt").write_text("教学资料")
    conf = index.configure(enabled=True, roots=[str(root)])
    scan(index, conf)

    def denied(*_args, **_kwargs):
        raise PermissionError("test permission")

    monkeypatch.setattr(module, "open_original", denied)
    assert scan(index, conf)["status"] == "partial"
    assert index.catalog()["items"][0]["status"] == "offline"
    assert not any(e["operation"] == "missing" for e in index.changes()["items"])


def test_new_exclusion_is_effective_without_another_scan(local):
    index, root = local
    (root / "个人记录.txt").write_text("需要移出授权范围的内容")
    conf = index.configure(enabled=True, roots=[str(root)])
    scan(index, conf)
    hit = index.search("内容")["items"][0]
    index.configure(enabled=True, roots=[str(root)], exclude=["个人记录.txt"])
    assert not index.search("内容")["items"]
    with pytest.raises(ValueError):
        index.read(hit["id"], hit["sha256"])
    assert all(e["path"] == "已移出授权范围" for e in index.changes()["items"])


def test_large_shared_catalog_is_not_silently_truncated(local, monkeypatch):
    import akb.local_index as module

    index, root = local
    for n in range(3):
        (root / f"{n}.txt").write_text("教学资料")
    conf = index.configure(enabled=True, roots=[str(root)], share_catalog=True)
    scan(index, conf)
    monkeypatch.setattr(module, "MAX_DOCUMENTS", 2)

    def api(*_args):
        pytest.fail("Oversized directory must not be presented as complete")

    assert index.publish_catalog(api)["status"] == "pending"
