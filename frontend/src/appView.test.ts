import { describe, expect, it } from "vitest";
import { chooseMainView } from "./appView";
import { jobProgressFraction, type JobStatus } from "./jobs";
import type { GraphData, GraphNode } from "./Graph";

const node = (i: number): GraphNode => ({ id: `a${i}`, kind: "address", label: `A${i}` });
const graph = (n: number): GraphData => ({ nodes: Array.from({ length: n }, (_, i) => node(i)), edges: [] });

describe("chooseMainView — a view-load failure is the ONLY thing that blanks the graph (P29/UX-08)", () => {
  it("a view-load error selects the full-screen error branch (even if stale data is present)", () => {
    expect(chooseMainView("HTTP 503", null)).toBe("error");
    expect(chooseMainView("HTTP 503", graph(3))).toBe("error");
  });
  it("no data yet and no view error -> loading", () => {
    expect(chooseMainView(null, null)).toBe("loading");
  });
  it("an ingested-but-empty case -> the empty state, not an error", () => {
    expect(chooseMainView(null, graph(0))).toBe("empty");
  });
  it("data with nodes -> the graph", () => {
    expect(chooseMainView(null, graph(2))).toBe("graph");
  });
  it("action errors are structurally excluded: they are not an input, so they can never select 'error'", () => {
    // The action-error channel (Toast) is separate state; chooseMainView only sees viewError + data, so a
    // transient action failure leaves whatever the graph was showing untouched (graph stays graph).
    expect(chooseMainView(null, graph(2))).toBe("graph");
    expect(chooseMainView(null, graph(0))).toBe("empty");
  });
});

const JOB = (over: Partial<JobStatus> = {}): JobStatus => ({
  id: "j1", kind: "valuation", state: "running", phase: "", requests: 0, valued: 0, total: 0,
  rate_limited: false, message: "", error: null, ...over,
});

describe("jobProgressFraction — determinate only when a total is known (P29/UX-08)", () => {
  it("valuation M of N -> the completion fraction", () => {
    expect(jobProgressFraction(JOB({ valued: 3, total: 12 }))).toBeCloseTo(0.25);
  });
  it("no total (ingest page-fetching) -> null so the bar renders indeterminate", () => {
    expect(jobProgressFraction(JOB({ kind: "ingest", requests: 5, total: 0 }))).toBeNull();
  });
  it("a null job -> null", () => {
    expect(jobProgressFraction(null)).toBeNull();
  });
  it("clamps a runaway count to 100%", () => {
    expect(jobProgressFraction(JOB({ valued: 20, total: 10 }))).toBe(1);
  });
});
