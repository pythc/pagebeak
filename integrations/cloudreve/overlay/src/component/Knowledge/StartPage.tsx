import { Alert, Box, Button, CircularProgress, Paper, Stack, Typography } from "@mui/material";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import SessionManager from "../../session";
import { store } from "../../redux/store.ts";
import { getAccessToken } from "../../api/request.ts";
import { knowledgeOrigin } from "./bridge.ts";

type Identity = {required: boolean; verified: boolean; providers: {key: string; label: string}[]};
type Connection = {id: string; client_name: string; scopes: string[]};
type Welcome = {identity: Identity; identity_management: boolean; csrf: string; has_space: boolean; installation: {launch_url: string; package_url: string} | null; consent?: {client_name: string; scopes: string[]}};
const scopeLabels: Record<string,string> = {"knowledge:read":"读取资料与目录", "knowledge:write":"收录和整理资料", "profile:read":"读取使用偏好", "forms:write":"准备并校验填写内容"};

export default function StartPage() {
  const [state,setState] = useState<Welcome | null>(null), [error,setError] = useState("");
  const [connections,setConnections] = useState<Connection[] | null>(null);
  const [busy,setBusy] = useState(false), [opened,setOpened] = useState(false);
  const [expired,setExpired] = useState(false);
  const initialized = useRef(false);
  const reference = new URLSearchParams(window.location.search).get("assistant_request");
  const restoreCode = useRef(new URLSearchParams(window.location.hash.slice(1)).get("space"));
  const request = async (path:string, body?:unknown, csrf?:string, adopt=false) => {
    const headers:Record<string,string> = {"Content-Type":"application/json"};
    if (csrf) headers["X-CSRF-Token"]=csrf;
    if (adopt && SessionManager.currentLoginOrNull()) {
      try {const token=await store.dispatch(getAccessToken()); if(token) headers["X-Cloudreve-Token"]=token.replace(/^Bearer /,"");} catch { /* Never infer identity from an expired browser entry. */ }
    }
    const response=await fetch(knowledgeOrigin()+"/v1/welcome"+path,{method:body===undefined?"GET":"POST",credentials:"include",headers,...(body===undefined?{}:{body:JSON.stringify(body)})});
    const result=await response.json();
    if(!response.ok) throw Object.assign(new Error(typeof result.detail === "string"?result.detail:"连接尚未完成，请重试"),{status:response.status});
    return result;
  };
  const files = async (s:Welcome, create=true) => {
    if(create) await request("/space",{},s.csrf,true);
    // Managed accounts get an independent browser session. Legacy, already linked
    // accounts retain their verified browser session and existing file ownership.
    const login=await request("/session",{},s.csrf,true);
    if(!login.existing) SessionManager.upsert(login);
    window.location.assign("/home");
  };
  const load = async () => {
    setError("");setExpired(false);
    try {
      const raw=await request(reference?"?reference="+encodeURIComponent(reference):"");
      // Cached pages may briefly reach the previous backend during a rollback.
      const next:Welcome={...raw,identity:raw.identity??{required:false,verified:false,providers:[]},identity_management:Boolean(raw.identity)};
      setState(next);
      if(restoreCode.current) {
        const code=restoreCode.current; restoreCode.current=null;
        window.history.replaceState({},"",window.location.pathname+window.location.search);
        await request("/restore",{code},next.csrf,true); await files(next,false);
      }
    } catch(e) {const ended=Boolean(reference && (e as {status?:number})?.status===410);setExpired(ended);setError(ended?"授权页已过期。请回到助手重新连接，它会打开新的授权页。":e instanceof Error?e.message:"暂时无法连接，请重试");}
  };
  useEffect(()=>{document.title="开始使用 · 知序"; if(!initialized.current){initialized.current=true;void load();}},[]);
  const connect = async () => {
    if(!state || !reference) return;
    setBusy(true);setError("");
    try {
      await request("/space",{},state.csrf,true);
      const description=await request("/authorization?reference="+encodeURIComponent(reference));
      const result=await request("/authorization",{reference,nonce:description.nonce,allowed:true},state.csrf);
      // The server validates the exact registered callback and PKCE client.
      // Keep native protocol compatibility in one server policy.
      const target=new URL(result.redirect_url);
      window.location.assign(target.href);setOpened(true);
    } catch(e) {setError(e instanceof Error?e.message:"连接未完成，请重试");} finally {setBusy(false);}
  };
  const login = async (provider:string, intent:"signin" | "link") => {
    if(!state) return;
    setBusy(true);setError("");
    try {
      const result=await request("/identity/start",{provider,intent,...(reference?{reference}:{})},state.csrf);
      window.location.assign(result.authorization_url);
    } catch(e) {setError(e instanceof Error?e.message:"账号授权未完成");setBusy(false);}
  };
  const signOut = async () => {
    if(!state) return;
    setBusy(true);setError("");
    try {
      await request("/identity/logout",{},state.csrf);
      SessionManager.signOutCurrent();setConnections(null);setOpened(false);await load();
    } catch(e) {setError(e instanceof Error?e.message:"退出未完成");} finally {setBusy(false);}
  };
  const needsIdentity=Boolean(state?.identity.required && !state.identity.verified);
  const hasIdentityOptions=Boolean(state?.identity.providers.length);
  const openAssistant=()=>{if(state?.installation){window.location.assign(state.installation.launch_url);setOpened(true);}};
  return <Box component="main" sx={{width:"100%",height:"100dvh",overflowY:"auto",bgcolor:"background.default",color:"text.primary",px:2,py:{xs:4,sm:8}}}>
    <Box sx={{maxWidth:620,mx:"auto"}}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{mb:4}}><Stack direction="row" spacing={1.5} alignItems="center"><img src="/static/img/zhixu-mark-v1.svg" width="40" height="40" alt=""/><Typography variant="h5">知序</Typography></Stack><Button component={Link} to="/help">使用帮助</Button></Stack>
      <Typography variant="h4" component="h1" sx={{fontSize:{xs:28,sm:34},fontWeight:650,lineHeight:1.4,mb:2}}>{reference?"连接你的个人资料空间":"让资料成为你的工作助理"}</Typography>
      <Typography color="text.secondary" sx={{mb:4,lineHeight:1.9}}>找资料、整理目录、写材料和填表。让助理从你已有的文件开始。</Typography>
      {error && <Alert severity="error" sx={{mb:2}} action={expired?<Button component={Link} to="/start" reloadDocument>返回开始页</Button>:<Button onClick={()=>void load()}>重试</Button>}>{error}</Alert>}
      {!state && !error && <CircularProgress aria-label="正在准备"/>}
      {state && <Paper variant="outlined" sx={{p:{xs:3,sm:4},borderRadius:3}}>
        {needsIdentity?<>
          <Typography sx={{mb:1}}>{state.has_space?"绑定账号，保留当前资料":"使用已有账号连接"}</Typography>
          <Typography color="text.secondary" sx={{mb:3}}>{state.has_space?"验证账号后，当前目录、原文件和人工修改都会保留，换电脑后也能继续使用。":"首次授权后自动建立个人空间，换浏览器或换助手仍可使用同一份资料。"}</Typography>
          <Stack spacing={1.5}>{state.identity.providers.map(p=><Button key={p.key} fullWidth variant="contained" disabled={busy} onClick={()=>void login(p.key,state.has_space?"link":"signin")}>{state.has_space?"绑定":"使用"}{p.label}{state.has_space?"并保留资料":"继续"}</Button>)}</Stack>
        </>:reference?<>
          <Typography sx={{mb:1}}>允许“{state.consent?.client_name || "Agent 客户端"}”连接你的个人空间：</Typography>
          <Typography color="text.secondary" sx={{mb:3}}>{state.consent?.scopes.map(s=>scopeLabels[s]||s).join("、")}。本次连接不会扫描或上传电脑文件。</Typography>
          <Button size="large" fullWidth variant="contained" disabled={busy || opened} onClick={()=>void connect()}>{busy?"正在准备你的空间…":opened?"已连接，请返回助手":"连接并开始"}</Button>
        </>:<>
          {state.installation?<Button size="large" fullWidth variant="contained" onClick={openAssistant}>{opened?"重新打开 WorkBuddy":"在 WorkBuddy 使用知序"}</Button>:<Typography sx={{overflowWrap:"anywhere"}}>在支持 OAuth 的 MCP 客户端添加服务地址：{knowledgeOrigin()}/mcp</Typography>}
          <Typography sx={{mt:3,mb:1}}>第一次对话，你可以选择：</Typography>
          <Stack spacing={1}><Typography>检索全部文件</Typography><Typography>检索指定文件夹</Typography><Typography>暂不检索，先用上传的资料</Typography></Stack>
          <Typography variant="body2" color="text.secondary" sx={{mt:2}}>先在本机建立索引，重要资料按需收录，不会默认上传整台电脑。</Typography>
          <Button sx={{mt:2}} disabled={busy} onClick={async()=>{setBusy(true);try{await files(state);}catch(e){setError(e instanceof Error?e.message:"未能打开资料");}finally{setBusy(false);}}}>打开我的资料</Button>
        </>}
      </Paper>}
      <Typography variant="body2" color="text.secondary" sx={{mt:3,lineHeight:1.8}}>模型费用由你使用的客户端和模型服务计费。{state?.identity.required?"资料归属你的账号；每个助手独立授权，可以分别取消访问。":hasIdentityOptions?"当前空间尚可通过已授权助理恢复；绑定已有账号后，换设备也能继续使用。":"换浏览器时，可以让已连接的助理“打开我的资料”，继续使用原空间。"}</Typography>
      {state?.has_space && state.identity_management && <Box component="details" sx={{mt:3}} onToggle={async(e)=>{if(e.currentTarget.open && connections===null){try{setConnections(await request("/connections"));}catch{setError("未能读取已授权客户端，请刷新重试");}}}}>
        <Box component="summary" sx={{cursor:"pointer"}}>{hasIdentityOptions?"账号与已授权助手":"已连接的助手"}</Box>
        {hasIdentityOptions && <Typography variant="body2" sx={{mt:2}}>{state.identity.verified?"账号已绑定":"当前空间尚未绑定外部账号"}</Typography>}
        <Stack spacing={1} sx={{mt:1}}>{state.identity.providers.map(p=><Button key={p.key} disabled={busy} onClick={()=>void login(p.key,"link")}>绑定{p.label}，保留当前资料</Button>)}</Stack>
        {connections?.map(c=><Stack key={c.id} direction="row" justifyContent="space-between" alignItems="center"><Typography>{c.client_name}</Typography><Button disabled={busy} onClick={async()=>{setBusy(true);try{await request("/connections/"+encodeURIComponent(c.id)+"/revoke",{},state.csrf);setConnections(await request("/connections"));}catch{setError("未能取消授权，请重试");}finally{setBusy(false);}}}>取消授权</Button></Stack>)}
        {connections?.length===0 && <Typography variant="body2" color="text.secondary">暂无已授权助手</Typography>}
        <Button disabled={busy} onClick={()=>void signOut()}>{hasIdentityOptions?"退出当前账号":"退出当前网页连接"}</Button>
      </Box>}
      {!reference && state?.installation && <Box component="details" sx={{mt:3,color:"text.secondary"}}><Box component="summary" sx={{cursor:"pointer"}}>打不开 WorkBuddy？</Box><Stack direction="row" spacing={2} sx={{mt:1}}><Button href="https://www.codebuddy.cn/work/" target="_blank" rel="noopener noreferrer">安装 WorkBuddy</Button>{state?.installation && <Button href={state.installation.package_url}>下载助理包</Button>}</Stack><Typography variant="body2">按 WorkBuddy 正常提示添加助理，再连接个人资料空间即可。无需配置连接器或运行安装命令。</Typography></Box>}
    </Box>
  </Box>;
}
