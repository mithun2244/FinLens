import type { Metadata } from "next";
import { JetBrains_Mono, Sora } from "next/font/google";
import "./globals.css";

// The typefaces the design specifies. Loaded through next/font so they are
// self-hosted and preloaded rather than fetched from Google at runtime.
const sora = Sora({
  variable: "--font-sora",
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "FinLens — Multimodal Document Intelligence",
  description:
    "Upload a financial document and ask why a charge was deducted. Every answer is grounded in the document and cites the page it came from.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${sora.variable} ${jetbrainsMono.variable} antialiased`}
    >
      <body>{children}</body>
    </html>
  );
}
