import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "WAJE BI v2",
  description: "SQL-first BI Agent investigation workspace",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
