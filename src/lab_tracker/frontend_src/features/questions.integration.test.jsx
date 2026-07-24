import * as React from "react";

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";

import { App } from "../app-shell.jsx";

import { TOKEN_STORAGE_KEY } from "../shared/constants.js";

import { installFetchMock } from "../test/utils.js";

import {
  activeSessionsPath,
  apiResponse,
  committedAnalysesPath,
  datasetCountPath,
  datasetListPath,
  note,
  noteCountPath,
  paged,
  project,
  projectsPath,
  question,
  questionCountPath,
  questionListPath,
  questionRefactorsPath,
  recentNotesPath,
  stagedAnalysesPath,
  targetedQuestionNotesPath,
} from "../test/fixtures.js";

describe("App", () => {
  it("stages and activates a question from the home route", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-actions");

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Project One")]),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([]),
          paged([question({ questionId: "question-1", status: "staged", text: "How stable is the rig?" })]),
          paged([question({ questionId: "question-1", status: "active", text: "How stable is the rig?" })]),
        ],
      },
      {
        match: "/questions",
        method: "POST",
        response: apiResponse(
          question({ questionId: "question-1", status: "staged", text: "How stable is the rig?" }),
          201
        ),
      },
      {
        match: "/questions/question-1",
        method: "PATCH",
        response: apiResponse(
          question({ questionId: "question-1", status: "active", text: "How stable is the rig?" })
        ),
      },
      {
        match: datasetListPath("project-1"),
        response: [paged([]), paged([]), paged([])],
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
        ],
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Staging & Commit" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Active project")).toHaveValue("project-1");
    });

    fireEvent.change(screen.getByLabelText("Question text"), {
      target: { value: "How stable is the rig?" },
    });
    const stageButton = screen.getByRole("button", { name: "Stage question" });
    await waitFor(() => expect(stageButton).toBeEnabled());
    fireEvent.click(stageButton);

    expect(await screen.findByText("Question staged.")).toBeInTheDocument();
    expect(
      await screen.findByText("How stable is the rig?", { selector: ".item strong" })
    ).toBeInTheDocument();

    const activateButton = screen.getByRole("button", { name: "Commit (activate)" });
    await waitFor(() => expect(activateButton).toBeEnabled());
    fireEvent.click(activateButton);

    expect(await screen.findByText("Question activated.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Commit (activate)" })).not.toBeInTheDocument();
    });
  });

  it("stages a question under parents and renders the question map", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-hierarchy");

    const rootQuestion = question({
      questionId: "question-root",
      text: "How does odor timing shape navigation?",
    });
    const childQuestion = question({
      parentQuestionIds: ["question-root"],
      questionId: "question-child",
      text: "Which temporal odor features drive forward locomotion?",
    });
    const grandchildQuestion = question({
      parentQuestionIds: ["question-child"],
      questionId: "question-grandchild",
      status: "staged",
      text: "Which controls isolate forward locomotion?",
    });
    const multiParentQuestion = question({
      parentQuestionIds: ["question-root", "question-child"],
      questionId: "question-multi",
      text: "Which analysis controls rule out speed artifacts?",
    });

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "Temporal odor project")]),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([rootQuestion, childQuestion, grandchildQuestion, multiParentQuestion]),
          paged([
            rootQuestion,
            childQuestion,
            grandchildQuestion,
            multiParentQuestion,
            question({
              parentQuestionIds: ["question-root"],
              questionId: "question-new",
              status: "staged",
              text: "Which plume gaps change run length?",
            }),
          ]),
        ],
      },
      {
        match: "/questions",
        method: "POST",
        response: apiResponse(
          question({
            parentQuestionIds: ["question-root"],
            questionId: "question-new",
            status: "staged",
            text: "Which plume gaps change run length?",
          }),
          201
        ),
      },
      {
        match: datasetListPath("project-1"),
        response: [paged([]), paged([])],
      },
      {
        match: noteCountPath("project-1"),
        response: [
          paged([], { limit: 1, offset: 0, total: 0 }),
          paged([], { limit: 1, offset: 0, total: 0 }),
        ],
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    const questionMap = await screen.findByTestId("question-map");
    expect(
      within(questionMap).getAllByText("How does odor timing shape navigation?").length
    ).toBeGreaterThan(0);
    expect(
      within(questionMap).getAllByText(
        "Which temporal odor features drive forward locomotion?"
      ).length
    ).toBeGreaterThan(0);
    expect(
      within(questionMap).getAllByText("Which controls isolate forward locomotion?").length
    ).toBeGreaterThan(0);
    expect(within(questionMap).getAllByText("also linked elsewhere").length).toBeGreaterThan(0);

    const parentSelect = screen.getByLabelText("Parent questions");
    Array.from(parentSelect.options).forEach((option) => {
      option.selected = option.value === "question-root";
    });
    fireEvent.change(parentSelect);
    fireEvent.change(screen.getByLabelText("Question text"), {
      target: { value: "Which plume gaps change run length?" },
    });
    const stageButton = screen.getByRole("button", { name: "Stage question" });
    await waitFor(() => expect(stageButton).toBeEnabled());
    fireEvent.click(stageButton);

    expect(await screen.findByText("Question staged.")).toBeInTheDocument();
    const questionPost = fetchMock.mock.calls.find(
      ([url, init]) => url === "/questions" && init?.method === "POST"
    );
    expect(JSON.parse(questionPost[1].body)).toMatchObject({
      parent_question_ids: ["question-root"],
      text: "Which plume gaps change run length?",
    });
  });

  it("dims superseded questions in the map and keeps them out of active selectors", async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-supersession-map");

    const replacementQuestion = question({
      questionId: "question-current",
      status: "active",
      text: "Which ATF4 comparison is testable first?",
    });
    const supersededQuestion = question({
      questionId: "question-old",
      status: "superseded",
      supersededByQuestionId: "question-current",
      text: "Does lifecycle nuance explain the ATF4 phenotype?",
    });

    installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "ATF4 arbitration")]),
      },
      {
        match: questionListPath("project-1"),
        response: paged([replacementQuestion, supersededQuestion]),
      },
      {
        match: datasetListPath("project-1"),
        response: paged([]),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: recentNotesPath("project-1"),
        response: paged([]),
      },
      {
        match: activeSessionsPath("project-1"),
        response: paged([]),
      },
      {
        match: stagedAnalysesPath("project-1"),
        response: paged([]),
      },
      {
        match: committedAnalysesPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
    ]);

    render(<App />);

    const questionMap = await screen.findByTestId("question-map");
    const supersededNode = within(questionMap)
      .getByText("Does lifecycle nuance explain the ATF4 phenotype?")
      .closest(".question-node");
    expect(supersededNode).toHaveClass("question-node-superseded");
    expect(
      within(supersededNode).getByRole("button", {
        name: "superseded by Which ATF4 comparison is testable first?",
      })
    ).toBeInTheDocument();

    const activeSelectorLabels = ["Primary question", "Link to active question (optional)"];
    activeSelectorLabels.forEach((label) => {
      screen.getAllByLabelText(label).forEach((select) => {
        const optionValues = Array.from(select.options).map((option) => option.value);
        expect(optionValues).toContain("question-current");
        expect(optionValues).not.toContain("question-old");
      });
    });
  });

  it("submits a question refactor with selected child and note moves", async () => {
    const sourceId = "11111111-1111-4111-8111-111111111111";
    const replacementId = "22222222-2222-4222-8222-222222222222";
    const childId = "33333333-3333-4333-8333-333333333333";
    const noteId = "44444444-4444-4444-8444-444444444444";
    const sourceQuestion = question({
      hypothesis: "The old wording is too broad.",
      questionId: sourceId,
      status: "active",
      text: "Does lifecycle nuance explain the ATF4 phenotype?",
    });
    const childQuestion = question({
      parentQuestionIds: [sourceId],
      questionId: childId,
      status: "staged",
      text: "Which assay should be prioritized?",
    });
    const replacementQuestion = question({
      hypothesis: "The refined contrast can be tested first.",
      questionId: replacementId,
      questionType: "hypothesis_driven",
      status: "active",
      supersedesQuestionId: sourceId,
      text: "Which ATF4 arbitration comparison is testable first?",
    });

    localStorage.setItem(TOKEN_STORAGE_KEY, "token-question-refactor");
    window.history.replaceState({}, "", `/app/questions/${sourceId}`);

    const fetchMock = installFetchMock([
      {
        match: "/auth/me",
        response: apiResponse({ role: "admin", username: "sam" }),
      },
      {
        match: projectsPath,
        response: apiResponse([project("project-1", "ATF4 arbitration")]),
      },
      {
        match: questionCountPath("project-1"),
        response: paged([sourceQuestion], { limit: 1, offset: 0, total: 2 }),
      },
      {
        match: datasetCountPath("project-1"),
        response: paged([], { limit: 1, offset: 0, total: 0 }),
      },
      {
        match: noteCountPath("project-1"),
        response: paged([note()], { limit: 1, offset: 0, total: 1 }),
      },
      {
        match: `/questions/${sourceId}`,
        response: apiResponse(sourceQuestion),
      },
      {
        match: questionListPath("project-1"),
        response: [
          paged([sourceQuestion, childQuestion]),
          paged([
            question({
              hypothesis: "The old wording is too broad.",
              questionId: sourceId,
              status: "superseded",
              supersededByQuestionId: replacementId,
              text: "Does lifecycle nuance explain the ATF4 phenotype?",
            }),
            replacementQuestion,
            question({
              parentQuestionIds: [replacementId],
              questionId: childId,
              status: "staged",
              text: "Which assay should be prioritized?",
            }),
          ]),
        ],
      },
      {
        match: targetedQuestionNotesPath("project-1", sourceId),
        response: paged([
          note({
            noteId,
            rawContent: "Old wording note to move.",
            targets: [{ entity_id: sourceId, entity_type: "question" }],
            transcribedText: "",
          }),
        ]),
      },
      {
        match: questionRefactorsPath(sourceId),
        response: paged([]),
      },
      {
        match: `/questions/${sourceId}/refactor`,
        method: "POST",
        response: apiResponse(
          {
            refactor: {
              created_at: "2026-04-20T02:00:00Z",
              created_by: "sam",
              project_id: "project-1",
              reason: "Make the project framing testable.",
              refactor_id: "refactor-1",
              relationship_changes: {
                child_question_ids_reparented: [childId],
                dataset_session_analysis_claim_links_moved: false,
                note_ids_retargeted: [noteId],
              },
              replacement_question_id: replacementId,
              replacement_snapshot: {},
              source_question_id: sourceId,
              source_snapshot: {},
            },
            replacement_question: replacementQuestion,
            source_question: {
              ...sourceQuestion,
              status: "superseded",
              superseded_by_question_id: replacementId,
            },
          },
          201
        ),
      },
      {
        match: `/questions/${replacementId}`,
        response: apiResponse(replacementQuestion),
      },
      {
        match: targetedQuestionNotesPath("project-1", replacementId),
        response: paged([]),
      },
      {
        match: questionRefactorsPath(replacementId),
        response: paged([
          {
            created_at: "2026-04-20T02:00:00Z",
            created_by: "sam",
            project_id: "project-1",
            reason: "Make the project framing testable.",
            refactor_id: "refactor-1",
            relationship_changes: {},
            replacement_question_id: replacementId,
            replacement_snapshot: {},
            source_question_id: sourceId,
            source_snapshot: {},
          },
        ]),
      },
    ]);

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Question Detail" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Refactor question" }));
    // The refactor form mounts after the click's state update; gate on its first
    // field with a retrying query (findBy*) before the synchronous field edits,
    // or the whole form-fill races the render under parallel CI load.
    fireEvent.change(await screen.findByLabelText("Replacement question text"), {
      target: { value: "Which ATF4 arbitration comparison is testable first?" },
    });
    fireEvent.change(screen.getByLabelText("Replacement type"), {
      target: { value: "hypothesis_driven" },
    });
    fireEvent.change(screen.getByLabelText("Replacement status"), {
      target: { value: "active" },
    });
    fireEvent.change(screen.getByLabelText("Replacement hypothesis"), {
      target: { value: "The refined contrast can be tested first." },
    });
    fireEvent.change(screen.getByLabelText("Refactor reason"), {
      target: { value: "Make the project framing testable." },
    });
    fireEvent.click(screen.getByLabelText("Which assay should be prioritized?"));
    fireEvent.click(screen.getByLabelText("Old wording note to move."));
    fireEvent.click(screen.getByRole("button", { name: "Create replacement" }));

    expect(await screen.findByText("Question refactored.")).toBeInTheDocument();
    expect(
      (await screen.findAllByText("Which ATF4 arbitration comparison is testable first?")).length
    ).toBeGreaterThan(0);
    expect(await screen.findByText("Does lifecycle nuance explain the ATF4 phenotype?")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([url, init]) => url === `/questions/${sourceId}/refactor` && init?.method === "POST"
    );
    expect(JSON.parse(post[1].body)).toEqual({
      child_question_ids_to_reparent: [childId],
      note_ids_to_retarget: [noteId],
      reason: "Make the project framing testable.",
      replacement: {
        hypothesis: "The refined contrast can be tested first.",
        parent_question_ids: [],
        question_type: "hypothesis_driven",
        status: "active",
        text: "Which ATF4 arbitration comparison is testable first?",
      },
    });
  });
});
