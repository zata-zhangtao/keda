import { BrowserRouter } from "react-router";

import { SessionProvider } from "@/auth/SessionProvider";
import { MainApp } from "@/main-app";
import { Toaster } from "@/components/ui/sonner";

export function App() {
  return (
    <BrowserRouter>
      <SessionProvider>
        <MainApp />
        <Toaster position="top-right" richColors />
      </SessionProvider>
    </BrowserRouter>
  );
}
