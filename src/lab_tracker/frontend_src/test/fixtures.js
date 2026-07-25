// Shared test fixtures for the frontend: API path builders, entity
// factories, and response helpers. Imported by App.test.jsx (shell journeys)
// and by feature-scoped integration tests so route mocks stay identical and
// installFetchMock's unexpected-request strictness is preserved everywhere.
import { buildApiPath } from "../shared/api.js";
import { apiResponse as rawApiResponse } from "./utils.js";

const AUTH_USER_CREATED_AT = "2026-04-20T00:00:00Z";

// Full-App journeys use compact auth fixtures. Complete the fields FastAPI
// always serializes while gateway unit tests continue to exercise malformed
// auth payloads through the raw response helper.
function apiResponse(data, status = 200, meta = undefined) {
  if (
    data &&
    typeof data === "object" &&
    !Array.isArray(data) &&
    "role" in data &&
    "username" in data
  ) {
    return rawApiResponse(
      {
        created_at: AUTH_USER_CREATED_AT,
        user_id: "user-1",
        ...data,
      },
      status,
      meta ?? { auth_enabled: true }
    );
  }
  if (
    data &&
    typeof data === "object" &&
    !Array.isArray(data) &&
    "access_token" in data &&
    data.user
  ) {
    return rawApiResponse(
      {
        ...data,
        user: {
          created_at: AUTH_USER_CREATED_AT,
          user_id: "user-1",
          ...data.user,
        },
      },
      status,
      meta
    );
  }
  return rawApiResponse(data, status, meta);
}

const projectsPath = buildApiPath("/projects", { limit: 200, offset: 0 });

function questionListPath(projectId, { limit = 200, offset = 0, ...rest } = {}) {
  return buildApiPath("/questions", {
    project_id: projectId,
    ...rest,
    limit,
    offset,
  });
}

function questionCountPath(projectId) {
  return buildApiPath("/questions", { project_id: projectId, limit: 1, offset: 0 });
}

function datasetListPath(projectId, { limit = 200, offset = 0, ...rest } = {}) {
  return buildApiPath("/datasets/summaries", {
    project_id: projectId,
    ...rest,
    limit,
    offset,
  });
}

function datasetCountPath(projectId) {
  return buildApiPath("/datasets/summaries", {
    project_id: projectId,
    limit: 1,
    offset: 0,
  });
}

function noteCountPath(projectId) {
  return buildApiPath("/notes", { project_id: projectId, limit: 1, offset: 0 });
}

function recentNotesPath(projectId) {
  return buildApiPath("/notes", { limit: 5, offset: 0, project_id: projectId });
}

function targetedQuestionNotesPath(projectId, questionId) {
  return buildApiPath("/notes", {
    project_id: projectId,
    target_entity_type: "question",
    target_entity_id: questionId,
    limit: 200,
    offset: 0,
  });
}

function questionRefactorsPath(questionId) {
  return buildApiPath(`/questions/${questionId}/refactors`, { limit: 50, offset: 0 });
}

function activeSessionsPath(projectId) {
  return buildApiPath("/sessions", {
    project_id: projectId,
    status: "active",
    limit: 200,
    offset: 0,
  });
}

function stagedAnalysesPath(projectId) {
  return buildApiPath("/analyses", {
    project_id: projectId,
    status: "staged",
    limit: 200,
    offset: 0,
  });
}

function committedAnalysesPath(projectId) {
  return buildApiPath("/analyses", {
    project_id: projectId,
    status: "committed",
    limit: 5,
    recent_first: true,
  });
}

function captureAnalysesPath(projectId) {
  return buildApiPath("/analyses", { project_id: projectId, limit: 50 });
}

function captureClaimsPath(projectId) {
  return buildApiPath("/claims", { project_id: projectId, limit: 50 });
}

function datasetFilesPath(datasetId) {
  return buildApiPath(`/datasets/${datasetId}/files`, { limit: 100, offset: 0 });
}

function visualizationsPath(analysisId) {
  return buildApiPath("/visualizations", {
    analysis_id: analysisId,
    limit: 200,
    offset: 0,
  });
}

function projectMembersPath(projectId) {
  return buildApiPath(`/projects/${projectId}/members`, { limit: 200 });
}

function projectGraphPath(projectId, view = "evidence") {
  return buildApiPath(`/projects/${projectId}/graph`, { view });
}

function projectGraphMermaidPath(projectId, view = "evidence") {
  return buildApiPath(`/projects/${projectId}/graph/mermaid`, { view });
}

function paged(data, { limit = 200, offset = 0, total = data.length } = {}) {
  return apiResponse(data, 200, { limit, offset, total });
}

function project(projectId, name) {
  return { name, project_id: projectId };
}

function question({
  createdAt = "2026-04-20T00:00:00Z",
  hypothesis = null,
  parentQuestionIds = [],
  projectId = "project-1",
  questionType = "descriptive",
  questionId = "question-1",
  status = "active",
  supersededByQuestionId = null,
  supersedesQuestionId = null,
  text = "Question",
  updatedAt = "2026-04-20T01:00:00Z",
} = {}) {
  return {
    created_at: createdAt,
    hypothesis,
    parent_question_ids: parentQuestionIds,
    project_id: projectId,
    question_id: questionId,
    question_type: questionType,
    status,
    superseded_by_question_id: supersededByQuestionId,
    supersedes_question_id: supersedesQuestionId,
    text,
    updated_at: updatedAt,
  };
}

function dataset({
  commitHash = "commit-1",
  createdAt = "2026-04-20T00:00:00Z",
  datasetId = "dataset-1",
  primaryQuestionId = "question-1",
  projectId = "project-1",
  questionLinks = null,
  status = "staged",
  updatedAt = "2026-04-20T01:00:00Z",
} = {}) {
  return {
    commit_hash: commitHash,
    created_at: createdAt,
    dataset_id: datasetId,
    primary_question_id: primaryQuestionId,
    project_id: projectId,
    question_links:
      questionLinks || [{ outcome_status: "unknown", question_id: primaryQuestionId, role: "primary" }],
    status,
    updated_at: updatedAt,
  };
}

function note({
  createdAt = "2026-04-20T00:00:00Z",
  metadata = {},
  noteId = "note-1",
  projectId = "project-1",
  rawAsset = null,
  rawContent = "",
  status = "staged",
  targets = [],
  transcribedText = "Captured note",
} = {}) {
  return {
    created_at: createdAt,
    metadata,
    note_id: noteId,
    project_id: projectId,
    raw_asset: rawAsset,
    raw_content: rawContent,
    status,
    targets,
    transcribed_text: transcribedText,
    updated_at: createdAt,
  };
}

function session({
  linkCode = "ABC123",
  primaryQuestionId = "question-1",
  projectId = "project-1",
  sessionId = "session-1",
  sessionType = "scientific",
  startedAt = "2026-04-20T03:00:00Z",
  status = "active",
} = {}) {
  return {
    link_code: linkCode,
    primary_question_id: primaryQuestionId,
    project_id: projectId,
    session_id: sessionId,
    session_type: sessionType,
    started_at: startedAt,
    status,
  };
}

function analysis({
  analysisId = "analysis-1",
  codeVersion = "sha-1",
  createdAt = "2026-04-20T00:00:00Z",
  datasetIds = ["dataset-1"],
  environmentHash = null,
  executedAt = "2026-04-20T02:00:00Z",
  methodHash = "method-1",
  projectId = "project-1",
  status = "staged",
  updatedAt = "2026-04-20T02:00:00Z",
} = {}) {
  return {
    analysis_id: analysisId,
    code_version: codeVersion,
    created_at: createdAt,
    dataset_ids: datasetIds,
    environment_hash: environmentHash,
    executed_at: executedAt,
    method_hash: methodHash,
    project_id: projectId,
    status,
    updated_at: updatedAt,
  };
}

function claim({
  claimId = "claim-1",
  confidence = 62,
  createdAt = "2026-04-20T02:00:00Z",
  projectId = "project-1",
  statement = "Turning appears stronger after pulse onset.",
  status = "proposed",
} = {}) {
  return {
    claim_id: claimId,
    confidence,
    created_at: createdAt,
    project_id: projectId,
    statement,
    status,
    supported_by_analysis_ids: [],
    supported_by_dataset_ids: [],
    updated_at: createdAt,
  };
}

function visualization({
  analysisId = "analysis-1",
  createdAt = "2026-04-20T02:00:00Z",
  filePath = "viz/output.png",
  vizId = "viz-1",
  vizType = "timeseries",
} = {}) {
  return {
    analysis_id: analysisId,
    created_at: createdAt,
    file_path: filePath,
    viz_id: vizId,
    viz_type: vizType,
  };
}

function projectGraph({
  edges = [],
  nodes = [],
  projectId = "project-1",
  view = "evidence",
} = {}) {
  return {
    edges,
    nodes,
    project_id: projectId,
    view,
  };
}

function graphNode({
  detail = null,
  entityId,
  entityType,
  label,
  route = null,
  status = null,
} = {}) {
  return {
    detail,
    entity_id: entityId,
    entity_type: entityType,
    id: `${entityType}:${entityId}`,
    label,
    metadata: {},
    route,
    status,
  };
}

function graphEdge({ label, relationship, source, target } = {}) {
  return {
    id: `${relationship}:${source}->${target}`,
    label,
    relationship,
    source,
    target,
  };
}

function requestedUrls(fetchMock) {
  return fetchMock.mock.calls.map(([input]) => (typeof input === "string" ? input : input.url));
}

export {
  activeSessionsPath,
  analysis,
  apiResponse,
  captureAnalysesPath,
  captureClaimsPath,
  claim,
  committedAnalysesPath,
  dataset,
  datasetCountPath,
  datasetFilesPath,
  datasetListPath,
  graphEdge,
  graphNode,
  note,
  noteCountPath,
  paged,
  project,
  projectGraph,
  projectGraphMermaidPath,
  projectGraphPath,
  projectMembersPath,
  projectsPath,
  question,
  questionCountPath,
  questionListPath,
  questionRefactorsPath,
  recentNotesPath,
  requestedUrls,
  session,
  stagedAnalysesPath,
  targetedQuestionNotesPath,
  visualization,
  visualizationsPath,
};
