import { useEffect, useState } from "react";
import { api } from "./api";

export function bytes(value: number) {
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${value.toLocaleString()} B`;
}

const sourceStatus: Record<string, string> = {
  ready: "解析完成",
  partial: "部分解析",
  queued: "等待处理",
  running: "处理中",
  failed: "处理失败",
  unsupported: "暂不支持解析",
  password_required: "需要密码",
};

export default function StorageView({
  admin = false,
  onChange,
}: {
  admin?: boolean;
  onChange?: () => void;
}) {
  const [usage, setUsage] = useState<any>(null),
    [files, setFiles] = useState<any[]>([]);
  const [plans, setPlans] = useState<any[]>([]),
    [archives, setArchives] = useState<any[]>([]);
  const [selected, setSelected] = useState<string[]>([]),
    [mode, setMode] = useState("summarize");
  const [users, setUsers] = useState<any[]>([]),
    [page, setPage] = useState(0),
    [more, setMore] = useState(false);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [ack, setAck] = useState<Record<string, boolean>>({});
  const [archivePage, setArchivePage] = useState(0),
    [archiveMore, setArchiveMore] = useState(false);
  const load = async () => {
    if (admin) {
      const data = await api(`/admin/storage/users?offset=${page * 100}`);
      setUsers(data.items);
      setMore(data.has_more);
    } else {
      const [u, c, p, a] = await Promise.all([
        api("/storage"),
        api("/storage/candidates"),
        api("/storage/plans"),
        api(`/storage/archives?offset=${archivePage * 10}`),
      ]);
      setUsage(u);
      setFiles(c.items);
      setPlans(p.items);
      setArchives(a.items);
      setArchiveMore(a.has_more);
    }
  };
  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, [admin, page, archivePage]);
  const act = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await operation();
      await load();
      onChange?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">个人存储空间</span>
          <h1>{admin ? "用户空间分配" : "空间管理"}</h1>
          <p>
            {admin
              ? "为每位用户设置独立容量。配额是逻辑上限，不代表服务器已经预留同等磁盘。"
              : "查看容量、审阅清理方案，以及管理删除原件后保留的摘要。"}
          </p>
        </div>
        <button className="secondary" disabled={busy} onClick={() => act(load)}>
          刷新空间
        </button>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {admin ? (
        <section className="panel storage-panel">
          {users.map((user) => (
            <AdminRow
              key={user.id}
              user={user}
              busy={busy}
              save={(value) =>
                act(() =>
                  api(`/admin/storage/users/${user.id}`, {
                    method: "PATCH",
                    body: JSON.stringify({
                      quota_bytes: value,
                      expected_quota_bytes: user.quota_bytes,
                    }),
                  }),
                )
              }
            />
          ))}
          <div className="pagination">
            <button
              disabled={!page || busy}
              onClick={() => setPage((p) => p - 1)}
            >
              上一页
            </button>
            <span>第 {page + 1} 页</span>
            <button
              disabled={!more || busy}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页
            </button>
          </div>
        </section>
      ) : (
        <>
          {usage && (
            <section className="panel storage-panel">
              <h2>我的容量</h2>
              <p>
                <strong>{bytes(usage.used_bytes)}</strong> 已用 /{" "}
                {bytes(usage.quota_bytes)} 总容量
              </p>
              <progress
                aria-label="空间使用率"
                max={1}
                value={Math.min(1, usage.usage_ratio)}
              />
              <p>
                剩余可上传 {bytes(usage.available_bytes)} · 上传预留{" "}
                {bytes(usage.reserved_bytes)} · 待清理{" "}
                {bytes(usage.pending_deletion_bytes)} · 单文件最高{" "}
                {bytes(usage.upload_limit_bytes)}
              </p>
              {usage.near_full && (
                <div className="source-warning" role="status">
                  空间接近满。可以审阅下方文件并清理，或联系管理员扩容。Agent
                  需要你的批准才能执行清理。
                </div>
              )}
              <p className="muted">
                按原件字节统计。上传预留整份文件容量；撤回的文件在后台删除原件后才释放。解析文本、索引和摘要由系统存储，暂不计入个人配额。
              </p>
            </section>
          )}
          <section className="panel storage-panel">
            <h2>选择要整理的文件</h2>
            <p className="muted">
              按大小列出最多 50
              份文件。请根据用途判断，文件大不代表没有价值；每批最多 20 份。
            </p>
            {files.map((file) => (
              <label className="check-row" key={file.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(file.id)}
                  disabled={
                    busy ||
                    (!selected.includes(file.id) && selected.length >= 20)
                  }
                  onChange={(e) =>
                    setSelected((ids) =>
                      e.target.checked
                        ? [...ids, file.id]
                        : ids.filter((id) => id !== file.id),
                    )
                  }
                />
                <span>
                  {file.title} · {bytes(file.size)} ·{" "}
                  {sourceStatus[file.status] || file.status}
                </span>
              </label>
            ))}
            {!files.length && <p className="muted">暂无可清理文件。</p>}
            <label className="check-row">
              <input
                type="radio"
                name="cleanup-mode"
                checked={mode === "summarize"}
                onChange={() => setMode("summarize")}
              />
              保留有损摘要，再删除原件
            </label>
            <label className="check-row">
              <input
                type="radio"
                name="cleanup-mode"
                checked={mode === "delete"}
                onChange={() => setMode("delete")}
              />
              直接删除所选原件
            </label>
            <button
              className="primary"
              disabled={busy || !selected.length}
              onClick={() =>
                act(async () => {
                  await api("/storage/plans", {
                    method: "POST",
                    body: JSON.stringify({ source_ids: selected, mode }),
                  });
                  setSelected([]);
                })
              }
            >
              生成待确认方案
            </button>
          </section>
          <section className="panel storage-panel">
            <h2>待确认与最近的清理方案</h2>
            <p className="muted">
              来自 Agent 的清理建议也会出现在这里。授权只适用于该批文件和版本，1
              小时后失效；可取消。
            </p>
            {!plans.length && <p>暂无清理方案。</p>}
            {plans.map((plan) => (
              <article className="storage-plan" key={plan.id}>
                <h3>
                  {plan.mode === "summarize" ? "保留摘要后清理" : "删除原件"} ·{" "}
                  {plan.status === "pending"
                    ? "待你确认"
                    : plan.status === "authorized"
                      ? "已授权，待执行"
                      : plan.status === "completed"
                        ? "已执行"
                        : "已取消"}
                </h3>
                <p>
                  预计释放 {bytes(plan.estimated_release_bytes)} · 有效期至{" "}
                  {new Date(plan.expires_at).toLocaleString()}
                </p>
                <ul>
                  {plan.files.map((f: any) => (
                    <li key={f.id}>
                      {f.title} · {bytes(f.size)} · 版本 {f.revision}
                    </li>
                  ))}
                </ul>
                {plan.summary && (
                  <details>
                    <summary>审阅将保留的摘要、归纳与引用（有损）</summary>
                    <pre className="storage-summary">{plan.summary}</pre>
                  </details>
                )}
                <p className="source-warning">{plan.loss_notice}</p>
                {plan.status === "pending" && (
                  <>
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={!!ack[plan.id]}
                        onChange={(e) =>
                          setAck((a) => ({ ...a, [plan.id]: e.target.checked }))
                        }
                      />
                      我已核对清单及摘要，理解删除后无法恢复原件和完整信息
                    </label>
                    <button
                      className="primary"
                      disabled={busy || !ack[plan.id]}
                      onClick={() =>
                        act(async () => {
                          await api(`/storage/plans/${plan.id}/approve`, {
                            method: "POST",
                            body: JSON.stringify({ acknowledge_loss: true }),
                          });
                          await api(`/storage/plans/${plan.id}/execute`, {
                            method: "POST",
                          });
                        })
                      }
                    >
                      批准并执行
                    </button>{" "}
                    <button
                      className="secondary"
                      disabled={busy || !ack[plan.id]}
                      onClick={() =>
                        act(() =>
                          api(`/storage/plans/${plan.id}/approve`, {
                            method: "POST",
                            body: JSON.stringify({ acknowledge_loss: true }),
                          }),
                        )
                      }
                    >
                      授权 Agent 执行该方案
                    </button>
                  </>
                )}
                {plan.status === "authorized" && (
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() =>
                      act(() =>
                        api(`/storage/plans/${plan.id}/execute`, {
                          method: "POST",
                        }),
                      )
                    }
                  >
                    执行已授权方案
                  </button>
                )}{" "}
                {["pending", "authorized"].includes(plan.status) && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() =>
                      act(() =>
                        api(`/storage/plans/${plan.id}/cancel`, {
                          method: "POST",
                        }),
                      )
                    }
                  >
                    取消方案
                  </button>
                )}
                {plan.status === "completed" && <p>{plan.result.message}</p>}
              </article>
            ))}
          </section>
          <section className="panel storage-panel">
            <h2>摘要归档</h2>
            <p className="muted">
              这些内容由你批准保留，原件可能已删除。摘要不能替代原件证据或直接用于自动填表。
            </p>
            {!archives.length && <p>暂无摘要归档。</p>}
            {archives.map((a) => (
              <article className="storage-plan" key={a.id}>
                <h3>{a.title}</h3>
                <pre className="storage-summary">{a.content}</pre>
                <button
                  className="text-button danger-text"
                  disabled={busy}
                  onClick={() => {
                    if (
                      window.confirm(
                        "同时删除此摘要及清理方案中保留的原文片段？",
                      )
                    )
                      act(() =>
                        api(`/storage/archives/${a.id}`, { method: "DELETE" }),
                      );
                  }}
                >
                  删除摘要归档
                </button>
              </article>
            ))}
            {(archivePage > 0 || archiveMore) && (
              <div className="pagination">
                <button
                  disabled={!archivePage || busy}
                  onClick={() => setArchivePage((p) => p - 1)}
                >
                  上一页归档
                </button>
                <span>第 {archivePage + 1} 页</span>
                <button
                  disabled={!archiveMore || busy}
                  onClick={() => setArchivePage((p) => p + 1)}
                >
                  下一页归档
                </button>
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}

function AdminRow({
  user,
  busy,
  save,
}: {
  user: any;
  busy: boolean;
  save: (value: number) => void;
}) {
  const [quota, setQuota] = useState(String(user.quota_bytes / 1024 ** 3));
  useEffect(
    () => setQuota(String(user.quota_bytes / 1024 ** 3)),
    [user.quota_bytes],
  );
  return (
    <article className="storage-plan">
      <h3>{user.display_name}</h3>
      <p>{user.email}</p>
      <p>
        已用 {bytes(user.used_bytes)} · 上传预留 {bytes(user.reserved_bytes)} ·
        当前分配 {bytes(user.quota_bytes)}
      </p>
      <label>
        分配容量（GiB）{" "}
        <input
          type="number"
          min={0}
          max={1048576}
          step="any"
          value={quota}
          onChange={(e) => setQuota(e.target.value)}
        />
      </label>{" "}
      <button
        className="secondary"
        disabled={
          busy ||
          quota === "" ||
          !Number.isFinite(Number(quota)) ||
          Number(quota) < 0
        }
        onClick={() => save(Math.round(Number(quota) * 1024 ** 3))}
      >
        保存容量
      </button>
    </article>
  );
}
