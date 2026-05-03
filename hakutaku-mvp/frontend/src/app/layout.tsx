import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Hakutaku — Organizational Intelligence",
  description: "Graph + reasoning + proposals over unstructured org content",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="pt-BR">
      <body className="bg-slate-950 text-slate-100 min-h-screen antialiased">
        <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur">
          <div className="max-w-7xl mx-auto px-6 py-4 flex items-center gap-8">
            <Link href="/" className="text-xl font-bold tracking-tight">
              <span className="text-cyan-400">白</span>{" "}
              <span>Hakutaku</span>
            </Link>
            <nav className="flex gap-6 text-sm font-medium">
              <Link
                href="/"
                className="text-slate-300 hover:text-white transition"
              >
                Dashboard
              </Link>
              <Link
                href="/graph"
                className="text-slate-300 hover:text-white transition"
              >
                Grafo
              </Link>
              <Link
                href="/proposals"
                className="text-slate-300 hover:text-white transition"
              >
                Propostas
              </Link>
            </nav>
            <div className="ml-auto text-xs text-slate-500">
              Organizational Intelligence Layer · MVP
            </div>
          </div>
        </header>
        <main className="max-w-7xl mx-auto px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
