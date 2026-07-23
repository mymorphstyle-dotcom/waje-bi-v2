import type { Metadata } from "next";
import "./globals.css";
import { TooltipProvider } from "@/components/ui/tooltip";

export const metadata: Metadata = {
  title: "WAJE BI v2",
  description: "SQL-first BI Agent investigation workspace",
};

// Per-request CSP nonces cannot be attached to statically generated script tags.
export const dynamic = "force-dynamic";

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body><TooltipProvider>{children}</TooltipProvider></body>
    </html>
  );
}
