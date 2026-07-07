// EDITH memory-graph viewer. Fetches /graph, renders a dark force-directed
// cloud with force-graph (vendored UMD). No build step, offline at runtime.

// Muted palette — subtle type encoding against the near-black background.
const TYPE_COLOR = {
  Project: "#8fb6d9",
  Repo: "#9fd6b0",
  PR: "#d9c07f",
  Person: "#d99f9f",
  Fact: "#b7a9d6",
  Owner: "#d9b98f",
};
const DEFAULT_COLOR = "#c9ccd1";
const LINK_COLOR = "rgba(180, 184, 190, 0.18)";

function colorFor(type) {
  return TYPE_COLOR[type] || DEFAULT_COLOR;
}

// Node radius scales gently with degree so hubs read as larger.
function radiusFor(node) {
  return 2.5 + Math.sqrt(node.degree || 0) * 1.6;
}

const elGraph = document.getElementById("graph");

const Graph = ForceGraph()(elGraph)
  .backgroundColor("#111214")
  .nodeRelSize(1)
  .nodeVal((n) => radiusFor(n))
  .nodeColor((n) => colorFor(n.type))
  .nodeLabel((n) => `${n.type}: ${n.label}`)
  .linkColor(() => LINK_COLOR)
  .linkWidth(0.5)
  .warmupTicks(40)
  .cooldownTicks(120)
  .onNodeClick((n) => showPanel(n));

// Slightly stronger repulsion for a dense-but-legible cloud.
Graph.d3Force("charge").strength(-40);

fetch("/graph")
  .then((r) => r.json())
  .then((data) => {
    Graph.graphData(data);
    buildLegend(data.nodes);
    setTimeout(() => Graph.zoomToFit(600, 40), 400);
  });

// --- Details panel ---
const panel = document.getElementById("panel");
const RESERVED = new Set(["id", "type", "label", "degree", "x", "y", "vx", "vy", "index", "__indexColor"]);

function showPanel(node) {
  document.getElementById("panel-label").textContent = node.label || node.id;
  document.getElementById("panel-type").textContent = node.type;
  document.getElementById("panel-id").textContent = node.id;
  const dl = document.getElementById("panel-props");
  dl.innerHTML = "";
  Object.keys(node)
    .filter((k) => !RESERVED.has(k) && node[k] !== null && node[k] !== undefined)
    .forEach((k) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = String(node[k]);
      dl.append(dt, dd);
    });
  panel.classList.remove("hidden");
}

document.getElementById("panel-close").addEventListener("click", () => {
  panel.classList.add("hidden");
});

// --- Legend (only types actually present) ---
function buildLegend(nodes) {
  const present = [...new Set(nodes.map((n) => n.type))].sort();
  const legend = document.getElementById("legend");
  legend.innerHTML = "";
  present.forEach((type) => {
    const span = document.createElement("span");
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = colorFor(type);
    span.append(sw, document.createTextNode(type));
    legend.append(span);
  });
}

// --- Zoom control cluster ---
document.getElementById("zoom-in").addEventListener("click", () => {
  Graph.zoom(Graph.zoom() * 1.4, 250);
});
document.getElementById("zoom-out").addEventListener("click", () => {
  Graph.zoom(Graph.zoom() / 1.4, 250);
});
document.getElementById("zoom-reset").addEventListener("click", () => {
  Graph.centerAt(0, 0, 400);
  Graph.zoom(1, 400);
});
document.getElementById("zoom-fit").addEventListener("click", () => {
  Graph.zoomToFit(500, 40);
});

// Keep the canvas sized to the window.
window.addEventListener("resize", () => {
  Graph.width(elGraph.clientWidth).height(elGraph.clientHeight);
});
