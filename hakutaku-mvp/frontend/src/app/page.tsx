"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type ProposalView, type Stats } from "@/lib/api";

export default function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [proposals, setProposals] = useState<ProposalView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reasoning, setReasoning] = useState(false);

  useEffect(() => {
    Promise.all([api.stats(), api.proposals("open")])
      .then(([s, p]) => {
        setStats(s);
        setProposals(p);
      })
      .catch((e) => setError(String(e)));
  }, []);

  async function runReasoning() {
    setReasoning(true);
    try {
      const result = await api.triggerReasoning();
      const refreshed = await api.proposals("open");
      setProposals(refreshed);
      alert(
        `Reasoning OK — ${result.proposals_persisted} propostas. ` +
          `Custo $${result.cost_usd.toFixed(4)}.`,
      );
    } catch (e) {
      alert(`Reasoning falhou: ${e}`);
    } finally {
      setReasoning(false);
    }
  }

  if (error)
    return (
      <div className="rounded-lg border border-red-700 bg-red-950/30 p-4 text-red-200">
        Erro ao carregar dashboard: {error}
        <p className="mt-2 text-xs text-red-300/70">
          Confirme que o backend está em http://127.0.0.1:8000.
        </p>
      </div>
    );

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Dashboard</h1>
        <p className="text-slate-400 mt-1">
          Visão geral do grafo organizacional acumulado.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard label="Entidades" value={stats?.entities} />
        <StatCard label="Relações" value={stats?.relations} />
        <StatCard label="Eventos registrados" value={stats?.events} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <NavCard
          href="/graph"
          title="Grafo →"
          description="Visualize entidades e relações em react-flow. Filtre por tipo."
          accent="bg-cyan-500"
        />
        <NavCard
          href="/proposals"
          title="Propostas →"
          description="Lista de propostas geradas pelo módulo de raciocínio."
          accent="bg-emerald-500"
        />
        <button
          onClick={runReasoning}
          disabled={reasoning}
          className="text-left rounded-xl border border-slate-800 bg-slate-900/50 hover:bg-slate-900 p-5 transition disabled:opacity-50"
        >
          <div className="text-lg font-semibold">
            {reasoning ? "Raciocinando..." : "Disparar raciocínio"}
          </div>
          <p className="text-sm text-slate-400 mt-1">
            Roda os 6 detectores + Sonnet. ~30-60s. Custo ~$0.04.
          </p>
        </button>
      </div>

      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xl font-semibold">Propostas em aberto</h2>
          <Link
            href="/proposals"
            className="text-sm text-cyan-400 hover:text-cyan-300"
          >
            ver todas →
          </Link>
        </div>
        {proposals === null && (
          <div className="text-slate-500">Carregando...</div>
        )}
        {proposals && proposals.length === 0 && (
          <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 text-slate-400">
            Nenhuma proposta em aberto. Dispare o raciocínio acima para gerar.
          </div>
        )}
        {proposals && proposals.length > 0 && (
          <div className="space-y-2">
            {proposals.slice(0, 3).map((p) => (
              <ProposalRow key={p.id} proposal={p} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
}: {
  label: string;
  value: number | undefined;
}) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="text-sm text-slate-400">{label}</div>
      <div className="text-3xl font-bold mt-1">
        {value !== undefined ? value : "—"}
      </div>
    </div>
  );
}

function NavCard({
  href,
  title,
  description,
  accent,
}: {
  href: string;
  title: string;
  description: string;
  accent: string;
}) {
  return (
    <Link
      href={href}
      className="block rounded-xl border border-slate-800 bg-slate-900/50 hover:bg-slate-900 p-5 transition"
    >
      <div className="flex items-center gap-3">
        <div className={`w-2 h-2 rounded-full ${accent}`}></div>
        <div className="text-lg font-semibold">{title}</div>
      </div>
      <p className="text-sm text-slate-400 mt-2">{description}</p>
    </Link>
  );
}

function ProposalRow({ proposal }: { proposal: ProposalView }) {
  const typeColor =
    proposal.proposal_type === "alert"
      ? "bg-red-500/20 text-red-300 border-red-700"
      : proposal.proposal_type === "action"
        ? "bg-emerald-500/20 text-emerald-300 border-emerald-700"
        : "bg-blue-500/20 text-blue-300 border-blue-700";
  return (
    <Link
      href="/proposals"
      className="block rounded-lg border border-slate-800 bg-slate-900/40 p-4 hover:bg-slate-900 transition"
    >
      <div className="flex items-start gap-3">
        <div
          className={`px-2 py-0.5 rounded border text-xs font-mono ${typeColor}`}
        >
          {proposal.proposal_type} · P{proposal.priority}
        </div>
        <div className="flex-1">
          <div className="font-medium">{proposal.title}</div>
          <div className="text-sm text-slate-400 mt-1 line-clamp-2">
            {proposal.description}
          </div>
        </div>
      </div>
    </Link>
  );
}
