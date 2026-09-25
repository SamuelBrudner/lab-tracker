import * as React from "react";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { apiResponse, installFetchMock } from "../../test/utils.js";
import {
  note,
  paged,
  project,
  question,
  questionListPath,
  questionRefactorsPath,
} from "../../test/fixtures.js";
import { buildApiPath } from "../../shared/api.js";

import { QuestionDetailCard } from "./QuestionDetailCard.jsx";

const SOURCE_ID = "question-source";
const ADMIN = { role: "admin", user_id: "user-admin", username: "sam" };

function targetedNotesPath(offset) {
  return buildApiPath("/notes", {
    project_id: "project-1",
    target_entity_type: "question",
    target_entity_id: SOURCE_ID,
    limit: 200,
    offset,
  });
}

function baseRoutes({ questionPages, notePages, refactorResponse }) {
  return [
    {
      match: `/questions/${SOURCE_ID}`,
      response: apiResponse(question({ questionId: SOURCE_ID, text: "Source question" })),
    },
    { match: /\/projects\/project-1\/members/, response: apiResponse([]) },
    ...questionPages.map(({ offset, response }) => ({
      match: questionListPath("project-1", { offset }),
      response,
    })),
    ...notePages.map(({ offset, response }) => ({
      match: targetedNotesPath(offset),
      response,
    })),
    { match: questionRefactorsPath(SOURCE_ID), response: paged([], { limit: 50 }) },
    ...(refactorResponse
      ? [
          {
            match: `/questions/${SOURCE_ID}/refactor`,
            method: "POST",
            response: refactorResponse,
          },
        ]
      : []),
  ];
}

function renderCard(overrides = {}) {
  const props = {
    navigate: vi.fn(),
    onSetActiveProject: vi.fn(),
    projects: [project("project-1", "Project One")],
    questionId: SOURCE_ID,
    setBusy: vi.fn(),
    setFlash: vi.fn(),
    token: "token-question",
    user: ADMIN,
    ...overrides,
  };
  render(<QuestionDetailCard {...props} />);
  return props;
}

describe("QuestionDetailCard refactor form", () => {
  it("offers parents, children and notes beyond the first page", async () => {
    // The server clamps each page to two items (meta.limit), so the late child
    // and the late note only arrive by following pagination past offset 0.
    const firstQuestionPage = [
      question({ questionId: SOURCE_ID, text: "Source question" }),
      question({ questionId: "question-0", text: "Question 0" }),
    ];
    const lateChild = question({
      parentQuestionIds: [SOURCE_ID],
      questionId: "question-late-child",
      text: "Late child question",
    });
    const firstNotePage = [
      note({ noteId: "note-0", transcribedText: "Note 0" }),
      note({ noteId: "note-1", transcribedText: "Note 1" }),
    ];
    const fetchMock = installFetchMock(
      baseRoutes({
        questionPages: [
          { offset: 0, response: paged(firstQuestionPage, { limit: 2, total: 3 }) },
          { offset: 2, response: paged([lateChild], { limit: 2, offset: 2, total: 3 }) },
        ],
        notePages: [
          { offset: 0, response: paged(firstNotePage, { limit: 2, total: 3 }) },
          {
            offset: 2,
            response: paged([note({ noteId: "note-late", transcribedText: "Late note" })], {
              limit: 2,
              offset: 2,
              total: 3,
            }),
          },
        ],
      })
    );

    renderCard();

    fireEvent.click(await screen.findByRole("button", { name: "Refactor question" }));

    expect(await screen.findByLabelText("Late child question")).toBeInTheDocument();
    expect(await screen.findByLabelText("Late note")).toBeInTheDocument();
    expect(
      Array.from(screen.getByLabelText("Replacement parents").options).map((option) => option.value)
    ).toContain("question-late-child");
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain(
      questionListPath("project-1", { offset: 2 })
    );
  });

  it("sends one refactor request when the submit button is clicked twice", async () => {
    let resolveRefactor;
    const pendingRefactor = new Promise((resolve) => {
      resolveRefactor = resolve;
    });
    const fetchMock = installFetchMock(
      baseRoutes({
        questionPages: [
          {
            offset: 0,
            response: paged([question({ questionId: SOURCE_ID, text: "Source question" })]),
          },
        ],
        notePages: [{ offset: 0, response: paged([]) }],
        refactorResponse: () => pendingRefactor,
      })
    );

    const props = renderCard();

    fireEvent.click(await screen.findByRole("button", { name: "Refactor question" }));
    fireEvent.change(await screen.findByLabelText("Refactor reason"), {
      target: { value: "Sharper framing." },
    });
    const submit = screen.getByRole("button", { name: "Create replacement" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(submit).toBeDisabled());
    const refactorPosts = () =>
      fetchMock.mock.calls.filter(
        ([url, init]) => url === `/questions/${SOURCE_ID}/refactor` && init?.method === "POST"
      );
    expect(refactorPosts()).toHaveLength(1);

    resolveRefactor(
      apiResponse(
        {
          replacement_question: question({ questionId: "question-replacement" }),
          source_question: question({ questionId: SOURCE_ID, status: "superseded" }),
        },
        201
      )
    );
    await waitFor(() =>
      expect(props.navigate).toHaveBeenCalledWith("/app/questions/question-replacement")
    );
    expect(refactorPosts()).toHaveLength(1);
  });
});

describe("QuestionDetailCard Back", () => {
  function renderWithDepth(depth) {
    window.history.replaceState(
      depth ? { labTracker: { depth } } : null,
      "",
      `/app/questions/${SOURCE_ID}`
    );
    installFetchMock(
      baseRoutes({
        questionPages: [
          {
            offset: 0,
            response: paged([question({ questionId: SOURCE_ID, text: "Source question" })]),
          },
        ],
        notePages: [{ offset: 0, response: paged([]) }],
      })
    );
    return renderCard();
  }

  it("Back uses in-app history with /app fallback", async () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => {});

    const direct = renderWithDepth(0);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(direct.navigate).toHaveBeenCalledWith("/app");
    expect(back).not.toHaveBeenCalled();
    cleanup();

    const fromApp = renderWithDepth(1);
    fireEvent.click(await screen.findByRole("button", { name: "Back" }));
    expect(back).toHaveBeenCalledTimes(1);
    expect(fromApp.navigate).not.toHaveBeenCalled();
  });
});
