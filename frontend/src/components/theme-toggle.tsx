"use client";
import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
export function ThemeToggle({ className }: { className?: string }) {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const root = document.documentElement;
    const sync = () => setDark(root.classList.contains("dark"));
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return (
    <button
      type="button"
      aria-label="切换主题"
      title={dark ? "切换浅色" : "切换深色"}
      className={cn("glass-icon-button", className)}
      onClick={() => {
        const root = document.documentElement,
          next = !root.classList.contains("dark");
        root.dataset.themeSwitching = "true";
        root.classList.toggle("dark", next);
        root.style.colorScheme = next ? "dark" : "light";
        try {
          localStorage.setItem("chatgpt2api-theme", next ? "dark" : "light");
        } catch {}
        requestAnimationFrame(() => {
          delete root.dataset.themeSwitching;
        });
      }}
    >
      {dark ? <Sun size={18} /> : <Moon size={18} />}
    </button>
  );
}
