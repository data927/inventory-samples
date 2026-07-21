import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { INVENTORY_BASE_PATH_ATTR } from "@/lib/apiBase";
import { PUBLIC_URL_PREFIX } from "@/lib/deployBasePath";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Inventory Intelligence",
  description: "Turn messy folders and repos into a clean, evidence-backed inventory workbook.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      {...{ [INVENTORY_BASE_PATH_ATTR]: PUBLIC_URL_PREFIX || "" }}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
