"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  type Edge,
  type Node,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  api,
  TYPE_COLORS,
  type GraphSnapshot,
} from "@/lib/api";

const ALL_TYPES = [
  "Person",
  "Project",
  "Client",
  "Task",
  "Decision",
  "Risk",
  "OpenQuestion",
  "Commitment",
  "Dependency",
];

export default function GraphPage() {
  const [snap, setSnap] = useState<GraphSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTypes, setActiveTypes] = useState<Set<string>>(
    new Set(ALL_TYPES),
  );

  useEffect(() => {
    api
      .graph()
      .then(setSnap)
      .catch((e) => setError(String(e)));
  }, []);

  const toggle = useCallback((t: string) => {
    setActiveTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }, []);

  const { nodes, edges } = useMemo(() => {
    if (!snap) return { nodes: [], edges: [] };
    const visibleEntityIds = new Set(
      snap.entities.filter((e) => activeTypes.has(e.type)).map((e) => e.id),
    );

    // Layout em grade simples agrupando por tipo (visual decente sem física).
    const byType: Record<string, typeof snap.entities> = {};
    snap.entities.forEach((e) => {
      if (!visibleEntityIds.has(e.id)) return;
      (byType[e.type] = byType[e.type] || []).push(e);
    });
    const types = Object.keys(byType).sort();
    const colW = 260;
    const rowH = 90;
    const nodes: Node[] = [];
    types.forEach((t, ti) => {
      byType[t].forEach((e, ei) => {
        nodes.push({
          id: e.id,
          position: { x: ti * colW, y: ei * rowH },
          data: {
            label: (
              <div className="text-xs">
                <div className="font-bold text-[11px] uppercase tracking-wider opacity-70">
                  {e.type}
                </div>
                <div className="font-medium mt-0.5">{e.canonical_name}</div>
                {e.aliases.length > 0 && (
                  <div className="text-[10px] opacity-60 mt-0.5 truncate max-w-[200px]">
                    {e.aliases.slice(0, 3).join(", ")}
                  </div>
                )}
              </div>
            ),
          },
          style: {
            background: TYPE_COLORS[e.type] || "#475569",
            color: "white",
            border: "1px solid rgba(255,255,255,0.2)",
            borderRadius: 8,
            padding: 8,
            width: 220,
            fontSize: 11,
          },
        });
      });
    });

    const edges: Edge[] = snap.relations
      .filter(
        (r) =>
          visibleEntityIds.has(r.from_entity) &&
          visibleEntityIds.has(r.to_entity),
      )
      .map((r) => ({
        id: r.id,
        source: r.from_entity,
        target: r.to_entity,
        label: r.relation_type,
        labelStyle: { fill: "#cbd5e1", fontSize: 10 },
        labelBgStyle: { fill: "#0f172a", fillOpacity: 0.85 },
        style: {
          stroke:
            r.relation_type === "answers"
              ? "#10b981"
              : "#64748b",
          strokeWidth: r.relation_type === "answers" ? 2 : 1,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color: "#94a3b8" },
        animated: r.relation_type === "answers",
      }));

    return { nodes, edges };
  }, [snap, activeTypes]);

  if (error)
    return (
      <div className="rounded-lg border border-red-700 bg-red-950/30 p-4 text-red-200">
        Erro: {error}
      </div>
    );
  if (!snap) return <div className="text-slate-500">Carregando grafo...</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Grafo</h1>
          <p className="text-slate-400 mt-1 text-sm">
            {snap.entities.length} entidades · {snap.relations.length}{" "}
            relações · gerado em {new Date(snap.generated_at).toLocaleString()}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {ALL_TYPES.map((t) => {
          const active = activeTypes.has(t);
          return (
            <button
              key={t}
              onClick={() => toggle(t)}
              className="px-3 py-1 rounded-full border text-xs font-medium transition"
              style={{
                background: active ? TYPE_COLORS[t] : "transparent",
                color: active ? "white" : "#94a3b8",
                borderColor: active ? TYPE_COLORS[t] : "#334155",
              }}
            >
              {t}
            </button>
          );
        })}
      </div>

      <div
        className="rounded-xl border border-slate-800 bg-slate-900/40"
        style={{ height: "70vh" }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          fitView
          minZoom={0.1}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
        >
          <Background color="#1e293b" gap={16} />
          <Controls
            style={{ background: "#0f172a", color: "white" }}
          />
        </ReactFlow>
      </div>

      <p className="text-xs text-slate-500">
        Arestas verdes animadas = relações <code>answers</code> (cross-source
        linking). Outras arestas em cinza.
      </p>
    </div>
  );
}
