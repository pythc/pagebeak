import { Accordion, AccordionDetails, AccordionSummary, Box, Button, Paper, Stack, TextField, Typography } from "@mui/material";
import { ExpandMore } from "@mui/icons-material";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

const questions = [
  { q: "它和普通网盘有什么区别？", a: "网盘帮你保存和管理文件；知序还让助手阅读资料、维护一份你和助手都能看的知识目录，并保留重要信息的原文出处。比如上次已整理过项目经历，下次写申报材料时，助手可以先看目录、找到对应原文，你不用再翻一遍文件、重新介绍背景。" },
  { q: "第一次应该怎么开始？", a: "在“开始使用”点击“在 WorkBuddy 使用知序”，发送准备好的安装请求。已有有效连接时，助手直接继续初始化，不需要再次授权或确认是否继续。首次连接按客户端提示核对权限并授权；若出现连接卡片，点击一次即可。随后选择本机资料的检索范围，不需要填写个人信息表。" },
  { q: "需要把电脑里的文件全部上传吗？", a: "不需要。第一次对话可以选择检索全部文件、指定文件夹或暂不检索。“全部文件”指受支持且有权限访问的个人文档，不是扫描系统目录或其他人的电脑。文件先在本机建立索引，重要原件再按你的授权收录；不会默认上传整台电脑。" },
  { q: "支持哪些文件？大小有限制吗？", a: "支持 Word、Excel、PPT、PDF、图片及常见文本资料。单文件上传上限为 2 GiB，同时受剩余空间限制。Office 在线预览上限为 100 MiB；大文件可以保存，但不保证一次完整解析。复杂排版、表格或扫描件应结合原文核对，上传成功不等于每个字都已识别。" },
  { q: "“目录树”和“知识目录”有什么区别？", a: "目录树展示文件夹和原文件的位置；知识目录按内容整理成主题，例如个人经历、项目、工作成果和参考资料。知识页包含摘要、重要信息和出处，用户与助手读取的是同一份目录。整理知识目录不等于移动或删除原文件。" },
  { q: "上传后，为什么还显示“待整理”？", a: "服务器会先解析文件、识别扫描文字并建立基础索引；归纳摘要和更新知识页由当前助手完成。你可以直接说“整理我新上传的资料，更新知识目录”。WorkBuddy 没有运行时，语义整理会等待下次对话；后台解析和整理完成是两件事。" },
  { q: "需要付模型费用吗？", a: "模型用量按你使用的客户端和模型服务计费。使用 WorkBuddy 时，消耗你自己的账号额度，是否需要付费取决于其当前套餐与用量。本项目不要求你另外提供模型密钥；服务器上的文件解析和 CPU 文字识别不调用你的大模型额度。" },
  { q: "本机文件的内容会发到哪里？", a: "本机索引保存在你的电脑上。助手阅读的资料片段会由你使用的模型服务处理；按需收录的原文件保存到知序文件空间。仅建立本机索引，不代表已上传全部原件。请只选择你愿意交给当前助手处理的范围。" },
  { q: "知识目录写错了，可以自己改吗？", a: "可以。打开“知识目录”，选择知识页后点击“修改”。人工修改会被保留，助手更新资料时不能直接覆盖。原文件变化后，旧内容可能标为待更新；请让助手重新核对来源，再用于正式材料。" },
  { q: "能直接帮我填网页和 Word 吗？", a: "可以让助手先查资料，再根据原文填写你提供的网页或 Word 模板。网页操作需要客户端的浏览器能力以及你对对应页面的授权；Word 操作也取决于客户端能力。让助手保存草稿并重新读取核对，没有依据的字段留空。正式申报、对外提交由你确认。" },
  { q: "换浏览器或电脑，怎样继续用原来的资料？", a: "在仍已连接的助手中说“打开我的资料”，使用它生成的短期、单次链接进入原空间，链接仅限本人，不要转发。新电脑上的本机索引需要重新选择范围建立。当前没有配置外部账号登录，不能只凭姓名或昵称找回空间；请保留至少一个有效连接。" },
  { q: "授权页打不开，或者显示已经过期怎么办？", a: "先让助手检查现有连接：连接有效就可以继续使用，不必打开新的授权页。确实需要重新授权时，使用客户端的连接或重新连接卡片；部分版本需要你点击卡片才能打开授权页。不要重复打开历史消息里的临时链接，也不必重新注册或反复安装助理。本机文件检索范围仍由你另外选择。" },
  { q: "可以接入 WorkBuddy 之外的助手吗？", a: "云端资料服务提供标准 MCP 接入，支持相应授权协议的客户端可以连接。目前提供的是 WorkBuddy 助理包；其他客户端的本机检索、网页和 Word 操作仍需单独适配、验证，不能保证安装和使用方式完全相同。" },
  { q: "我的资料会和其他人的混在一起吗？", a: "资料按个人空间隔离，助手只能访问当前有效授权对应的空间。共用一个浏览器配置会共用该网页会话；共用电脑时请退出当前网页连接。个人空间链接、授权码和令牌都不要发给别人。" },
  { q: "空间快满了，助手会自行删除文件吗？", a: "可以让助手查看剩余空间，列出重复文件、旧版本和可归档资料的建议。删除原文件需要你针对具体文件授权；一份摘要不能完全替代原件。网页里遗忘知识页，也不等于删除原文件。" },
];

export default function HelpPage() {
  const [query, setQuery] = useState("");
  useEffect(() => { document.title = "使用帮助 · 知序"; }, []);
  const visible = questions.filter(item => (item.q + item.a).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <Box component="main" sx={{ width: "100%", height: "100dvh", overflowY: "auto", bgcolor: "background.default", px: 2, py: { xs: 3, sm: 5 } }}>
    <Box sx={{ maxWidth: 920, mx: "auto" }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 4 }}>
        <Stack direction="row" spacing={1.5} alignItems="center"><img src="/static/img/zhixu-mark-v1.svg" width="34" height="34" alt=""/><Typography variant="h6">知序 · Pagebeak</Typography></Stack>
        <Button component={Link} to="/home">返回文件</Button>
      </Stack>
      <Typography component="h1" variant="h4" sx={{ fontWeight: 650, mb: 2 }}>使用帮助</Typography>
      <Typography variant="h6" sx={{ mb: 1 }}>让散落在电脑和云盘里的资料，在需要的时候用得上。</Typography>
      <Typography color="text.secondary" sx={{ lineHeight: 1.9, mb: 3 }}>知序是一位逐渐了解你的办公助手。它把你允许访问的本机文档和上传的文件串起来，整理成有出处的知识目录。以后查资料、写材料或填表，可以从已有积累接着做。</Typography>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} sx={{ mb: 3 }}>
        {[ ["一起找资料", "同时查找已授权的本机文档与云端资料，找到后核对原文。"], ["越用越有积累", "你和助手共用知识目录，记住明确的偏好，保留人工修改。"], ["把资料用起来", "按用途写材料、填写表格，金额和关键事实回到出处核验。"] ].map(([title, body]) => <Paper variant="outlined" key={title} sx={{ p: 2.5, flex: 1 }}><Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 1 }}>{title}</Typography><Typography variant="body2" color="text.secondary" sx={{ lineHeight: 1.8 }}>{body}</Typography></Paper>)}
      </Stack>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ xs: "stretch", sm: "center" }} sx={{ mb: 5 }}>
        <Button component={Link} to="/start" variant="contained">开始使用</Button>
        <Typography variant="body2" color="text.secondary">先试一份简历、工作总结或待填表格，就能开始。</Typography>
      </Stack>
      <Typography component="h2" variant="h5" sx={{ mb: 2 }}>常见问题</Typography>
      <TextField fullWidth label="搜索问题" placeholder="例如：上传、费用、换电脑" value={query} onChange={event => setQuery(event.target.value)} sx={{ mb: 2 }}/>
      {visible.map(item => <Accordion key={item.q} disableGutters elevation={0} sx={{ border: 1, borderColor: "divider", "&:before": { display: "none" } }}>
        <AccordionSummary expandIcon={<ExpandMore/>}><Typography sx={{ fontWeight: 500 }}>{item.q}</Typography></AccordionSummary>
        <AccordionDetails><Typography color="text.secondary" sx={{ lineHeight: 1.9 }}>{item.a}</Typography></AccordionDetails>
      </Accordion>)}
      {!visible.length && <Typography role="status" sx={{ p: 3 }}>没有找到对应问题，换个关键词试试。</Typography>}
      <Typography variant="body2" color="text.secondary" sx={{ mt: 4, mb: 3, lineHeight: 1.9 }}>仍需帮助？可联系维护邮箱 <Box component="a" href="mailto:2813994715@qq.com" sx={{ color: "primary.main" }}>2813994715@qq.com</Box>。说明遇到的步骤即可，不要发送密码或令牌。</Typography>
    </Box>
  </Box>;
}
