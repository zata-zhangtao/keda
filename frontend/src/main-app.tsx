import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router";

import { RequireSession } from "@/auth/RequireSession";
import { AppSidebar } from "@/components/app-sidebar";
import { SiteHeader } from "@/components/site-header";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

const LoginPage = lazy(async () => ({
  default: (await import("@/pages/login-page")).LoginPage,
}));

const DashboardPage = lazy(async () => ({
  default: (await import("@/pages/dashboard-page")).DashboardPage,
}));

function PageLoadingFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
      加载中...
    </div>
  );
}

function AppShell() {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <SiteHeader />
        <Suspense fallback={<PageLoadingFallback />}>
          <Routes>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<DashboardPage />} />
            {/* TODO: 在此添加更多受保护的路由 */}
          </Routes>
        </Suspense>
      </SidebarInset>
    </SidebarProvider>
  );
}

export function MainApp() {
  return (
    <Routes>
      <Route
        path="login"
        element={
          <Suspense fallback={<PageLoadingFallback />}>
            <LoginPage />
          </Suspense>
        }
      />
      <Route element={<RequireSession />}>
        <Route path="/*" element={<AppShell />} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
}
