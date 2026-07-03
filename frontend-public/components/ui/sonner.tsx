import type { CSSProperties } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      closeButton
      className="toaster group"
      style={
        {
          "--normal-bg": "rgba(255, 255, 255, 0.96)",
          "--normal-text": "rgb(15 23 42)",
          "--normal-border": "rgb(226 232 240)",
          "--border-radius": "18px",
        } as CSSProperties
      }
      toastOptions={{
        duration: 2000,
        classNames: {
          toast: "border border-slate-200 bg-white text-slate-900 shadow-sm",
          title: "text-sm font-semibold text-slate-950",
          description: "text-sm text-slate-600",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
