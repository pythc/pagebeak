import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "./api";
import KnowledgeView from "./KnowledgeView";

type Entry = {
  id: string | null;
  source_id: string;
  title: string;
  category: string;
  summary: string;
  note: string;
  manual_fields: string[];
  current: boolean;
  status: string;
  coverage: string;
  subject_scope: string;
  error: string | null;
  generated: {
    excerpts?: { evidence_id: string; quote: string; locator: unknown }[];
  };
};
type Catalog = {
  id: string;
  revision: number;
  title: string;
  note: string;
  enabled: boolean;
  total: number;
  pending: number;
  offset: number;
  limit: number;
  has_more: boolean;
  items: Entry[];
  markdown: string;
  archives?: { id: string; title: string }[];
  sections: { name: string; count: number }[];
  organized_at: string | null;
  profile: { introduction: string; candidate_count: number } | null;
  job: { id: string; status: string; error: string | null } | null;
  last_result: {
    added?: string[];
    updated?: string[];
    removed?: string[];
    unchanged?: number;
    sources_read?: number;
  };
};
type Edit = {
  id?: string;
  revision: number;
  title: string;
  category: string;
  summary: string;
  note: string;
  original: Record<string, string>;
  conflict?: boolean;
};

const sourceStates: Record<string, string> = {
  queued: "等待解析",
  running: "正在解析",
  ready: "解析完成",
  partial: "部分解析",
  unsupported: "暂不支持解析",
  failed: "解析失败",
  password_required: "需要密码",
};

export default function CatalogView({
  onSource,
  onEvidence,
  onProfile,
  onStorage,
}: {
  onSource: (id: string) => void;
  onEvidence: (id: string) => void;
  onProfile: () => void;
  onStorage: () => void;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [pane, setPane] = useState("files");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(""),
    [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false),
    [edit, setEdit] = useState<Edit | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api<Catalog>("/catalog?offset=" + offset + "&limit=100")
        .then((data) => {
          if (active)
            setCatalog((old) =>
              old && old.revision > data.revision ? old : data,
            );
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    refresh();
    const timer = setInterval(refresh, 2500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [offset]);
  const refresh = async () => {
    const data = await api<Catalog>("/catalog?offset=" + offset + "&limit=100");
    setCatalog((old) => (old && old.revision > data.revision ? old : data));
    return data;
  };
  const mutate = async (path: string, body: unknown, success: string) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api(path, { method: "PATCH", body: JSON.stringify(body) });
      await refresh();
      setMessage(success);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存未完成");
      if (e instanceof ApiError && e.status === 409) {
        setEdit((old) => (old ? { ...old, conflict: true } : old));
        await refresh().catch(() => {});
      }
      return false;
    } finally {
      setBusy(false);
    }
  };
  const organize = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api("/catalog/organize", { method: "POST" });
      await refresh();
      setMessage("整理任务已加入后台，目录会自动更新。");
    } catch (e) {
      setError(e instanceof Error ? e.message : "启动未完成");
    } finally {
      setBusy(false);
    }
  };
  const beginEdit = (entry?: Entry) => {
    if (!catalog) return;
    const original = entry
      ? {
          title: entry.title,
          category: entry.category,
          summary: entry.summary,
          note: entry.note,
        }
      : { title: catalog.title, note: catalog.note, category: "", summary: "" };
    setEdit({
      id: entry?.id || undefined,
      revision: catalog.revision,
      ...original,
      original,
    });
    setError("");
    setMessage("");
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!edit) return;
    const keys = edit.id
      ? ["title", "category", "summary", "note"]
      : ["title", "note"];
    const changes = Object.fromEntries(
      keys
        .filter((key) => edit[key as keyof typeof edit] !== edit.original[key])
        .map((key) => [key, edit[key as keyof typeof edit]]),
    );
    if (!Object.keys(changes).length) {
      setEdit(null);
      return;
    }
    const saved = await mutate(
      edit.id ? "/catalog/entries/" + edit.id : "/catalog",
      { expected_revision: edit.revision, ...changes },
      "人工修改已保存，后续整理会保留。",
    );
    if (saved) setEdit(null);
  };
  if (!catalog)
    return (
      <section className="panel">
        <h1>资料目录</h1>
        <p>{error || "正在读取目录…"}</p>
      </section>
    );
  const running =
    catalog.job && ["queued", "running"].includes(catalog.job.status);
  const tabs = <div className="catalog-actions" role="tablist" aria-label="目录视图">
    {[["files", "资料目录"], ["knowledge", "知识目录"], ["pending", "待处理"]].map(([key, label]) =>
      <button role="tab" aria-selected={pane === key} className={pane === key ? "primary" : "secondary"} key={key} onClick={() => setPane(key)}>{label}</button>)}
  </div>;
  if (pane !== "files") return <div className="catalog"><div className="page-heading"><h1>{catalog.title}</h1></div>{tabs}<KnowledgeView pendingOnly={pane === "pending"} onEvidence={onEvidence} /></div>;
  return (
    <div className="catalog">
      <div className="page-heading">
        <div>
          <span className="eyebrow">你和 Agent 共用的目录</span>
          <h1>{catalog.title}</h1>
          <p>你和 Agent 共用的资料入口。分类、摘录和来源一起保留。</p>
        </div>
        <button
          className="primary"
          disabled={busy || !!running}
          onClick={organize}
        >
          {running ? "正在整理…" : "整理现有资料"}
        </button>
      </div>
      {tabs}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {message && (
        <p role="status" className="catalog-message">
          {message}
        </p>
      )}
      <section className="panel catalog-overview">
        <div className="catalog-toolbar">
          <span>
            {catalog.total} 份资料 · {catalog.sections.length} 个分类 · 修订{" "}
            {catalog.revision}
          </span>
          <button className="text-button" onClick={() => beginEdit()}>
            编辑目录标题与备注
          </button>
          <label className="check-row">
            <input
              type="checkbox"
              checked={catalog.enabled}
              disabled={busy}
              onChange={(e) =>
                mutate(
                  "/catalog",
                  {
                    expected_revision: catalog.revision,
                    enabled: e.target.checked,
                  },
                  e.target.checked
                    ? "已开启新增资料后的自动整理。"
                    : "已暂停后续自动整理；已排队的任务仍会完成。",
                )
              }
            />
            自动整理新增资料
          </label>
        </div>
        {catalog.note && (
          <p className="catalog-note">
            {catalog.note}
            <small>用户目录备注</small>
          </p>
        )}
        <p>
          {catalog.profile?.introduction ||
            "个人介绍会根据已采纳的个人信息显示。"}
          <button className="text-button" onClick={onProfile}>
            查看个人档案
            {!!catalog.profile?.candidate_count &&
              "（" + catalog.profile.candidate_count + " 项待确认）"}
          </button>
        </p>
        <p className="muted">
          {running
            ? "后台正在核对资料变化，完成后会自动刷新。"
            : catalog.job?.status === "failed"
              ? "上次整理失败，请重新整理或检查后台服务。"
              : catalog.organized_at
                ? "最近整理：" + new Date(catalog.organized_at).toLocaleString()
                : "等待首次整理。"}
          {!!catalog.pending && " " + catalog.pending + " 份资料等待目录更新。"}
        </p>
        {catalog.last_result.added && (
          <p className="muted">
            上次整理：新增 {catalog.last_result.added.length}， 更新{" "}
            {catalog.last_result.updated?.length || 0}，移除{" "}
            {catalog.last_result.removed?.length || 0}， 跳过未变资料{" "}
            {catalog.last_result.unchanged || 0}。
          </p>
        )}
        <p className="catalog-caption">
          自动内容使用规则分类和原文摘录；人工摘要与备注不作为独立事实证据。
          原文件保持不变。
        </p>
        <div className="catalog-sections">
          {catalog.sections.map((s, i) => (
            <a key={s.name} href={"#catalog-section-" + i}>
              {s.name}
              <b>{s.count}</b>
            </a>
          ))}
        </div>
      </section>
      {edit && (
        <form className="panel catalog-editor" onSubmit={save}>
          <h2>{edit.id ? "编辑目录条目" : "编辑目录"}</h2>
          <p className="muted">
            只保存改动的字段。自动整理会继续更新原文依据，并保留这些人工修改。
          </p>
          {(
            ["title", ...(edit.id ? ["category", "summary"] : []), "note"] as (
              | "title"
              | "category"
              | "summary"
              | "note"
            )[]
          ).map((key) => (
            <label key={key}>
              {
                {
                  title: "标题",
                  category: "分类",
                  summary: "人工摘要",
                  note: "备注",
                }[key]
              }
              {key === "summary" || key === "note" ? (
                <textarea
                  rows={key === "summary" ? 5 : 3}
                  maxLength={4000}
                  value={edit[key]}
                  onChange={(e) => setEdit({ ...edit, [key]: e.target.value })}
                />
              ) : (
                <input
                  required
                  maxLength={key === "category" ? 60 : 200}
                  value={edit[key]}
                  onChange={(e) => setEdit({ ...edit, [key]: e.target.value })}
                />
              )}
            </label>
          ))}
          {edit.conflict && (
            <div className="catalog-conflict">
              <p>
                目录在编辑期间发生了变化，草稿已保留。
                请核对下方最新目录，再选择继续。
              </p>
              <button
                type="button"
                className="secondary"
                onClick={() =>
                  setEdit({
                    ...edit,
                    revision: catalog.revision,
                    conflict: false,
                  })
                }
              >
                使用最新目录版本，保留编辑草稿
              </button>
            </div>
          )}
          <div className="catalog-actions">
            <button className="primary" disabled={busy || !!edit.conflict}>
              保存人工修改
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setEdit(null)}
            >
              取消
            </button>
          </div>
        </form>
      )}
      {!catalog.total && (
        <section className="panel">
          <h2>目录会随资料一起生长</h2>
          <p>
            上传资料后，后台会自动加入目录。也可以点击“整理现有资料”检查已有内容。
          </p>
        </section>
      )}
      {catalog.sections.map((section, index) => {
        const entries = catalog.items.filter(
          (i) => i.category === section.name,
        );
        return (
          <section
            className="catalog-group"
            id={"catalog-section-" + index}
            key={section.name}
          >
            <h2>
              {section.name}
              <span>{section.count}</span>
            </h2>
            {!entries.length && (
              <p className="muted">此分类的条目在其他分页。</p>
            )}
            {entries.map((entry) => (
              <article className="panel catalog-entry" key={entry.source_id}>
                <div className="catalog-entry-heading">
                  <button
                    className="text-button catalog-title"
                    onClick={() => onSource(entry.source_id)}
                  >
                    {entry.title}
                  </button>
                  <button
                    className="text-button"
                    disabled={!entry.id}
                    onClick={() => beginEdit(entry)}
                  >
                    编辑条目
                  </button>
                </div>
                <p className="catalog-caption">
                  {entry.subject_scope === "self"
                    ? "本人资料"
                    : "参考资料 · 不代表本人成果"}
                  {" · "}
                  {sourceStates[entry.status] || entry.status}
                  {" · "}
                  {entry.current ? "目录已同步" : "等待目录更新"}
                  {entry.coverage === "partial" && " · 部分解析"}
                  {entry.manual_fields.length > 0 && " · 含人工修改"}
                </p>
                {entry.error && <p className="source-warning">{entry.error}</p>}
                {entry.summary && (
                  <>
                    <small className="catalog-caption">
                      {entry.manual_fields.includes("summary")
                        ? "人工摘要"
                        : "原文摘录"}
                    </small>
                    <p className="catalog-summary">{entry.summary}</p>
                  </>
                )}
                {entry.note && (
                  <p className="catalog-note">
                    {entry.note}
                    <small>用户备注</small>
                  </p>
                )}
                <div className="catalog-actions">
                  {(entry.generated.excerpts || []).map((e, i) => (
                    <button
                      key={`${e.evidence_id}-${i}`}
                      className="evidence-link"
                      onClick={() => onEvidence(e.evidence_id)}
                    >
                      原文依据 {i + 1}
                    </button>
                  ))}
                  {entry.manual_fields.some((f) =>
                    ["title", "category", "summary"].includes(f),
                  ) && (
                    <button
                      className="text-button"
                      disabled={busy}
                      onClick={() =>
                        mutate(
                          "/catalog/entries/" + entry.id,
                          {
                            expected_revision: catalog.revision,
                            title: null,
                            category: null,
                            summary: null,
                          },
                          "已恢复自动标题、分类和摘录；备注保留。",
                        )
                      }
                    >
                      恢复自动内容
                    </button>
                  )}
                </div>
              </article>
            ))}
          </section>
        );
      })}
      {(offset > 0 || catalog.has_more) && (
        <div className="catalog-actions">
          <button
            className="secondary"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 100))}
          >
            上一页
          </button>
          <span>第 {Math.floor(offset / 100) + 1} 页</span>
          <button
            className="secondary"
            disabled={!catalog.has_more}
            onClick={() => setOffset(offset + 100)}
          >
            下一页
          </button>
        </div>
      )}
      {!!catalog.archives?.length && (
        <section className="panel storage-panel">
          <h2>摘要归档</h2>
          <p className="muted">
            经你批准删除原件后保留的有损摘要，不能作为原件证据。
          </p>
          {catalog.archives.map((a) => (
            <p key={a.id}>{a.title}</p>
          ))}
          <button className="secondary" onClick={onStorage}>
            查看与管理摘要归档
          </button>
        </section>
      )}
      <details className="panel catalog-markdown">
        <summary>查看 Agent 可读目录（当前页 Markdown）</summary>
        <p className="muted">
          与上方使用同一份目录和修订版本。Agent 可通过 MCP 分页读取，并按来源 ID
          获取原文。
        </p>
        <pre>{catalog.markdown}</pre>
      </details>
    </div>
  );
}
