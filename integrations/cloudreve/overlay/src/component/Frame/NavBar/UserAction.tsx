import { Button } from "@mui/material";
import { useNavigate } from "react-router-dom";
import SessionManager from "../../../session";
import { GroupBS } from "../../../session/utils.ts";
import { GroupPermission } from "../../../api/user.ts";
import OperatorUserAction from "./OperatorUserAction.tsx";
export default function UserAction() {
  const navigate=useNavigate(), user=SessionManager.currentLoginOrNull()?.user;
  if (user && GroupBS(user).enabled(GroupPermission.is_admin)) return <OperatorUserAction/>;
  return <Button onClick={()=>navigate("/help")}>使用帮助</Button>;
}
