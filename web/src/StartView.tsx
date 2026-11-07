import { useEffect, useState } from "react";
import { api } from "./api";

export type OnboardingState = {
  revision: number;
  knowledge_base_id: string;
  skill_version: string;
  connected: boolean;
  installation: { server_name: string; reported_at: string; skill_version: string; channel?: "manual" | "expert" } | null;
  completed_at: string | null;
  dismissed: boolean;
  source_count: number;
  answers: Record<string, string>;
  skipped: string[];
  next_question: { key: string; prompt: string } | null;
  pending_questions?: { key: string; prompt: string }[];
  distribution: { expert_name: string; initialization_url: string };
};
type Instruction = { prompt: string; expires_at: string };
type PackageLaunch = { launch_url: string; expires_at: string; expert_name: string };

export default function StartView({ onCatalog, onUpload, onAdvanced }: {
  onCatalog: () => void; onUpload: () => void; onAdvanced: () => void;
}) {
  const [state, setState] = useState<OnboardingState | null>(null);
  const [instruction, setInstruction] = useState<Instruction | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [copied, setCopied] = useState(false), [now, setNow] = useState(Date.now());
  const [copyNotice, setCopyNotice] = useState("");
  const [packageDownloaded, setPackageDownloaded] = useState(false), [legacy, setLegacy] = useState(false);
  const [packageLaunch, setPackageLaunch] = useState<PackageLaunch | null>(null);
  const fail = (error: unknown) => setError(error instanceof Error ? error.message : "暂时无法读取进度，请重试");
  const refresh = () => api<OnboardingState>("/onboarding").then(setState);
  useEffect(() => {
    let active = true;
    const read = () => api<OnboardingState>("/onboarding")
      .then((value) => { if (active) { setState(value); setError(""); } })
      .catch((e) => { if (active) fail(e); });
    void read();
    const timer = setInterval(() => {
      setNow(Date.now());
      if (!document.hidden) void read();
    }, 4000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  const expired = !!instruction && Date.parse(instruction.expires_at) <= now;
  const copy = async (text: string, message = "") => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setCopyNotice(message); }
    catch { setError("浏览器未允许复制。展开下面的文字，手动选中并复制即可。"); }
  };
  const connect = async () => {
    setBusy(true); setError(""); setCopied(false);
    try {
      const value = await api<Instruction>("/onboarding/connection", { method: "POST" });
      setInstruction(value); setNow(Date.now()); await copy(value.prompt);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const downloadPackage = async () => {
    if (!state) return;
    setBusy(true); setError("");
    try {
      const blob = await api<Blob>("/onboarding/package", { method: "POST" });
      const url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = `zhixu-${state.knowledge_base_id}-expert.zip`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setPackageDownloaded(true);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const launchPackage = async () => {
    setBusy(true); setError("");
    try {
      const value = await api<PackageLaunch>("/onboarding/package/launch", { method: "POST" });
      setPackageLaunch(value); window.location.href = value.launch_url;
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const exit = async () => {
    if (!state) return;
    setBusy(true);
    try {
      await api("/onboarding", { method: "PATCH", body: JSON.stringify({ expected_revision: state.revision, dismissed: true }) });
      onCatalog();
    } catch (e) { fail(e); void refresh().catch(fail); } finally { setBusy(false); }
  };
  const serverName = state?.installation?.server_name || `zhixu-${state?.knowledge_base_id || "（请从开始页面取得身份）"}`;
  const continuation = `请使用 personal-knowledge 知识库管家 Skill 继续初始化知序。仅使用连接 ${serverName}，目标知识库 ID 为 ${state?.knowledge_base_id || "（请从开始页面取得身份）"}，不能回退其他库。先 get_knowledge_context(expected_knowledge_base_id="${state?.knowledge_base_id || ""}") 核对身份，调用 start_onboarding（skill_version=${state?.skill_version || "2026.10.17.1"}）和 read_onboarding；就在当前对话继续，保留已有回答，结合上下文合并询问缺项、多项回答一并保存，并直接完成初始化和整理资料；不猜测我的身份，不要求回网页点击初始化、另开对话或再次召唤专家。只有宿主实际要求授权或工具确实不可用时处理必要操作，不重复安装或修改保护设置。`;
  const startLink = state?.distribution?.initialization_url;
  return <div className="start-view">
    <header className="start-heading">
      <span className="eyebrow">从第一份资料开始</span>
      <h1>{state?.connected && state?.completed_at ? "准备好了，试着用一次" : "让资料替你省一点事"}</h1>
      <p>把散落的文件放在一起。需要找资料、写材料或填表时，让助手带着依据帮你完成。</p>
    </header>
    <ol className="start-steps" aria-label="开始使用的进度">
      {["连接助手", "让助手认识你", "用一份资料试试"].map((label, index) => {
        const done = index === 0 ? state?.connected : index === 1 ? !!state?.completed_at : !!state?.source_count;
        return <li key={label} className={done ? "done" : ""}><span>{done ? "✓" : index + 1}</span>{label}</li>;
      })}
    </ol>
    {error && <div className="alert" role="alert">{error}</div>}
    {copyNotice && <p className="start-copy-notice" role="status">{copyNotice}</p>}
    {!state ? <p role="status">正在读取开始进度…</p> : <>
      {state.source_count > 0 && <p className="start-existing">你已有 {state.source_count} 份资料，我们会继续使用，保留原件和人工修改。</p>}
      {!state.connected && <section className="start-main panel">
        <span className="start-kicker">管家与资料库连接，一起接入</span>
        <h2>把知序装进 WorkBuddy</h2>
        <p>接入包包含管家的使用方法和本资料库的专属连接。安装和连接就绪后，助手会在当前对话了解需求并整理资料；相关信息可以一起提供，无需再点击初始化。</p>
        <div className="start-actions"><button className="primary" disabled={busy} onClick={launchPackage}>{busy ? "正在准备…" : "在 WorkBuddy 安装 · 本机测试"}</button><button className="secondary" disabled title="尚未发布 WorkBuddy 市场资产">正式一键安装 · 待发布</button></div>
        <p>点击后打开 WorkBuddy，发送已准备好的安装请求，由助手完成接入包安装；无需复制命令，安装和连接权限以客户端提示为准。</p>
        {packageLaunch && <p role="status">已发起打开 WorkBuddy 的请求，尚未确认安装。<a href={packageLaunch.launch_url}>没有打开？再试一次</a></p>}
        {startLink && <details className="start-expert-entry"><summary>对话丢失？恢复使用</summary>
          <p>安装后继续刚才的对话即可。仅在对话丢失或没有打开时，使用下面的恢复入口。</p>
          <div className="start-actions"><button className="secondary" onClick={() => { window.location.href = startLink; }}>重新打开 WorkBuddy</button><button className="text-button" onClick={() => refresh().catch(fail)}>检查接入进度</button></div>
          <p className="start-permission">打开后，仅在确实需要恢复对话时发送准备好的请求；如果出现连接授权，按客户端实际提示处理，不需要重新安装。</p>
        </details>}
        <button className="text-button" disabled={busy} onClick={downloadPackage}>{packageDownloaded ? "重新下载接入包" : "下载接入包作本地验证"}</button>
        <p className="start-permission">当前为本机验收版，仅供这台电脑使用。首次连接需在下载后 15 分钟内完成；接入包与账号绑定，请勿分享。下载成功不代表安装或授权已完成。</p>
        {packageDownloaded && <p role="status">接入包已下载。请使用 WorkBuddy 提供的专家导入入口；若当前版本没有导入入口，请保留接入包进行本地安装验证。过期可重新生成，已有可用连接不会被撤销。</p>}
        {state.installation?.channel === "expert" && <p>已收到本接入包的连接回执，下一步由管家核对资料库身份并开始提问。</p>}
        <button className="text-button" onClick={() => setLegacy(!legacy)}>{legacy ? "收起兼容接入" : "使用原来的复制指令接入"}</button>
      </section>}
      {(!state.connected && !legacy) ? null : !state.connected && state.installation ? <section className="start-main panel">
        <span className="start-kicker">安装已完成 · 等待助手核验</span>
        <h2>继续刚才的 WorkBuddy 对话</h2>
        <p>连接文件和技能已经就位，助手应在当前对话核验连接并继续初始化，不需要回网页启动。</p>
        <p>如果客户端提示连接授权，按提示完成后继续原任务；只有工具确实不可见时才使用下方恢复方式，不预设重启或新对话为必经步骤。</p>
        <div className="start-connector-name"><code>{state.installation.server_name}</code><button className="text-button" onClick={() => copy(state.installation!.server_name, "连接名称已复制，可粘贴到 WorkBuddy 的连接器搜索中。")}>复制连接名称</button></div>
        <div className="start-actions"><button className="primary" onClick={() => copy(continuation, "恢复指令已复制；仅在原对话无法继续时使用。")}>复制恢复对话指令</button><button className="text-button" onClick={() => refresh().catch(fail)}>我已选用，检查进度</button></div>
        <details className="start-manual-copy"><summary>查看继续指令 / 手动复制</summary><textarea aria-label="继续初始化指令" readOnly value={continuation} rows={5} /></details>
        <p className="start-permission">这里在助手实际调用知识库后自动更新，不需要点击本页才能开始。安装收据不代表工具已加载。</p>
      </section> : !state.connected ? <section className="start-main panel">
        <span className="start-kicker">第一步 · 交给助手来连接</span>
        <h2>复制一段文字，发给这台电脑上的 WorkBuddy</h2>
        <p>WorkBuddy 是能帮你操作电脑的 AI 助手。打开它，新建一个对话，把下面的接入指令粘贴并发送。助手会安装知识库连接和使用说明，再开始与你交谈。</p>
        <div className="start-actions">
          <button className="primary" disabled={busy} onClick={() => instruction && !expired ? copy(instruction.prompt) : connect()}>
            {busy ? "正在准备…" : expired ? "重新生成并复制" : copied ? "再次复制接入指令" : "复制接入指令"}
          </button>
          <a className="text-button" href="https://www.workbuddy.cn/" target="_blank" rel="noreferrer">还没有 WorkBuddy？</a>
        </div>
        <p className="start-permission">点击后，允许助手读取和整理当前账号的资料、提出信息候选和准备填写草稿。个人信息确认、资料删除和对外提交仍由你决定。</p>
        <p className="start-permission">如果 WorkBuddy 提示安装或连接授权，确认是知序接入后允许本次操作即可；不需要把登录密码发给助手。</p>
        {instruction && <div className="start-instruction">
          <p role="status">{expired ? "这段接入指令已过期，请重新生成。" : copied ? "已复制。现在去 WorkBuddy 粘贴并发送，连接成功后这里会自动更新。" : "接入指令已准备好，请复制并发送。"}</p>
          <small>指令 15 分钟内有效，只能使用一次；不包含长期访问令牌。若已安装，在原对话继续即可；只有连接确实受阻时才使用恢复方式。</small>
          <details><summary>查看要发送的文字 / 手动复制</summary><textarea aria-label="接入指令" readOnly value={instruction.prompt} rows={10} /></details>
          <button className="text-button" onClick={() => refresh().catch(fail)}>我已发送，检查连接</button>
          <button className="text-button" disabled={busy} onClick={connect}>重新生成指令</button>
        </div>}
      </section> : <section className="start-main panel">
        <span className="start-kicker">{state.completed_at ? "基础引导已完成" : "第二步 · 在同一对话继续"}</span>
        <h2>{state.completed_at ? "你的助手已经了解这些信息" : "连接已核验，在对话中继续了解需求"}</h2>
        {state.completed_at ? <dl className="start-answers">{[["preferred_name", "称呼"], ["identity", "身份"], ["goal", "优先用途"]].map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{state.answers[key] || "你选择暂不提供"}</dd></div>)}</dl> : <>
          <p>助手会了解怎么称呼你、你平时做什么，以及最希望它先帮你解决什么。已有回答会保留，不想说的可以直接告诉它“跳过”。</p>
          {(state.pending_questions || (state.next_question ? [state.next_question] : [])).length > 0 && <blockquote className="start-question">{(state.pending_questions || (state.next_question ? [state.next_question] : [])).map(question => question.prompt).join(" ")}</blockquote>}
          <p className="muted">这一步只保存引导上下文，不会未经复核就把回答带入正式表单。</p>
        </>}
        <div className="start-actions">{state.installation?.channel === "expert" && startLink ? <button className="secondary" onClick={() => { window.location.href = startLink; }}>回到 WorkBuddy 继续</button> : <button className="secondary" onClick={() => copy(continuation, "继续指令已复制，请在 WorkBuddy 粘贴并发送。")}>复制“继续初始化”</button>}<button className="text-button" onClick={onAdvanced}>连接设置</button></div>
      </section>}
      <section className="start-trial panel">
        <div><span className="start-kicker">第三步 · 从小处开始</span><h2>{state.source_count ? "让助手帮你找到一条有依据的信息" : "先放一份简历或工作材料就够了"}</h2><p>支持 Word、Excel、PPT、PDF 等常见文件。可以先添加一份，也可以稍后再补；不用一次准备全部资料。</p></div>
        <div className="start-actions"><button className="secondary" onClick={onUpload}>添加一份资料</button><button className="text-button" onClick={onCatalog}>查看共用目录</button></div>
      </section>
      <details className="start-help"><summary>已经安装，或没有自动连上？</summary><p>安装成功后先在原对话继续，按客户端实际提示完成必要授权。只有工具确实不可见时，再在连接器管理中检查此名称；不把重启或新建对话作为默认步骤。</p><button className="secondary" onClick={() => copy(continuation, "继续指令已复制，请在选用正确连接后发送。")}>复制“继续初始化”</button><p>需要恢复时，在原对话选用正确连接并发送恢复指令；具体重连方式以客户端实际提示为准，不需要重复安装或关闭保护设置。</p><p>普通网页聊天无法安装本机连接，请在同一台电脑的 WorkBuddy 中操作。当前为本地开发版，知识库服务需要保持运行。</p><button className="text-button" onClick={onAdvanced}>查看手动连接设置</button></details>
      <button className="text-button start-skip" disabled={busy} onClick={exit}>先看看资料库，稍后继续</button>
    </>}
  </div>;
}
