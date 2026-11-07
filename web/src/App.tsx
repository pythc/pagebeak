import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, setCsrf } from "./api";
import CatalogView from "./CatalogView";
import StorageView, { bytes } from "./StorageView";
import StartView, { type OnboardingState } from "./StartView";

type View =
  | "start"
  | "catalog"
  | "sources"
  | "profile"
  | "pages"
  | "search"
  | "forms"
  | "agent"
  | "storage"
  | "admin";
type Source = {
  id: string;
  title: string;
  filename: string;
  size: number;
  category: string;
  status: string;
  coverage: string;
  subject_scope: string;
  error?: string;
  created_at: string;
  summary?: string;
};
type Claim = {
  id: string;
  field: string;
  value: string;
  quote: string;
  revision: number;
  evidence_id?: string;
  basis: string;
  kind?: string;
};
type Profile = {
  revision: number;
  introduction: string;
  fields: Record<
    string,
    { label: string; value: string | null; status: string; claims: Claim[] }
  >;
  candidates: Claim[];
};
const fieldLabels: Record<string, string> = {
  name: "姓名",
  organization: "工作单位",
  job_title: "职称",
  email: "邮箱",
  phone: "联系电话",
  research_interests: "研究方向",
  administrative_position: "行政职务",
  teaching_hours: "指定年份教学学时合计",
};
const statusLabels: Record<string, string> = {
  queued: "等待处理",
  running: "处理中",
  ready: "已整理",
  partial: "部分解析",
  unsupported: "待接入解析",
  password_required: "需要密码",
  failed: "处理失败",
  completed: "已完成",
};
const views: { key: View; label: string; icon: string }[] = [
  { key: "start", label: "开始使用", icon: "arrow" },
  { key: "catalog", label: "资料目录", icon: "book" },
  { key: "sources", label: "我的文件", icon: "folder" },
  { key: "profile", label: "关于我", icon: "person" },
  { key: "pages", label: "专题知识", icon: "book" },
  { key: "search", label: "查找资料", icon: "search" },
  { key: "forms", label: "填写表单", icon: "form" },
  { key: "agent", label: "连接 WorkBuddy", icon: "link" },
  { key: "storage", label: "空间管理", icon: "folder" },
  { key: "admin", label: "管理后台", icon: "shield" },
];
const paths: Record<string, string> = {
  folder: "M3 7h6l2 2h10v11H3z M3 7V4h6l2 3",
  person: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0 M4 21v-2a8 8 0 0 1 16 0v2",
  book: "M12 5c-3-2-7-2-10 0v15c3-2 7-2 10 0 3-2 7-2 10 0V5c-3-2-7-2-10 0v15",
  search: "M20 20l-5-5 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
  form: "M7 3h10v3H7z M7 5H4v17h16V5h-3 M8 11h8 M8 15h8 M8 19h5",
  link: "M9 15l6-6 M7 10l-2 2a4 4 0 0 0 6 6l2-2 M11 8l2-2a4 4 0 0 1 6 6l-2 2",
  plus: "M12 5v14 M5 12h14",
  upload: "M12 16V3 M7 8l5-5 5 5 M3 16v5h18v-5",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  file: "M5 2h9l5 5v15H5z M14 2v6h5 M8 13h8 M8 17h5",
  check: "M5 12l4 4L19 6",
  close: "M6 6l12 12 M6 18L18 6",
  logout: "M9 3H3v18h6 M10 12h11 M16 7l5 5-5 5",
  shield: "M12 2l9 4v6c0 6-9 10-9 10S3 18 3 12V6z M8 12l3 3 5-6",
};
function Icon({ name, size = 20 }: { name: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.file} />
    </svg>
  );
}
function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <i />
        <i />
        <i />
      </span>
      <span>
        知序<small>个人知识库</small>
      </span>
    </div>
  );
}
function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`}>{statusLabels[status] || status}</span>
  );
}
function Empty({
  title,
  text,
  action,
}: {
  title: string;
  text: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name="book" size={34} />
      </span>
      <h3>{title}</h3>
      <p>{text}</p>
      {action}
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<any>(null),
    [boot, setBoot] = useState(true);
  const [view, setView] = useState<View>(window.location.hash === "#start" ? "start" : "catalog"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [sources, setSources] = useState<Source[]>([]),
    [profile, setProfile] = useState<Profile | null>(null),
    [pages, setPages] = useState<any[]>([]);
  const [selected, setSelected] = useState<Source | null>(null),
    [evidence, setEvidence] = useState<any[]>([]),
    [evidenceOffset, setEvidenceOffset] = useState(0);
  const [uploading, setUploading] = useState(false),
    [uploadOpen, setUploadOpen] = useState(false),
    [selfScope, setSelfScope] = useState(false);
  const [uploadFolder, setUploadFolder] = useState<string | null>(null);
  const [query, setQuery] = useState(""),
    [results, setResults] = useState<any[] | null>(null),
    [searching, setSearching] = useState(false);
  const [formFields, setFormFields] = useState([
      "name",
      "organization",
      "job_title",
    ]),
    [sensitive, setSensitive] = useState(false),
    [draft, setDraft] = useState<any>(null);
  const [tokens, setTokens] = useState<any[]>([]),
    [newToken, setNewToken] = useState(""),
    [config, setConfig] = useState<any>(null);
  const [tokenScopes, setTokenScopes] = useState([
    "knowledge:read",
    "profile:read",
    "knowledge:write",
    "forms:write",
  ]);
  const [statisticsYear, setStatisticsYear] = useState(String(new Date().getFullYear()));
  const [busy, setBusy] = useState(false);
  const [uploadProgress, setUploadProgress] = useState("");
  const [pendingUploads, setPendingUploads] = useState<string[]>([]);
  const [storage, setStorage] = useState<any>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const sourceRequest = useRef(0);
  const closeSource = () => {
    sourceRequest.current++;
    setSelected(null);
  };
  const fail = (e: unknown) =>
    setError(e instanceof Error ? e.message : "操作未完成，请重试");
  const load = async () => {
    const [s, p, pg, t, st] = await Promise.all([
      api("/sources"),
      api("/profile"),
      api("/pages"),
      api("/tokens"),
      api("/storage"),
    ]);
    setSources(s.items);
    setProfile(p);
    setPages(pg.items);
    setTokens(t);
    setStorage(st);
    setDraft((old: any) =>
      old && old.snapshot_revision !== p.revision
        ? {
            ...old,
            stale: true,
            answers: [],
            message: "资料已变化，请重新生成草稿",
          }
        : old,
    );
  };
  useEffect(() => {
    api("/auth/me")
      .then((u) => {
        setUser(u);
        setCsrf(u.csrf);
      })
      .catch(() => {})
      .finally(() => setBoot(false));
  }, []);
  useEffect(() => {
    if (user) {
      load().catch(fail);
      api("/integrations/workbuddy/config").then(setConfig).catch(fail);
      api<OnboardingState>("/onboarding").then((state) => {
        if (!state.dismissed && !state.completed_at && state.source_count === 0) setView("start");
      }).catch(fail);
    }
  }, [user]);
  useEffect(() => {
    if (
      !user ||
      !sources.some((s) => ["queued", "running", "revoked"].includes(s.status))
    )
      return;
    const timer = setInterval(() => load().catch(fail), 1800);
    return () => clearInterval(timer);
  }, [user, sources]);
  useEffect(() => {
    if (!user || !["profile", "forms", "pages"].includes(view)) return;
    void load().catch(fail);
    const timer = setInterval(() => load().catch(fail), 5000);
    return () => clearInterval(timer);
  }, [user, view]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 5000);
    return () => clearTimeout(timer);
  }, [notice]);
  const logout = async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) {
        fail(e);
        return;
      }
    }
    setCsrf("");
    setUser(null);
    setSources([]);
    setProfile(null);
    setPages([]);
    setResults(null);
    setDraft(null);
    setNewToken("");
    setTokens([]);
    setSelected(null);
    setConfig(null);
    setView("sources");
    window.location.reload();
  };
  const uploadFiles = async (files: FileList | File[]) => {
    setUploading(true);
    setError("");
    try {
      for (const file of Array.from(files)) {
        if (!file.size || file.size > 2 * 1024 ** 3)
          throw new Error("单文件需大于 0，且不超过 2 GiB");
        const upload = await api("/uploads", {
          method: "POST",
          body: JSON.stringify({
            filename: file.name,
            folder_id: uploadFolder,
            media_type: file.type || "application/octet-stream",
            size: file.size,
            subject_scope: selfScope ? "self" : "reference",
          }),
        });
        try {
          let offset = 0;
          while (offset < file.size) {
            const end = Math.min(file.size, offset + 8 * 1024 ** 2);
            const chunk = file.slice(offset, end);
            setUploadProgress(
              `${file.name} · ${Math.floor((offset / file.size) * 100)}%`,
            );
            try {
              const state = await api(
                `/uploads/${upload.id}/content?offset=${offset}`,
                { method: "PUT", body: chunk },
              );
              offset = state.offset;
            } catch (error) {
              const state = await api(`/uploads/${upload.id}`);
              if (state.offset === end) offset = end;
              else throw error;
            }
          }
          setUploadProgress(`${file.name} · 正在校验并入库`);
          await api(`/uploads/${upload.id}/complete`, { method: "POST" });
        } catch (error) {
          if (error instanceof ApiError && error.status === 503) {
            setPendingUploads((ids) => [...new Set([...ids, upload.id])]);
          } else {
            await api(`/uploads/${upload.id}`, { method: "DELETE" }).catch(() => {});
          }
          throw error;
        }
      }
      setUploadOpen(false);
      setNotice("资料已保存，正在后台整理");
      await load();
    } catch (e) {
      fail(e);
    } finally {
      setUploading(false);
      setUploadProgress("");
      await load().catch(fail);
      if (fileInput.current) fileInput.current.value = "";
    }
  };
  const openSource = async (source: Source) => {
    const request = ++sourceRequest.current;
    setSelected(source);
    setEvidence([]);
    setEvidenceOffset(0);
    try {
      const data = await api(`/sources/${source.id}/evidence`);
      if (request === sourceRequest.current) setEvidence(data.items);
    } catch (e) {
      fail(e);
    }
  };
  const openEvidence = async (id: string) => {
    const request = ++sourceRequest.current;
    try {
      const e = await api(`/evidence/${id}`);
      const source = await api(`/sources/${e.source_id}`);
      if (request !== sourceRequest.current) return;
      setSelected(source);
      setEvidence([e]);
      setEvidenceOffset(-1);
    } catch (e) {
      fail(e);
    }
  };
  const decide = async (claim: Claim, action: string) => {
    const value =
      action === "correct"
        ? window.prompt(
            "请输入修正后的个人信息。该修正会保存为你的独立声明。",
            claim.value,
          )
        : undefined;
    if (action === "correct" && value === null) return;
    setBusy(true);
    try {
      await api(`${claim.kind ? "/knowledge/facts" : "/claims"}/${claim.id}/decisions`, {
        method: "POST",
        body: JSON.stringify({
          action,
          expected_revision: claim.revision,
          ...(value !== undefined ? { value } : {}),
        }),
      });
      await load();
      setNotice("个人资料已更新");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };
  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    try {
      const data = await api("/knowledge/search", {
        method: "POST",
        body: JSON.stringify({ query, limit: 15 }),
      });
      setResults(data.items);
    } catch (e) {
      fail(e);
    } finally {
      setSearching(false);
    }
  };
  const createDraft = async () => {
    setBusy(true);
    try {
      setDraft(
        await api("/forms/drafts", {
          method: "POST",
          body: JSON.stringify({
            title: "个人信息表",
            fields: formFields.map((key) => ({
              key,
              label: fieldLabels[key],
              fact_key: key,
              allow_sensitive: sensitive,
              max_length: 500,
              ...(key === "teaching_hours" ? { knowledge: { kind: "teaching", field: "hours", operation: "sum", filters: { year: statisticsYear } } } : {}),
            })),
          }),
        }),
      );
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setNotice("已复制");
    } catch {
      setError("浏览器未允许剪贴板访问，请手动选择并复制");
    }
  };
  const deleteSource = async (source: Source) => {
    if (
      !window.confirm(
        `将“${source.title}”移入回收站？检索和填表引用会暂停，恢复后可继续使用。`,
      )
    )
      return;
    setBusy(true);
    try {
      await api("/files/actions", { method: "POST", body: JSON.stringify({ ids: [source.id], action: "trash" }) });
      setSelected(null);
      await load();
      setResults(null);
      setNotice("已移入回收站，可在“我的文件”中恢复");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };
  if (boot)
    return (
      <div className="boot">
        <Brand />
        <p>正在打开你的资料库…</p>
      </div>
    );
  if (!user)
    return (
      <Auth
        onSuccess={(u) => {
          setUser(u);
          setCsrf(u.csrf);
          setError("");
        }}
      />
    );
  const workbuddyConfig = config
    ? JSON.stringify(
        newToken
          ? {
              mcpServers: {
                "personal-knowledge": {
                  ...config.mcpServers["personal-knowledge"],
                  env: {
                    ...config.mcpServers["personal-knowledge"].env,
                    AKB_TOKEN: newToken,
                  },
                },
              },
            }
          : config,
        null,
        2,
      )
    : "";
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Brand />
        <div className="workspace-chip">
          <span className="avatar">{user.display_name.slice(0, 1)}</span>
          <div>
            {user.display_name}
            <small>个人知识空间</small>
          </div>
          <Icon name="shield" size={17} />
        </div>
        <div className="nav-caption">你的知识工作台</div>
        <nav>
          {views
            .filter((v) => v.key !== "admin" || user.is_admin)
            .map((v) => (
              <button
                key={v.key}
                className={view === v.key ? "active" : ""}
                onClick={async () => {
                  if (v.key === "sources") {
                    const target = new URL(window.location.href);
                    target.port = "5878";
                    target.pathname = "/home";
                    target.search = "";
                    try {
                      const link = await api("/native/link-code", { method: "POST" });
                      target.hash = "kb_link=" + encodeURIComponent(link.code);
                      window.location.assign(target.href);
                    } catch (e) { fail(e); }
                    return;
                  }
                  setView(v.key);
                  setError("");
                }}
              >
                <Icon name={v.icon} />
                <span>{v.label}</span>
                {v.key === "profile" && !!profile?.candidates.length && (
                  <b>{profile.candidates.length}</b>
                )}
              </button>
            ))}
        </nav>
        <div className="sidebar-bottom">
          {storage && (
            <button className="text-button" onClick={() => setView("storage")}>
              已用 {bytes(storage.used_bytes)} / {bytes(storage.quota_bytes)}
            </button>
          )}
          <span className="private-dot" />
          每个人，独立的资料空间
          <button className="text-button" onClick={logout}>
            <Icon name="logout" size={17} />
            退出登录
          </button>
        </div>
      </aside>
      <main>
        <div className="topbar">
          <span>
            个人空间 <i>/</i> {views.find((v) => v.key === view)?.label}
          </span>
          <div className="topbar-actions">
            <span className="version">本地开发版 · 0.1</span>
            <button
              className="text-button"
              onClick={() =>
                load()
                  .then(() => setNotice("已刷新资料"))
                  .catch(fail)
              }
            >
              刷新
            </button>
            <button
              className="mobile-logout text-button"
              aria-label="退出登录"
              onClick={logout}
            >
              <Icon name="logout" size={16} />
            </button>
          </div>
        </div>
        <div className="main-content">
          {error && (
            <div className="alert" role="alert">
              {error}
              <button onClick={() => setError("")} aria-label="关闭错误">
                <Icon name="close" size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="toast" role="status">
              <Icon name="check" size={18} />
              {notice}
            </div>
          )}
          {storage?.near_full && view !== "storage" && (
            <div className="source-warning" role="status">
              空间接近满，剩余 {bytes(storage.available_bytes)}。
              <button
                className="text-button"
                onClick={() => setView("storage")}
              >
                查看与清理空间
              </button>
            </div>
          )}
          {view === "storage" && (
            <StorageView onChange={() => load().catch(fail)} />
          )}
          {view === "start" && <StartView
            onCatalog={() => setView("catalog")}
            onUpload={() => setUploadOpen(true)}
            onAdvanced={() => setView("agent")}
          />}
          {view === "admin" && user.is_admin && <StorageView admin />}
          {view === "catalog" && (
            <CatalogView
              onSource={(id) => {
                api<Source>("/sources/" + id)
                  .then(openSource)
                  .catch(fail);
              }}
              onEvidence={openEvidence}
              onProfile={() => {
                load().catch(fail);
                setView("profile");
              }}
              onStorage={() => setView("storage")}
            />
          )}
          {view === "profile" && (
            <>
              <PageHeading
                title="关于我"
                text="从资料中提取个人信息，每一项都保留依据。"
              />
              <div className="profile-grid">
                {profile &&
                  Object.entries(profile.fields).map(([key, field]) => (
                    <section className="fact-card" key={key}>
                      <span>{field.label}</span>
                      <strong>
                        {field.value ||
                          (field.status === "conflict"
                            ? "存在不同信息，待确认"
                            : "尚未补充")}
                      </strong>
                      <div className="fact-footer">
                        {field.claims.length ? (
                          <>
                            <small>
                              {field.claims[0].basis === "user_asserted"
                                ? "本人声明"
                                : `${field.claims.length} 条资料依据`}
                            </small>
                            <button
                              className="text-button"
                              onClick={() => decide(field.claims[0], "correct")}
                            >
                              修正
                            </button>
                          </>
                        ) : (
                          <small>上传描述本人的资料后补充</small>
                        )}
                      </div>
                      {field.claims[0]?.evidence_id && (
                        <button
                          className="evidence-link"
                          onClick={() =>
                            openEvidence(field.claims[0].evidence_id!)
                          }
                        >
                          查看原文依据
                          <Icon name="arrow" size={14} />
                        </button>
                      )}
                    </section>
                  ))}
              </div>
              {!!profile?.introduction && (
                <section className="panel intro">
                  <div className="panel-top">
                    <h2>个人资料概览</h2>
                    <button
                      className="text-button"
                      onClick={() => copy(profile.introduction)}
                    >
                      复制
                    </button>
                  </div>
                  <p>{profile.introduction}</p>
                  <small>根据当前可用事实整理</small>
                </section>
              )}
              <section className="panel">
                <div className="panel-top">
                  <h2>
                    待确认信息<span>{profile?.candidates.length || 0}</span>
                  </h2>
                </div>
                {profile?.candidates.length ? (
                  profile.candidates.map((c) => (
                    <div className="candidate" key={c.id}>
                      <div>
                        <span className="eyebrow">
                          {fieldLabels[c.field] || c.field}
                        </span>
                        <h3>{c.value}</h3>
                        <p>{c.quote}</p>
                        <button
                          className="evidence-link"
                          onClick={() =>
                            c.evidence_id && openEvidence(c.evidence_id)
                          }
                        >
                          核对原文
                        </button>
                      </div>
                      <div className="candidate-actions">
                        <button
                          className="secondary"
                          disabled={busy}
                          onClick={() => decide(c, "accept")}
                        >
                          确认采纳
                        </button>
                        <button
                          className="text-button"
                          disabled={busy}
                          onClick={() => decide(c, "reject")}
                        >
                          忽略
                        </button>
                      </div>
                    </div>
                  ))
                ) : (
                  <div className="small-empty">暂无需要确认的信息</div>
                )}
              </section>
            </>
          )}
          {view === "pages" && (
            <>
              <PageHeading
                title="专题知识"
                text="将相关资料连成专题，保留每一段内容的原始依据。"
              />
              {pages.length ? (
                <div className="page-grid">
                  {pages.map((p) => (
                    <section className="panel knowledge-page" key={p.id}>
                      <span className="eyebrow">AGENT 整理</span>
                      <h2>{p.title}</h2>
                      {p.stale && <p className="source-warning">{p.message}</p>}
                      <p>{p.content}</p>
                      <div className="page-sources">
                        {p.evidence_ids.map((id: string, i: number) => (
                          <button
                            className="evidence-link"
                            key={id}
                            onClick={() => openEvidence(id)}
                          >
                            来源 {i + 1}
                          </button>
                        ))}
                      </div>
                    </section>
                  ))}
                </div>
              ) : (
                <section className="panel">
                  <Empty
                    title="让资料之间建立联系"
                    text="在 WorkBuddy 中说“根据我的项目资料整理一份专题”，Agent 会读取原文并把整理结果保存到这里。"
                    action={
                      <button
                        className="secondary"
                        onClick={() => setView("agent")}
                      >
                        连接 WorkBuddy
                        <Icon name="arrow" size={17} />
                      </button>
                    }
                  />
                </section>
              )}
            </>
          )}
          {view === "search" && (
            <>
              <PageHeading
                title="查找资料"
                text="从自己的资料中找到相关原文，随时核对出处。"
              />
              <form className="knowledge-search" onSubmit={search}>
                <Icon name="search" size={23} />
                <input
                  aria-label="搜索知识库"
                  placeholder="输入姓名、项目、论文标题或关键词…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
                <button
                  className="primary"
                  disabled={searching || !query.trim()}
                >
                  {searching ? "查找中…" : "查找资料"}
                </button>
              </form>
              <p className="search-caption">
                当前支持关键词检索。需要跨资料总结时，可以让 WorkBuddy
                读取并整理这些证据。
              </p>
              {results === null ? (
                <Empty
                  title="需要的资料，从这里找到"
                  text="所有结果来自你已处理的资料，并带有原文位置。"
                />
              ) : results.length ? (
                <div className="results">
                  {results.map((r) => (
                    <section className="panel result" key={r.id}>
                      <button
                        className="text-button"
                        onClick={() => openEvidence(r.id)}
                      >
                        <Icon name="file" size={17} />
                        {r.source_title}
                        <Icon name="arrow" size={16} />
                      </button>
                      <p>{r.text}</p>
                      <small>{locator(r.locator)}</small>
                    </section>
                  ))}
                </div>
              ) : (
                <Empty
                  title="暂时没有匹配的资料"
                  text="试试文件中的准确用词，或检查资料是否已解析。"
                />
              )}
            </>
          )}
          {view === "forms" && (
            <>
              <PageHeading
                title="让资料帮你填表"
                text="选择需要的字段，从已采纳的个人信息准备填写草稿。"
              />
              <div className="form-layout">
                <section className="panel form-options">
                  <h2>个人信息表</h2>
                  <p className="muted">需要填写哪些内容？</p>
                  {Object.entries(fieldLabels).map(([key, label]) => (
                    <label className="check-row" key={key}>
                      <input
                        type="checkbox"
                        checked={formFields.includes(key)}
                        onChange={(e) =>
                          setFormFields((old) =>
                            e.target.checked
                              ? [...old, key]
                              : old.filter((k) => k !== key),
                          )
                        }
                      />
                      {label}
                    </label>
                  ))}
                  <label className="check-row sensitive">
                    <input
                      type="checkbox"
                      checked={sensitive}
                      onChange={(e) => setSensitive(e.target.checked)}
                    />
                    允许在草稿中使用联系方式
                  </label>
                  {formFields.includes("teaching_hours") && <label>统计年份<input type="number" min="1900" max="2200" value={statisticsYear} onChange={e => setStatisticsYear(e.target.value)} /></label>}
                  <button
                    className="primary"
                    disabled={busy || !formFields.length}
                    onClick={createDraft}
                  >
                    {busy ? "准备中…" : "生成填写草稿"}
                    <Icon name="arrow" size={17} />
                  </button>
                </section>
                <section className="panel draft-panel">
                  <h2>填写预览</h2>
                  {draft ? (
                    <>
                      {draft.stale ? (
                        <div className="alert">
                          资料已变化，请重新生成草稿。
                        </div>
                      ) : (
                        draft.answers.map((a: any) => (
                          <div className="field-answer" key={a.key}>
                            <label>
                              {a.label}
                              <span className={`answer-status ${a.status}`}>
                                {{
                                  ready: "可填写",
                                  missing: "缺少资料",
                                  conflict: "需要确认",
                                  blocked: "暂不可用",
                                }[a.status as string] || a.status}
                              </span>
                            </label>
                            <div
                              className={
                                a.value ? "answer-value" : "answer-value absent"
                              }
                            >
                              {a.value || a.reason}
                            </div>
                            {a.evidence_ids.map((id: string, i: number) => (
                              <button
                                className="evidence-link"
                                key={id}
                                onClick={() => openEvidence(id)}
                              >
                                依据 {i + 1}
                              </button>
                            ))}
                            {a.calculation?.operation !== "value" && a.calculation && <p className="muted">{a.calculation.scope} · {a.calculation.record_count} 条事项</p>}
                            {!!a.pending_tasks && <p className="source-warning">还有 {a.pending_tasks} 份资料等待语义整理，结果可能不完整。</p>}
                          </div>
                        ))
                      )}
                      <p className="muted">{draft.message}</p>
                      {!draft.stale && <label className="word-template">填写 Word 模板
                        <input type="file" accept=".docx" disabled={busy} onChange={async e => {
                          const file = e.target.files?.[0]; if (!file) return;
                          setBusy(true);
                          try {
                            const body = new FormData(); body.append("file", file);
                            const blob = await api<Blob>(`/forms/drafts/${draft.id}/document`, { method: "POST", body });
                            const url = URL.createObjectURL(blob); const link = document.createElement("a");
                            link.href = url; link.download = "已填写_" + file.name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
                          } catch (err) { fail(err); } finally { setBusy(false); e.target.value = ""; }
                        }} />
                        <small>模板用 {"{{field:字段key}}"} 标记填写位置，例如 {"{{field:job_title}}"}。其他正文保留，未知项留空。</small>
                      </label>}
                    </>
                  ) : (
                    <Empty
                      title="先准备，再核对"
                      text="有依据的字段会自动带入。缺失、冲突或未授权使用的内容会明确标注。"
                    />
                  )}
                </section>
              </div>
            </>
          )}
          {view === "agent" && (
            <>
              <PageHeading
                title="连接 WorkBuddy"
                text="把你的个人知识库交给 Agent 使用，权限始终由你掌握。"
              />
              <div className="agent-intro">
                <span className="agent-monogram">
                  W<span>↗</span>
                </span>
                <div>
                  <span className="eyebrow">连接你的 Agent</span>
                  <h2>让 WorkBuddy 了解你的资料</h2>
                  <p>检索原文 · 整理专题 · 提出事实候选 · 准备表单</p>
                </div>
              </div>
              <div className="agent-columns">
                <section className="panel">
                  <h2>
                    <span className="step">1</span>生成个人访问令牌
                  </h2>
                  <p className="muted">
                    只允许访问当前账号的资料。有效期 30 天，可随时撤销。
                  </p>
                  {[
                    ["knowledge:read", "读取资料和证据"],
                    ["profile:read", "读取个人档案"],
                    ["knowledge:write", "保存资料、专题和事实候选"],
                    ["forms:write", "准备表单草稿"],
                  ].map(([scope, label]) => (
                    <label className="check-row" key={scope}>
                      <input
                        type="checkbox"
                        checked={tokenScopes.includes(scope)}
                        onChange={(e) =>
                          setTokenScopes((old) =>
                            e.target.checked
                              ? [...old, scope]
                              : old.filter((s) => s !== scope),
                          )
                        }
                      />
                      {label}
                    </label>
                  ))}
                  <button
                    className="primary"
                    disabled={!tokenScopes.length || busy}
                    onClick={async () => {
                      setBusy(true);
                      try {
                        const t = await api("/tokens", {
                          method: "POST",
                          body: JSON.stringify({
                            label: "WorkBuddy",
                            scopes: tokenScopes,
                          }),
                        });
                        setNewToken(t.token);
                        await load();
                      } catch (e) {
                        fail(e);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  >
                    生成 WorkBuddy 令牌
                  </button>
                  {newToken && (
                    <p className="success-text">
                      新令牌已加入下方配置，仅在本次页面中显示。请妥善保存。
                    </p>
                  )}
                </section>
                <section className="panel">
                  <h2>
                    <span className="step">2</span>在 WorkBuddy 添加 MCP
                  </h2>
                  <p className="muted">
                    将 personal-knowledge 合并到 WorkBuddy 的 MCP
                    配置中，保留已有服务。 知识库 API
                    和后台处理服务需要保持运行。
                  </p>
                  <pre className="config-code">{workbuddyConfig}</pre>
                  <button
                    className="secondary"
                    onClick={() => copy(workbuddyConfig)}
                    disabled={!config}
                  >
                    {newToken ? "复制包含新令牌的配置" : "复制配置模板"}
                  </button>
                </section>
              </div>
              <section className="panel prompts">
                <h2>连接后，可以这样说</h2>
                {[
                  "在我的知识库中找到与项目申报相关的资料，并列出原文依据。",
                  "读取我的个人资料，整理研究方向专题并保存到知识库。",
                  "根据已确认的信息准备一份个人信息表，标注缺失和冲突字段。",
                ].map((text) => (
                  <button key={text} onClick={() => copy(text)}>
                    “{text}”<Icon name="arrow" size={17} />
                  </button>
                ))}
              </section>
              <section className="panel">
                <div className="panel-top">
                  <h2>已创建的访问令牌</h2>
                </div>
                {tokens.length ? (
                  tokens.map((t) => (
                    <div className="token-row" key={t.id}>
                      <div>
                        <strong>{t.label}</strong>
                        <small>
                          {t.revoked
                            ? "已撤销"
                            : `到期 ${new Date(t.expires_at).toLocaleDateString("zh-CN")}`}
                        </small>
                      </div>
                      <button
                        className="text-button danger-text"
                        disabled={t.revoked}
                        onClick={async () => {
                          try {
                            await api(`/tokens/${t.id}`, { method: "DELETE" });
                            setNewToken("");
                            await load();
                          } catch (e) {
                            fail(e);
                          }
                        }}
                      >
                        撤销
                      </button>
                    </div>
                  ))
                ) : (
                  <div className="small-empty">尚未创建访问令牌</div>
                )}
              </section>
            </>
          )}
          <footer>
            知序 · 让资料成为可以使用的知识
            <span>原始资料与个人事实，始终保留依据。</span>
          </footer>
        </div>
      </main>
      {uploadOpen && (
        <div
          className="modal-backdrop"
          onClick={() => !uploading && setUploadOpen(false)}
        >
          <section
            className="modal upload-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="upload-title"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close"
              aria-label="关闭上传"
              onClick={() => setUploadOpen(false)}
              disabled={uploading}
            >
              <Icon name="close" />
            </button>
            <span className="eyebrow">添加到个人空间</span>
            <h2 id="upload-title">添加资料</h2>
            <p>原件会保留在你的独立资料库中。</p>
            {error && <div className="alert" role="alert">{error}</div>}
            {pendingUploads.length > 0 && <div className="source-warning">
              <p>已上传的内容暂存 24 小时，存储恢复后可重试保存。</p>
              <button className="secondary" disabled={uploading} onClick={async () => {
                setUploading(true); setError("");
                try {
                  for (const id of pendingUploads) {
                    const state = await api(`/uploads/${id}`);
                    if (state.offset !== state.size) throw new Error("文件未传完，请取消暂存后重新上传");
                    await api(`/uploads/${id}/complete`, { method: "POST" });
                    setPendingUploads((ids) => ids.filter((value) => value !== id));
                  }
                  await load(); setUploadOpen(false); setNotice("资料已保存，正在后台整理");
                } catch (e) { fail(e); } finally { setUploading(false); }
              }}>重试保存</button>
              <button className="text-button" disabled={uploading} onClick={async () => {
                setUploading(true);
                try {
                  for (const id of pendingUploads) await api(`/uploads/${id}`, { method: "DELETE" });
                  setPendingUploads([]); setError(""); await load();
                } catch (e) { fail(e); } finally { setUploading(false); }
              }}>取消暂存上传</button>
            </div>}
            <label className="scope-box">
              <input
                type="checkbox"
                checked={selfScope}
                onChange={(e) => setSelfScope(e.target.checked)}
              />
              <span>
                <strong>这些资料明确描述我本人</strong>
                <small>用于提取个人信息。收藏论文和他人资料请保持未选。</small>
              </span>
            </label>
            <button
              className="dropzone"
              disabled={uploading}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                if (!uploading) uploadFiles(e.dataTransfer.files);
              }}
              onClick={() => fileInput.current?.click()}
            >
              <span>
                <Icon name="upload" size={32} />
              </span>
              <strong>
                {uploading ? "正在保存资料…" : "拖入文件，或点击选择"}
              </strong>
              <small>PDF、Word、Excel、PowerPoint、文本等</small>
              <small>单文件最多 2 GiB；图片和扫描 PDF 自动尝试本地 OCR</small>
              {uploadProgress && <p role="status">{uploadProgress}</p>}
            </button>
            <input
              ref={fileInput}
              type="file"
              hidden
              multiple
              onChange={(e) => e.target.files && uploadFiles(e.target.files)}
            />
          </section>
        </div>
      )}
      {selected && (
        <div className="modal-backdrop" onClick={closeSource}>
          <section
            className="modal evidence-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="source-title"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close"
              aria-label="关闭原文"
              onClick={closeSource}
            >
              <Icon name="close" />
            </button>
            <span className="eyebrow">原文与依据</span>
            <h2 id="source-title">{selected.title}</h2>
            <div className="source-meta">
              <Badge status={selected.status} />
              <span>
                {selected.subject_scope === "self" ? "描述本人" : "参考资料"}
              </span>
              <a href={`/v1/sources/${selected.id}/original`}>下载原件</a>
            </div>
            {selected.error && (
              <div className="source-warning">{selected.error}</div>
            )}
            {selected.summary && <section className="file-summary"><h3>资料摘要</h3><p>{selected.summary}</p></section>}
            <p className="muted">下方为解析后的内容预览。完整排版请下载原件查看。</p>
            <div className="evidence-body">
              {evidence.length ? (
                evidence.map((e) => (
                  <article className="evidence-block" key={e.id}>
                    <span>{locator(e.locator)}</span>
                    <p>{e.text}</p>
                  </article>
                ))
              ) : (
                <p className="muted">
                  暂无可读取原文。请等待处理完成，或查看上方解析说明。
                </p>
              )}
            </div>
            {evidenceOffset >= 0 && (
              <div className="pagination">
                <button
                  className="secondary"
                  disabled={!evidenceOffset}
                  onClick={async () => {
                    const offset = Math.max(0, evidenceOffset - 10);
                    try {
                      const data = await api(
                        `/sources/${selected.id}/evidence?offset=${offset}`,
                      );
                      setEvidence(data.items);
                      setEvidenceOffset(offset);
                    } catch (e) {
                      fail(e);
                    }
                  }}
                >
                  上一页
                </button>
                <span>第 {evidenceOffset + 1} 项起</span>
                <button
                  className="secondary"
                  disabled={evidence.length < 10}
                  onClick={async () => {
                    const offset = evidenceOffset + 10;
                    try {
                      const data = await api(
                        `/sources/${selected.id}/evidence?offset=${offset}`,
                      );
                      setEvidence(data.items);
                      setEvidenceOffset(offset);
                    } catch (e) {
                      fail(e);
                    }
                  }}
                >
                  下一页
                </button>
              </div>
            )}
            <div className="modal-actions">
              <button
                className="text-button danger-text"
                disabled={busy}
                onClick={() => deleteSource(selected)}
              >
                移入回收站
              </button>
              <button className="secondary" onClick={closeSource}>
                完成查看
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function PageHeading({ title, text }: { title: string; text: string }) {
  return (
    <div className="page-heading">
      <div>
        <span className="eyebrow">个人知识工作台</span>
        <h1>{title}</h1>
        <p>{text}</p>
      </div>
    </div>
  );
}
function locator(l: any) {
  if (l.page) return `PDF 第 ${l.page} 页`;
  if (l.sheet) return `${l.sheet} · ${l.range}`;
  if (l.slide) return `幻灯片 ${l.slide}`;
  if (l.paragraph) return `第 ${l.paragraph} 段`;
  if (l.table) return `表格 ${l.table} · 第 ${l.row} 行`;
  return `原文第 ${l.line || 1} 行`;
}
function Auth({ onSuccess }: { onSuccess: (user: any) => void }) {
  const [register, setRegister] = useState(false),
    [email, setEmail] = useState(""),
    [password, setPassword] = useState(""),
    [name, setName] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const data = await api(register ? "/auth/register" : "/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email,
          password,
          ...(register ? { display_name: name } : {}),
        }),
      });
      onSuccess(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录未完成");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="auth-page">
      <section className="auth-story">
        <Brand />
        <div>
          <span className="eyebrow">让知识有一个家</span>
          <h1>
            你的资料，
            <br />
            值得被好好使用。
          </h1>
          <p>
            从散落的文件，到有依据的个人知识。
            <br />
            查找、整理、填写，让信息真正帮上忙。
          </p>
          <div className="paper-scene">
            <div className="paper paper-back" />
            <div className="paper paper-middle" />
            <div className="paper paper-front">
              <Icon name="file" size={33} />
              <i />
              <i />
              <i />
              <span>
                <Icon name="check" size={16} />
                每一项，都有出处
              </span>
            </div>
            <span className="scene-dot" />
          </div>
        </div>
        <small>独立资料空间 · 原件可追溯 · WorkBuddy MCP</small>
      </section>
      <section className="auth-form">
        <div className="auth-box">
          <span className="eyebrow">你的知识空间</span>
          <h2>{register ? "建立你的知识空间" : "欢迎回来"}</h2>
          <p>
            {register
              ? "把第一份资料放进来，知识从这里开始积累。"
              : "登录后，继续整理和使用你的资料。"}
          </p>
          <form onSubmit={submit}>
            {register && (
              <label>
                空间名称
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  maxLength={80}
                  autoComplete="name"
                />
              </label>
            )}
            <label>
              邮箱
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
                placeholder="you@example.com"
              />
            </label>
            <label>
              密码
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={10}
                maxLength={256}
                autoComplete={register ? "new-password" : "current-password"}
                placeholder="至少 10 位"
              />
            </label>
            {error && (
              <div className="alert" role="alert">
                {error}
              </div>
            )}
            <button className="primary" type="submit" disabled={busy}>
              {busy ? "正在打开…" : register ? "创建个人空间" : "登录知识库"}
              <Icon name="arrow" size={18} />
            </button>
          </form>
          <button
            className="auth-switch"
            onClick={() => {
              setRegister(!register);
              setError("");
            }}
          >
            {register ? "已有账号？返回登录" : "第一次使用？创建个人空间"}
          </button>
          <div className="auth-note">
            <Icon name="shield" size={18} />
            每个账号拥有独立的资料与个人档案
          </div>
        </div>
      </section>
    </div>
  );
}
