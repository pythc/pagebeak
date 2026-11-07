import { Alert, Button, Paper, Stack, Typography } from "@mui/material";
import { useEffect, useState } from "react";
import { knowledgeRequest } from "./bridge.ts";

type Authorization = {client_name: string; display_name: string; knowledge_base_id: string; nonce: string; scopes: string[]};
const labels: Record<string, string> = {"knowledge:read": "读取资料和目录", "knowledge:write": "收录和整理资料",
  "profile:read": "读取初始化信息", "forms:write": "准备并校验填写内容"};

export default function AssistantConsent({reference, knowledgeBaseId}: {reference: string; knowledgeBaseId: string}) {
  const [data, setData] = useState<Authorization | null>(null), [error, setError] = useState("");
  const [busy, setBusy] = useState(false), [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setData(null); setError("");
    void knowledgeRequest("assistant/authorization?reference=" + encodeURIComponent(reference)).then(next => {
      if (active) { if (next.knowledge_base_id !== knowledgeBaseId) setError("账号已变化，请重新连接"); else setData(next); }
    }).catch(e => {if (active) setError(e instanceof Error ? e.message : "无法检查连接请求");});
    return () => {active = false;};
  }, [reference, knowledgeBaseId, attempt]);
  const decide = async (allowed: boolean) => {
    if (!data || busy) return;
    setBusy(true); setError("");
    try {
      const result = await knowledgeRequest("assistant/authorization", {reference, nonce: data.nonce, allowed});
      const url = new URL(result.redirect_url);
      const permitted = ["https:", "workbuddy:", "codebuddy:"].includes(url.protocol)
        || url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
      if (!permitted) throw new Error("客户端回调地址无效，请重新连接");
      window.location.href = url.href;
    } catch(e) {setError(e instanceof Error ? e.message : "授权尚未完成");} finally {setBusy(false);}
  };
  return <Paper variant="outlined" sx={{p:3, mb:3}}>
    <Typography variant="h6" component="h2">连接知序教师助理</Typography>
    {error && <Alert severity="error" sx={{my:2}} action={<Button onClick={()=>setAttempt(v=>v+1)}>重新检查</Button>}>{error}</Alert>}
    {!data ? <Typography sx={{mt:1}}>正在核对当前文件账号与连接请求…</Typography> : <>
      <Typography sx={{my:1}}>允许“{data.client_name}”访问 {data.display_name} 的资料空间：</Typography>
      <Typography color="text.secondary">{data.scopes.map(scope=>labels[scope] || scope).join("、")}。</Typography>
      <Typography color="text.secondary" sx={{my:2}}>模型使用你自己的 WorkBuddy 账号额度。助理按你的要求处理资料，人工修改继续保留。</Typography>
      <Stack direction="row" spacing={1}><Button variant="contained" disabled={busy} onClick={()=>void decide(true)}>允许并返回 WorkBuddy</Button>
        <Button disabled={busy} onClick={()=>void decide(false)}>取消连接</Button></Stack>
    </>}
  </Paper>;
}
