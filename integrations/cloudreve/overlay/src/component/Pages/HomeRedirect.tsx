import SessionManager from "../../session";
import { Navigate } from "react-router-dom";
export const HomeRedirect = () => <Navigate to={SessionManager.currentLoginOrNull()?"/home":"/start"} replace/>;
