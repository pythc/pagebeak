import { useLocation, useNavigate } from "react-router-dom";
import { useMediaQuery, useTheme } from "@mui/material";
import { useAppDispatch } from "../../redux/hooks.ts";
import { setDrawerOpen } from "../../redux/globalStateSlice.ts";
import SessionManager from "../../session";
import HomeOutlined from "../Icons/HomeOutlined.tsx";
import SideNavItem from "../Frame/NavBar/SideNavItem.tsx";
import { MenuBookOutlined } from "@mui/icons-material";

export default function StartEntry() {
  const navigate = useNavigate(), location = useLocation(), dispatch = useAppDispatch();
  const mobile = useMediaQuery(useTheme().breakpoints.down("sm"));
  if (!SessionManager.currentLoginOrNull()) return null;
  const open = (path: string) => {
    navigate(path);
    if (mobile) dispatch(setDrawerOpen(false));
  };
  return <><SideNavItem label="开始使用" icon={<HomeOutlined />} active={location.pathname === "/start"} onClick={() => open("/start")} />
    <SideNavItem label="知识目录" icon={<MenuBookOutlined />} active={location.pathname === "/knowledge"} onClick={() => open("/knowledge")} /></>;
}
