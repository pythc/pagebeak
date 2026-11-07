import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

type Fact = { id: string; kind: string; subject: string; subject_scope: string; entity_key: string;
  label: string; value: string; polarity: string; status: string; revision: number; quote: string;
  valid_from: string | null; valid_to: string | null; evidence_id: string | null; basis: string; adopted_by?: string | null;
  decision?: { actor?: string; reason?: string; at?: string }; conflict?: boolean; effective?: boolean };
type Knowledge = { revision: number; total: number; items: Fact[]; has_more: boolean; markdown: string;
  sections: { kind: string; title: string; count: number }[];
  pending: { tasks: number; candidates: number; conflicts: number; parse_failures: { id: string; title: string }[] };
  tasks: { id: string; title: string; status: string; coverage: string }[] };
const statuses: Record<string, string> = { candidate: "待采纳", accepted: "已采纳", rejected: "已拒绝" };
const polarities: Record<string, string> = { positive: "原文明示", negative: "只否定这个值", absent: "明确不存在" };

export default function KnowledgeView({ pendingOnly, onEvidence }: {
  pendingOnly: boolean; onEvidence: (id: string) => void;
}) {
  const [data, setData] = useState<Knowledge | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<{ fact: Fact; value: string; polarity: string; scope: string } | null>(null);
  const load = useCallback(async () => {
    const next = await api<Knowledge>(`/knowledge?offset=${offset}&limit=100`);
    setData(old => old && old.revision > next.revision ? old : next);
  }, [offset]);
  useEffect(() => {
    let active = true;
    const refresh = () => { if (active) void load().catch(e => { if (active) setError(e.message); }); };
    refresh(); const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [load]);
  const decide = async (f: Fact, action: string, changes = {}) => {
    setBusy(true); setError("");
    try {
      await api(`/knowledge/facts/${f.id}/decisions`, { method: "POST",
        body: JSON.stringify({ action, expected_revision: f.revision, ...changes }) });
      setEdit(null); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "保存未完成，编辑内容已保留"); }
    finally { setBusy(false); }
  };
  if (!data) return <section className="panel">{error || "正在读取知识目录…"}</section>;
  return <div className="knowledge-directory">
    {error && <p className="alert" role="alert">{error}</p>}
    <section className="panel">
      <h2>{pendingOnly ? "待处理" : "知识目录"}</h2>
      <p>等待 Agent 整理 {data.pending.tasks} 份 · 待采纳 {data.pending.candidates} 项 · 冲突 {data.pending.conflicts} 组</p>
      <p className="muted">Agent 可依据原文采纳知识，用于检索和填写。采纳人会明确标记，你仍可修正或撤销；人工修正独立保留。</p>
      {pendingOnly && <>
        {data.tasks.map(t => <p key={t.id}>{t.title} · {t.status === "running" ? "Agent 处理中" : "等待 Agent"}{t.coverage === "partial" && " · 原件部分解析"}</p>)}
        {data.pending.tasks > data.tasks.length && <p className="muted">另有 {data.pending.tasks - data.tasks.length} 份资料等待整理。</p>}
        {data.pending.parse_failures.map(s => <p className="source-warning" key={s.id}>解析需处理：{s.title}</p>)}
      </>}
    </section>
    {edit && <form className="panel catalog-editor" onSubmit={e => {
      e.preventDefault(); void decide(edit.fact, "correct", { value: edit.value, polarity: edit.polarity, subject_scope: edit.scope });
    }}>
      <h2>修正 {edit.fact.subject} · {edit.fact.label}</h2>
      <label>修正值<input required maxLength={500} value={edit.value} onChange={e => setEdit({ ...edit, value: e.target.value })} /></label>
      <label>含义<select value={edit.polarity} onChange={e => setEdit({ ...edit, polarity: e.target.value })}>
        <option value="positive">确认这个值</option><option value="negative">只否定这个值</option><option value="absent">确认整体不存在（填写“无”）</option>
      </select></label>
      <label>归属<select value={edit.scope} onChange={e => setEdit({ ...edit, scope: e.target.value })}>
        <option value="self">本人信息</option><option value="reference">参考信息</option>
      </select></label>
      <p className="muted">这会成为你的独立声明。原资料更新或删除后，仍保留你的修正。</p>
      <div className="catalog-actions"><button className="primary" disabled={busy}>保存人工修正</button><button type="button" className="secondary" onClick={() => setEdit(null)}>取消</button></div>
    </form>}
    {data.sections.map(section => {
      const items = data.items.filter(f => f.kind === section.kind && (!pendingOnly || f.status === "candidate" || f.conflict));
      if (!items.length) return null;
      return <section className="catalog-group" key={section.kind}>
        <h2>{section.title}<span>{section.count}</span></h2>
        {items.map(f => <article className="panel" key={f.id}>
          <div className="catalog-entry-heading"><strong>{f.subject} · {f.label}：{f.polarity === "negative" ? "不是“" + f.value + "”" : f.value}</strong><span>{f.status === "accepted" && !f.effective ? "历史 / 已被覆盖" : statuses[f.status] || f.status}</span></div>
          <p className="catalog-caption">{f.subject_scope === "self" ? "本人信息" : "参考信息，不用于本人填表"} · {polarities[f.polarity]} · {f.basis === "user_asserted" ? "用户独立声明" : "原始资料依据"}{f.status === "accepted" && ` · ${f.adopted_by === "agent" ? "Agent 采纳" : "用户采纳"}`}</p>
          {f.adopted_by === "agent" && f.decision?.reason && <p className="muted">采纳依据：{f.decision.reason}</p>}
          {f.conflict && <p className="source-warning">与其他已确认信息冲突，当前不用于填写。请核对并修正或撤销错误的采纳。</p>}
          {(f.valid_from || f.valid_to) && <p className="muted">有效时间：{f.valid_from || "起始未记载"} — {f.valid_to || "截止未记载"}</p>}
          {f.quote && <blockquote className="catalog-summary">{f.quote}</blockquote>}
          <div className="catalog-actions">
            {f.evidence_id && <button className="evidence-link" onClick={() => onEvidence(f.evidence_id!)}>查看原文位置</button>}
            {f.status === "candidate" && <button className="primary" disabled={busy} onClick={() => decide(f, "accept")}>确认这条信息</button>}
            <button className="secondary" disabled={busy} onClick={() => setEdit({ fact: f, value: f.value, polarity: f.polarity, scope: f.subject_scope })}>修正 / 调整归属</button>
            <button className="text-button" disabled={busy} onClick={() => decide(f, "reject")}>{f.status === "accepted" ? "撤销采纳" : "拒绝候选"}</button>
          </div>
        </article>)}
      </section>;
    })}
    {!data.total && <section className="panel"><p>尚无结构化知识。资料解析后会生成整理任务，Agent 提取并核验后可采纳，结果将在这里显示。</p></section>}
    {(offset > 0 || data.has_more) && <div className="catalog-actions"><button disabled={!offset} onClick={() => { setData(null); setOffset(Math.max(0, offset - 100)); }}>上一页</button><span>第 {offset / 100 + 1} 页</span><button disabled={!data.has_more} onClick={() => { setData(null); setOffset(offset + 100); }}>下一页</button></div>}
    <details className="panel catalog-markdown"><summary>Agent 可读目录（同一份数据）</summary><pre>{data.markdown}</pre></details>
  </div>;
}
