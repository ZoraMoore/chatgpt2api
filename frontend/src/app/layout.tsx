import type { Metadata, Viewport } from "next";
import "./globals.css";
import "../styles/global-glass.css";
import { AppShell } from "@/components/shell/app-shell";
import { ThemeScript } from "@/components/theme-script";
import { ThemedToaster } from "@/components/themed-toaster";

export const metadata: Metadata = {
  title: "chatgpt2api 工作台",
  description: "ChatGPT account pool management console",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f5f7" },
    { media: "(prefers-color-scheme: dark)", color: "#161617" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="zh-CN"
      className="h-dvh overflow-hidden"
      suppressHydrationWarning
    >
      <head>
        <ThemeScript />
      </head>
      <body
        data-global-glass=""
        className="h-dvh overflow-hidden text-foreground antialiased"
      >
        <ThemedToaster />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
