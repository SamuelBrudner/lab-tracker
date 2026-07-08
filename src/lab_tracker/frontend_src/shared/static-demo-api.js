const DEMO_USER = {
  user_id: "00000000-0000-4000-8000-0000000000d0",
  username: "demo.viewer",
  role: "viewer",
};

const PROJECT_ID = "2a32290d-5977-4e1a-9639-23210d3d4f1e";
const QUESTION_GAIN_ID = "6e3bfd66-ce26-4a66-ab08-1b951bb79d56";
const QUESTION_LATERAL_ID = "fb3454e0-6319-40bb-864c-9de91d0b04f1";
const QUESTION_ODOR_ID = "16a213e4-0dd4-4871-92e3-b4c81bfaee3c";
const QUESTION_ORN_ID = "5bb54eec-4e66-4a53-a634-587820655fc3";
const QUESTION_SUPERSEDED_ID = "4e63dfc8-f6fa-4fbf-8eba-307e59db887a";
const SESSION_ID = "e5fc06ea-58a2-49f1-8400-adcf118863a6";
const DATASET_ID = "968fe3b8-55dd-4756-a840-839ac03c27f1";
const ANALYSIS_ID = "ea73a6c8-f33b-4ab6-b2b6-76ac39117f9b";
const CLAIM_ID = "3c722fef-c0ce-4f16-8fe1-1973a6767da6";
const VIZ_ID = "c18ed44e-19c5-45d5-83c8-8750af44f207";
const NOTE_ID = "13212142-db49-427e-b105-91044e931261";
const CREATED_BY = "00000000-0000-4000-8000-000000000001";

const PROJECTS = [
  {
    project_id: PROJECT_ID,
    group_id: null,
    name: "Antennal-lobe gain control",
    description: "How does the antennal lobe control PN gain?",
    status: "active",
    created_at: "2026-06-05T09:53:58.181415Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T09:53:58.181419Z",
  },
];

const QUESTIONS = [
  {
    question_id: QUESTION_LATERAL_ID,
    project_id: PROJECT_ID,
    text: "Does lateral inhibition normalize PN output?",
    question_type: "hypothesis_driven",
    hypothesis: null,
    status: "active",
    parent_question_ids: [QUESTION_GAIN_ID],
    superseded_by_question_id: null,
    supersedes_question_id: null,
    created_at: "2026-06-05T09:53:58.240301Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.213512Z",
  },
  {
    question_id: QUESTION_SUPERSEDED_ID,
    project_id: PROJECT_ID,
    text: "What is the relationship between ORN and PN coding?",
    question_type: "descriptive",
    hypothesis: "Local interneurons in the antennal lobe shape PN output.",
    status: "superseded",
    parent_question_ids: [],
    superseded_by_question_id: QUESTION_GAIN_ID,
    supersedes_question_id: null,
    created_at: "2026-06-05T10:10:03.191718Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:39:07.220009Z",
  },
  {
    question_id: QUESTION_GAIN_ID,
    project_id: PROJECT_ID,
    text: "How does the antennal lobe control gain?",
    question_type: "descriptive",
    hypothesis: "The antennal lobe implements gain control over PN output.",
    status: "active",
    parent_question_ids: [],
    superseded_by_question_id: null,
    supersedes_question_id: QUESTION_SUPERSEDED_ID,
    created_at: "2026-06-05T10:10:03.198862Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.198866Z",
  },
  {
    question_id: QUESTION_ODOR_ID,
    project_id: PROJECT_ID,
    text: "Does normalization depend on odor identity?",
    question_type: "descriptive",
    hypothesis: "Divisive normalization strength varies across odor identities.",
    status: "active",
    parent_question_ids: [QUESTION_GAIN_ID],
    superseded_by_question_id: null,
    supersedes_question_id: null,
    created_at: "2026-06-05T10:10:03.206959Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.206964Z",
  },
  {
    question_id: QUESTION_ORN_ID,
    project_id: PROJECT_ID,
    text: "Is the suppression presynaptic on ORNs?",
    question_type: "descriptive",
    hypothesis: "Gain suppression acts presynaptically at ORN terminals.",
    status: "active",
    parent_question_ids: [QUESTION_GAIN_ID],
    superseded_by_question_id: null,
    supersedes_question_id: null,
    created_at: "2026-06-05T10:10:03.218132Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:24:22.799232Z",
  },
];

const SESSIONS = [
  {
    session_id: SESSION_ID,
    project_id: PROJECT_ID,
    session_type: "scientific",
    status: "closed",
    primary_question_id: QUESTION_LATERAL_ID,
    started_at: "2026-06-05T10:10:03.222506Z",
    ended_at: "2026-06-05T10:10:03.227375Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.227381Z",
    link_code: "4X6AN2SYUJE7DBAAVXHRDCDDUY",
  },
];

const DATASETS = [
  {
    dataset_id: DATASET_ID,
    project_id: PROJECT_ID,
    commit_hash: "f2eabaa157282d2a3613b495d117528457a303da5de6429bf48faf776f724ce1",
    primary_question_id: QUESTION_LATERAL_ID,
    question_links: [
      { question_id: QUESTION_ORN_ID, role: "secondary", outcome_status: "unknown" },
      { question_id: QUESTION_LATERAL_ID, role: "primary", outcome_status: "unknown" },
    ],
    commit_manifest: {
      files: [
        {
          file_id: null,
          path: "2025_12_10_Rig2_session001.nwb",
          checksum: "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
          size_bytes: null,
        },
      ],
      external_artifacts: [],
      metadata: {
        dataset_name: "2025_12_10_Rig2_session001.nwb",
        rig: "Rig2",
        subject: "fly07",
        date: "2025-12-10",
        acquisition_session_id: SESSION_ID,
      },
      nwb_metadata: {
        session_description: "PN whole-cell, background-odor gain protocol",
      },
      bids_metadata: {},
      note_ids: [],
      question_links: [
        { question_id: QUESTION_ORN_ID, role: "secondary", outcome_status: "unknown" },
        { question_id: QUESTION_LATERAL_ID, role: "primary", outcome_status: "unknown" },
      ],
      source_session_id: null,
    },
    status: "committed",
    created_at: "2026-06-05T10:10:03.232096Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.232100Z",
  },
];

const ANALYSES = [
  {
    analysis_id: ANALYSIS_ID,
    project_id: PROJECT_ID,
    dataset_ids: [DATASET_ID],
    method_hash: "sha256:divisive-norm-fit-pn-dose-response-v1",
    code_version: "divisive-norm fit (PN dose-response) @ git:9f8e7d6",
    environment_hash: "conda:lock-fly-olfaction-2025-12",
    executed_by: CREATED_BY,
    executed_by_user_id: null,
    executed_at: "2026-06-05T10:10:03.238983Z",
    status: "committed",
    created_at: "2026-06-05T10:10:03.238986Z",
    updated_at: "2026-06-05T10:10:03.238987Z",
  },
];

const CLAIMS = [
  {
    claim_id: CLAIM_ID,
    project_id: PROJECT_ID,
    statement: "Background odor scales PN gain ~0.6x (n=18)",
    confidence: 80.0,
    status: "supported",
    supported_by_dataset_ids: [DATASET_ID],
    supported_by_analysis_ids: [ANALYSIS_ID],
    answers_question_ids: [QUESTION_LATERAL_ID],
    created_at: "2026-06-05T10:10:03.245906Z",
    updated_at: "2026-06-05T10:10:03.245910Z",
  },
];

const VISUALIZATIONS = [
  {
    viz_id: VIZ_ID,
    analysis_id: ANALYSIS_ID,
    viz_type: "line_plot",
    file_path: "figures/fig_3b_pn_gain_vs_background.png",
    caption: "Fig 3b - PN gain vs background",
    related_claim_ids: [CLAIM_ID],
    asset: null,
    created_at: "2026-06-05T10:10:03.253702Z",
    updated_at: "2026-06-05T10:10:03.253705Z",
    asset_download_path: null,
  },
];

const NOTES = [
  {
    note_id: NOTE_ID,
    project_id: PROJECT_ID,
    raw_content: "bench note: fly07, 2 odor blocks tossed",
    raw_asset: null,
    transcribed_text: null,
    targets: [
      { entity_type: "question", entity_id: QUESTION_LATERAL_ID },
      { entity_type: "session", entity_id: SESSION_ID },
    ],
    metadata: {
      rig: "Rig2",
      subject: "fly07",
      date: "2025-12-10",
    },
    status: "committed",
    created_at: "2026-06-05T10:10:03.259964Z",
    created_by: CREATED_BY,
    created_by_user_id: null,
    updated_at: "2026-06-05T10:10:03.259965Z",
  },
];

const PROJECT_MEMBERS = [
  {
    membership_id: "demo-membership-viewer",
    project_id: PROJECT_ID,
    user_id: DEMO_USER.user_id,
    username: DEMO_USER.username,
    role: "viewer",
    created_at: "2026-06-05T10:10:03.260000Z",
    updated_at: "2026-06-05T10:10:03.260000Z",
  },
];

const COLLECTIONS = {
  analyses: ANALYSES,
  claims: CLAIMS,
  datasets: DATASETS,
  notes: NOTES,
  projects: PROJECTS,
  questions: QUESTIONS,
  sessions: SESSIONS,
  visualizations: VISUALIZATIONS,
};

function isStaticDemoEnabled() {
  if (typeof globalThis === "undefined") {
    return false;
  }
  if (typeof globalThis.__LAB_TRACKER_STATIC_DEMO__ === "boolean") {
    return globalThis.__LAB_TRACKER_STATIC_DEMO__;
  }
  const location = globalThis.location;
  if (!location) {
    return false;
  }
  const search = new URLSearchParams(location.search || "");
  if (search.get("demo") === "1" || search.get("demo") === "true") {
    return true;
  }
  return (
    String(location.hostname || "").endsWith("github.io") &&
    String(location.pathname || "").includes("/lab-tracker/app")
  );
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function jsonResponse(payload, init = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status || 200,
    headers: {
      "content-type": "application/json",
      ...(init.headers || {}),
    },
  });
}

function textResponse(text, init = {}) {
  return new Response(text, {
    status: init.status || 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      ...(init.headers || {}),
    },
  });
}

function errorResponse(message, status = 404) {
  return jsonResponse(
    {
      error: {
        code: status === 403 ? "read_only_demo" : "not_found",
        message,
      },
    },
    { status }
  );
}

function listResponse(items, searchParams) {
  const limit = Number(searchParams.get("limit") || 50);
  const offset = Number(searchParams.get("offset") || 0);
  const data = items.slice(offset, offset + limit);
  return jsonResponse({
    data: clone(data),
    meta: {
      limit,
      offset,
      total: items.length,
    },
  });
}

function dataResponse(data, meta = null) {
  return jsonResponse({ data: clone(data), meta });
}

function byProject(items, projectId) {
  if (!projectId) {
    return items;
  }
  return items.filter((item) => item.project_id === projectId);
}

function filterCollection(name, searchParams) {
  let items = COLLECTIONS[name] || [];
  items = byProject(items, searchParams.get("project_id") || "");
  const status = searchParams.get("status") || "";
  if (status) {
    items = items.filter((item) => item.status === status);
  }
  if (name === "analyses") {
    const datasetId = searchParams.get("dataset_id") || "";
    if (datasetId) {
      items = items.filter((item) => (item.dataset_ids || []).includes(datasetId));
    }
  }
  if (name === "claims") {
    const datasetId = searchParams.get("dataset_id") || "";
    const analysisId = searchParams.get("analysis_id") || "";
    if (datasetId) {
      items = items.filter((item) => (item.supported_by_dataset_ids || []).includes(datasetId));
    }
    if (analysisId) {
      items = items.filter((item) => (item.supported_by_analysis_ids || []).includes(analysisId));
    }
  }
  if (name === "visualizations") {
    const analysisId = searchParams.get("analysis_id") || "";
    const claimId = searchParams.get("claim_id") || "";
    if (analysisId) {
      items = items.filter((item) => item.analysis_id === analysisId);
    }
    if (claimId) {
      items = items.filter((item) => (item.related_claim_ids || []).includes(claimId));
    }
    const projectId = searchParams.get("project_id") || "";
    if (projectId) {
      const analysisIds = new Set(
        ANALYSES.filter((analysis) => analysis.project_id === projectId).map(
          (analysis) => analysis.analysis_id
        )
      );
      items = items.filter((item) => analysisIds.has(item.analysis_id));
    }
  }
  if (name === "notes") {
    const targetType = searchParams.get("target_entity_type") || "";
    const targetId = searchParams.get("target_entity_id") || "";
    if (targetType && targetId) {
      items = items.filter((item) =>
        (item.targets || []).some(
          (target) => target.entity_type === targetType && target.entity_id === targetId
        )
      );
    }
  }
  return items;
}

// Mirror the backend _dataset_label precedence (see project_graph.py): prefer
// the manifest's human-readable name over the opaque commit hash.
function datasetLabel(dataset) {
  const manifest = dataset.commit_manifest || {};
  const name = ((manifest.metadata || {}).dataset_name || "").trim();
  if (name) return name;
  const files = manifest.files || [];
  if (files.length) {
    const base = String(files[0].path || "").replace(/\\/g, "/").split("/").pop().trim();
    if (base) return base;
  }
  return `Dataset ${dataset.commit_hash}`;
}

function graphNode(entityType, entity, label, detail, status, route, metadata = {}) {
  const entityId = entity[`${entityType}_id`] || entity.viz_id || entity.id;
  return {
    id: `${entityType}:${entityId}`,
    entity_type: entityType,
    entity_id: entityId,
    label,
    detail,
    status,
    route,
    metadata,
  };
}

function baseGraph() {
  const nodes = [
    graphNode(
      "question",
      QUESTIONS.find((item) => item.question_id === QUESTION_LATERAL_ID),
      "Does lateral inhibition normalize PN output?",
      "hypothesis_driven",
      "active",
      `/app/questions/${QUESTION_LATERAL_ID}`,
      { hypothesis: "", question_type: "hypothesis_driven" }
    ),
    graphNode(
      "question",
      QUESTIONS.find((item) => item.question_id === QUESTION_ODOR_ID),
      "Does normalization depend on odor identity?",
      "descriptive",
      "active",
      `/app/questions/${QUESTION_ODOR_ID}`,
      {
        hypothesis: "Divisive normalization strength varies across odor identities.",
        question_type: "descriptive",
      }
    ),
    graphNode(
      "question",
      QUESTIONS.find((item) => item.question_id === QUESTION_GAIN_ID),
      "How does the antennal lobe control gain?",
      "descriptive",
      "active",
      `/app/questions/${QUESTION_GAIN_ID}`,
      {
        hypothesis: "The antennal lobe implements gain control over PN output.",
        question_type: "descriptive",
      }
    ),
    graphNode(
      "question",
      QUESTIONS.find((item) => item.question_id === QUESTION_ORN_ID),
      "Is the suppression presynaptic on ORNs?",
      "descriptive",
      "active",
      `/app/questions/${QUESTION_ORN_ID}`,
      {
        hypothesis: "Gain suppression acts presynaptically at ORN terminals.",
        question_type: "descriptive",
      }
    ),
    graphNode(
      "question",
      QUESTIONS.find((item) => item.question_id === QUESTION_SUPERSEDED_ID),
      "What is the relationship between ORN and PN coding?",
      "descriptive",
      "superseded",
      `/app/questions/${QUESTION_SUPERSEDED_ID}`,
      {
        hypothesis: "Local interneurons in the antennal lobe shape PN output.",
        question_type: "descriptive",
      }
    ),
    graphNode(
      "session",
      SESSIONS[0],
      "scientific session",
      "4X6AN2SYUJE7DBAAVXHRDCDDUY",
      "closed",
      `/app/sessions/${SESSION_ID}`
    ),
    graphNode(
      "note",
      NOTES[0],
      "bench note: fly07, 2 odor blocks tossed",
      null,
      "committed",
      `/app/notes/${NOTE_ID}`
    ),
    graphNode(
      "dataset",
      DATASETS[0],
      datasetLabel(DATASETS[0]),
      DATASETS[0].commit_hash,
      "committed",
      `/app/datasets/${DATASET_ID}`
    ),
    graphNode(
      "analysis",
      ANALYSES[0],
      "Analysis divisive-norm fit (PN dose-response) @ git:9f8e7d6",
      ANALYSES[0].method_hash,
      "committed",
      null,
      { code_version: ANALYSES[0].code_version }
    ),
    graphNode(
      "claim",
      CLAIMS[0],
      "Background odor scales PN gain ~0.6x (n=18)",
      "confidence 80",
      "supported",
      null
    ),
    graphNode(
      "visualization",
      VISUALIZATIONS[0],
      "Fig 3b - PN gain vs background",
      "figures/fig_3b_pn_gain_vs_background.png",
      null,
      `/app/visualizations/${VIZ_ID}`,
      { viz_type: "line_plot" }
    ),
  ];

  const edges = [
    ["analysis_dataset", `dataset:${DATASET_ID}`, `analysis:${ANALYSIS_ID}`, "used by"],
    ["claim_analysis_support", `analysis:${ANALYSIS_ID}`, `claim:${CLAIM_ID}`, "supports"],
    ["claim_dataset_support", `dataset:${DATASET_ID}`, `claim:${CLAIM_ID}`, "supports"],
    ["visualization_analysis", `analysis:${ANALYSIS_ID}`, `visualization:${VIZ_ID}`, "generates"],
    ["visualization_claim", `claim:${CLAIM_ID}`, `visualization:${VIZ_ID}`, "related claim"],
    [
      "claim_question_answers",
      `claim:${CLAIM_ID}`,
      `question:${QUESTION_LATERAL_ID}`,
      "answers",
    ],
    ["note_target_question", `note:${NOTE_ID}`, `question:${QUESTION_LATERAL_ID}`, "targets"],
    ["note_target_session", `note:${NOTE_ID}`, `session:${SESSION_ID}`, "targets"],
    [
      "question_superseded_by",
      `question:${QUESTION_SUPERSEDED_ID}`,
      `question:${QUESTION_GAIN_ID}`,
      "superseded by",
    ],
    [
      "question_supersedes",
      `question:${QUESTION_GAIN_ID}`,
      `question:${QUESTION_SUPERSEDED_ID}`,
      "supersedes",
    ],
    [
      "question_parent",
      `question:${QUESTION_GAIN_ID}`,
      `question:${QUESTION_ODOR_ID}`,
      "parent",
    ],
    [
      "question_parent",
      `question:${QUESTION_GAIN_ID}`,
      `question:${QUESTION_ORN_ID}`,
      "parent",
    ],
    [
      "question_parent",
      `question:${QUESTION_GAIN_ID}`,
      `question:${QUESTION_LATERAL_ID}`,
      "parent",
    ],
    [
      "dataset_question_secondary",
      `question:${QUESTION_ORN_ID}`,
      `dataset:${DATASET_ID}`,
      "secondary question",
    ],
    [
      "dataset_question_primary",
      `question:${QUESTION_LATERAL_ID}`,
      `dataset:${DATASET_ID}`,
      "primary question",
    ],
    [
      "session_question",
      `question:${QUESTION_LATERAL_ID}`,
      `session:${SESSION_ID}`,
      "session question",
    ],
  ].map(([relationship, source, target, label]) => ({
    id: `${relationship}:${source}->${target}`,
    source,
    target,
    label,
    relationship,
  }));

  return { nodes, edges };
}

function graphForView(view) {
  const graph = baseGraph();
  const allowedByView = {
    evidence: new Set(["question", "dataset", "analysis", "claim", "visualization"]),
    questions: new Set(["question"]),
  };
  const allowed = allowedByView[view];
  if (!allowed) {
    return graph;
  }
  const nodes = graph.nodes.filter((node) => allowed.has(node.entity_type));
  const nodeIds = new Set(nodes.map((node) => node.id));
  return {
    nodes,
    edges: graph.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target)),
  };
}

function mermaidForView(view) {
  const graph = graphForView(view);
  const lines = ["graph LR"];
  graph.nodes.forEach((node) => {
    lines.push(`  ${node.id.replace(/[:.-]/g, "_")}[${JSON.stringify(node.label)}]`);
  });
  graph.edges.forEach((edge) => {
    lines.push(
      `  ${edge.source.replace(/[:.-]/g, "_")} -->|${edge.label}| ${edge.target.replace(
        /[:.-]/g,
        "_"
      )}`
    );
  });
  return `${lines.join("\n")}\n`;
}

function findDetail(pathname) {
  const parts = pathname.split("/").filter(Boolean);
  if (parts.length !== 2) {
    return null;
  }
  const [collection, id] = parts;
  const singular = collection.endsWith("s") ? collection.slice(0, -1) : collection;
  const key = `${singular}_id`;
  const items = COLLECTIONS[collection] || [];
  return items.find((item) => item[key] === id) || null;
}

function demoPayload(url) {
  const { pathname, searchParams } = url;
  if (pathname === "/auth/bootstrap-status") {
    return dataResponse({
      bootstrap_admin_configured: false,
      bootstrap_token: null,
      bootstrap_token_warning: null,
      first_admin_available: false,
      has_users: true,
      public_viewer_registration_enabled: false,
    });
  }
  if (pathname === "/auth/me") {
    return jsonResponse({
      data: DEMO_USER,
      meta: { auth_enabled: true, demo: true },
    });
  }
  if (pathname === "/health") {
    return jsonResponse({ status: "ok", demo: true });
  }
  if (pathname === "/readiness") {
    return jsonResponse({ status: "ready", demo: true });
  }
  if (pathname === `/projects/${PROJECT_ID}/members`) {
    return listResponse(PROJECT_MEMBERS, searchParams);
  }
  if (pathname === `/projects/${PROJECT_ID}/graph`) {
    const view = searchParams.get("view") || "evidence";
    return dataResponse({ project_id: PROJECT_ID, view, ...graphForView(view) });
  }
  if (pathname === `/projects/${PROJECT_ID}/graph/mermaid`) {
    return textResponse(mermaidForView(searchParams.get("view") || "evidence"));
  }
  if (pathname === `/projects/${PROJECT_ID}/graph-draft-batch-settings`) {
    return dataResponse({
      project_id: PROJECT_ID,
      enabled: false,
      cadence_minutes: 1440,
      run_at_local_time: "18:00",
      timezone_name: "America/New_York",
      next_run_at: null,
      updated_at: "2026-06-05T10:10:03.260000Z",
    });
  }
  if (pathname === "/batches" || pathname === "/batches/runs" || pathname === "/graph-drafts") {
    return listResponse([], searchParams);
  }
  if (pathname === "/auth/tokens" || pathname === "/auth/devices") {
    return listResponse([], searchParams);
  }
  if (pathname === `/questions/${QUESTION_LATERAL_ID}/refactors`) {
    return listResponse([], searchParams);
  }
  if (pathname === `/sessions/${SESSION_ID}/outputs`) {
    return listResponse([], searchParams);
  }
  if (pathname === `/datasets/${DATASET_ID}/files`) {
    return listResponse(DATASETS[0].commit_manifest.files, searchParams);
  }
  if (pathname === `/notes/${NOTE_ID}/raw`) {
    return dataResponse({
      content_base64: btoa(NOTES[0].raw_content),
      content_type: "text/plain",
      filename: "bench-note.txt",
    });
  }
  for (const collection of Object.keys(COLLECTIONS)) {
    if (pathname === `/${collection}`) {
      return listResponse(filterCollection(collection, searchParams), searchParams);
    }
  }
  const detail = findDetail(pathname);
  if (detail) {
    return dataResponse(detail);
  }
  return errorResponse("Static demo record not found.", 404);
}

async function demoFetch(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    return errorResponse("The hosted demo is read-only.", 403);
  }
  const url = new URL(path, "https://lab-tracker-demo.local");
  return demoPayload(url);
}

export { demoFetch, isStaticDemoEnabled };
