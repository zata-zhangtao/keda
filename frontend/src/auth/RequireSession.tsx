import { Navigate, Outlet, useLocation } from "react-router";

import { useSession } from "@/auth/SessionProvider";

export function RequireSession() {
  const location = useLocation();
  const { status } = useSession();

  if (status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        加载中...
      </div>
    );
  }

  if (status !== "authenticated") {
    const redirectPath = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to={`/login?to=${encodeURIComponent(redirectPath)}`} replace />;
  }

  return <Outlet />;
}
