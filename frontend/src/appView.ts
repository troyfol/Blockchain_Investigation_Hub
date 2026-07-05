import type { GraphData } from "./Graph";

// The main-canvas render decision (P29/UX-08), extracted as a PURE, DOM-free helper so it can be unit
// tested. The full-screen "Could not load view" branch is reserved for a genuine VIEW-LOAD failure
// (`viewError`); a transient ACTION failure is surfaced as a Toast instead and is NOT an input here, so
// by construction it can never select the error branch / blank the graph.

export type MainView = "error" | "loading" | "empty" | "graph";

export function chooseMainView(viewError: string | null, data: GraphData | null): MainView {
  if (viewError) return "error";
  if (!data) return "loading";
  if (data.nodes.length === 0) return "empty";
  return "graph";
}
