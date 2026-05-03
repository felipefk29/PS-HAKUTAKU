"use client";

import { useEffect, useState } from "react";
import { api, type ProposalView } from "@/lib/api";

const STATUS_OPTIONS = ["open", "accepted", "dismissed", "resolved"] as const;
type Status = (typeof STATUS_OPTIONS)[number];

const TYPE_BADGE_CLASS: Record<ProposalView["proposal_type"], string> = {
  alert: "bg-red-500/15 text-red-300 border-red-700/60",
  action: "bg-emerald-500/15 text-emerald-300 border-emerald-700/60",
  suggestion: "bg-blue-500/15 text-blue-300 border-blue-700/60",
};

const PRIORITY_BADGE_CLASS: Record<number, string> = {
  5: "bg-red-600 text-white",
  4: "bg-orange-500 text-white",
  3: "bg-yellow-500 text-yellow-950",
  2: "bg-blue-500 text-white",
  1: "bg-slate-500 text-white",
};

export default function ProposalsPage() {
  const [proposals, setProposals] = useState<ProposalView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Status | "all">("all");

  function load(status: Status | "all" = filter) {
    api
      .proposals(status === "all" ? undefined : status)
      .then(setProposals)
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  async function changeStatus(id: string, newStatus: Status) {
    try {
      await api.updateProposalStatus(id, newStatus);
      load();
    } catch (e) {
      alert(`Falha ao atualizar: ${e}`);
    }
  }

  if (error)
    return (
      <div className="rounded-lg border border-red-700 bg-red-950/30 p-4 text-red-200">
        Erro: {error}
      </div>
    );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Propostas</h1>
        <p className="text-slate-400 mt-1 text-sm">
          Saídas do módulo de raciocínio. Cada proposta cita findings e entidades reais do grafo.
        </p>
      </div>

      <div className="flex gap-2 flex-wrap">
        <FilterButton
          active={filter === "all"}
          onClick={() => setFilter("all")}
        >
          todas
        </FilterButton>
        {STATUS_OPTIONS.map((s) => (
          <FilterButton
            key={s}
            active={filter === s}
            onClick={() => setFilter(s)}
          >
            {s}
          </FilterButton>
        ))}
      </div>

      {proposals === null && (
        <div className="text-slate-500">Carregando...</div>
      )}
      {proposals && proposals.length === 0 && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-slate-400">
          Nenhuma proposta com status <code>{filter}</code>.
        </div>
      )}
      {proposals && proposals.length > 0 && (
        <div className="space-y-3">
          {proposals.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              onStatusChange={changeStatus}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1 rounded-full border text-xs font-medium transition ${
        active
          ? "bg-cyan-500 text-white border-cyan-500"
          : "border-slate-700 text-slate-400 hover:border-slate-600 hover:text-slate-200"
      }`}
    >
      {children}
    </button>
  );
}

function ProposalCard({
  proposal,
  onStatusChange,
}: {
  proposal: ProposalView;
  onStatusChange: (id: string, status: Status) => void;
}) {
  const justification = proposal.justification as {
    based_on_findings?: string[];
    reasoning?: string;
    evidence_excerpts?: string[];
  };

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-5">
      <div className="flex items-start gap-3">
        <div
          className={`text-xs font-bold px-2 py-1 rounded ${PRIORITY_BADGE_CLASS[proposal.priority] || ""}`}
          title={`prioridade ${proposal.priority}`}
        >
          P{proposal.priority}
        </div>
        <div
          className={`text-xs uppercase tracking-wider px-2 py-1 rounded border ${TYPE_BADGE_CLASS[proposal.proposal_type]}`}
        >
          {proposal.proposal_type}
        </div>
        <div className="ml-auto text-xs text-slate-500 px-2 py-1 rounded bg-slate-800/60">
          {proposal.status}
        </div>
      </div>

      <div className="mt-3">
        <h3 className="text-lg font-semibold">{proposal.title}</h3>
        <p className="text-slate-300 text-sm mt-1 leading-relaxed">
          {proposal.description}
        </p>
      </div>

      {(justification.based_on_findings || justification.reasoning) && (
        <div className="mt-4 rounded-lg bg-slate-800/40 p-3 text-xs">
          {justification.based_on_findings && (
            <div className="mb-1">
              <span className="text-slate-500">findings:</span>{" "}
              {justification.based_on_findings.map((f) => (
                <code
                  key={f}
                  className="mx-1 px-1.5 py-0.5 rounded bg-slate-700 text-slate-300"
                >
                  {f}
                </code>
              ))}
            </div>
          )}
          {justification.reasoning && (
            <div className="text-slate-300 italic">
              &ldquo;{justification.reasoning}&rdquo;
            </div>
          )}
        </div>
      )}

      <div className="mt-4 flex items-center justify-between">
        <div className="text-xs text-slate-500">
          {proposal.related_entities.length} entidade(s) ·{" "}
          {new Date(proposal.created_at).toLocaleString()}
        </div>
        {proposal.status === "open" && (
          <div className="flex gap-2">
            <button
              onClick={() => onStatusChange(proposal.id, "accepted")}
              className="text-xs px-3 py-1 rounded border border-emerald-700 text-emerald-300 hover:bg-emerald-700/20 transition"
            >
              Aceitar
            </button>
            <button
              onClick={() => onStatusChange(proposal.id, "dismissed")}
              className="text-xs px-3 py-1 rounded border border-slate-700 text-slate-400 hover:bg-slate-700/40 transition"
            >
              Descartar
            </button>
            <button
              onClick={() => onStatusChange(proposal.id, "resolved")}
              className="text-xs px-3 py-1 rounded border border-cyan-700 text-cyan-300 hover:bg-cyan-700/20 transition"
            >
              Resolver
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
