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

const ProcessesPage = lazy(async () => ({
  default: (await import("@/pages/processes-page")).ProcessesPage,
}));

const StatsPage = lazy(async () => ({
  default: (await import("@/pages/stats-page")).StatsPage,
}));

const RepositoriesPage = lazy(async () => ({
  default: (await import("@/pages/repositories-page")).RepositoriesPage,
}));

const RoadmapPage = lazy(async () => ({
  default: (await import("@/pages/roadmap-page")).RoadmapPage,
}));

const IdeasPage = lazy(async () => ({
  default: (await import("@/pages/ideas-page")).IdeasPage,
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
            <Route path="processes" element={<ProcessesPage />} />
            <Route path="stats" element={<StatsPage />} />
            <Route path="repositories" element={<RepositoriesPage />} />
            <Route path="roadmap" element={<RoadmapPage />} />
            <Route path="ideas" element={<IdeasPage />} />
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
