import { describe, expect, it } from "vitest";

import { appPathLabel, captureSetupTips } from "./capture-setup.js";

const NOTE_A = "11111111-1111-4111-8111-111111111111";
const NOTE_B = "22222222-2222-4222-8222-222222222222";
const SESSION = "33333333-3333-4333-8333-333333333333";

function recommendation(overrides = {}) {
  return {
    recommendation_id: "shortcut_no_active_session",
    kind: "shortcut_session",
    gap: "shortcut_no_active_session",
    detected: true,
    note_ids: [NOTE_A, NOTE_B],
    note_count: 2,
    session_id: null,
    session_label: null,
    explanation: "Two memos named no session.",
    explanation_source: "model",
    guide: {
      title: "Have a session open when you dictate",
      steps: ["Start a session first.", "Then dictate."],
      app_path: "/app",
      command: null,
      doc: "docs/bench-capture.md#hands-free-voice-shortcut",
    },
    ...overrides,
  };
}

function draftWith(capture_setup) {
  return { context_packet: { capture_setup } };
}

function packet(recommendations) {
  return draftWith({
    version: "capture_setup/v1",
    offered: ["shortcut_no_active_session"],
    returned: recommendations.length,
    dropped: 0,
    recommendations,
  });
}

describe("captureSetupTips", () => {
  it("reads each recorded tip into what the page shows", () => {
    expect(captureSetupTips(packet([recommendation()]))).toEqual([
      {
        id: "shortcut_no_active_session",
        title: "Have a session open when you dictate",
        sessionLabel: "",
        explanation: "Two memos named no session.",
        explanationSource: "model",
        noteIds: [NOTE_A, NOTE_B],
        steps: ["Start a session first.", "Then dictate."],
        command: "",
        appPath: "/app",
        doc: "docs/bench-capture.md#hands-free-voice-shortcut",
      },
    ]);
  });

  it("returns nothing for a missing or malformed packet", () => {
    expect(captureSetupTips(null)).toEqual([]);
    expect(captureSetupTips({})).toEqual([]);
    expect(captureSetupTips({ context_packet: null })).toEqual([]);
    expect(captureSetupTips(draftWith(null))).toEqual([]);
    expect(captureSetupTips(draftWith("tips"))).toEqual([]);
    expect(captureSetupTips(draftWith({ recommendations: "tips" }))).toEqual([]);
    expect(captureSetupTips(draftWith({ recommendations: { 0: recommendation() } }))).toEqual(
      []
    );
  });

  it("drops a tip without a title or steps", () => {
    const base = recommendation();
    const tips = captureSetupTips(
      packet([
        null,
        "tip",
        { ...base, guide: null },
        { ...base, guide: { ...base.guide, title: "" } },
        { ...base, guide: { ...base.guide, title: 7 } },
        { ...base, guide: { ...base.guide, steps: [] } },
        { ...base, guide: { ...base.guide, steps: "Start a session." } },
        { ...base, guide: { ...base.guide, steps: [3, ""] } },
      ])
    );

    expect(tips).toEqual([]);
  });

  it("keeps only string steps and UUID note ids", () => {
    const [tip] = captureSetupTips(
      packet([
        recommendation({
          note_ids: [NOTE_A, "not-a-uuid", 42, `${NOTE_B}/../x`, NOTE_B],
          guide: { ...recommendation().guide, steps: ["One.", 2, "", "Two."] },
        }),
      ])
    );

    expect(tip.noteIds).toEqual([NOTE_A, NOTE_B]);
    expect(tip.steps).toEqual(["One.", "Two."]);
  });

  it("keeps at most six tips", () => {
    const many = Array.from({ length: 9 }, (_, index) =>
      recommendation({ recommendation_id: `tip-${index}` })
    );

    expect(captureSetupTips(packet(many)).map((tip) => tip.id)).toEqual([
      "tip-0",
      "tip-1",
      "tip-2",
      "tip-3",
      "tip-4",
      "tip-5",
    ]);
  });

  it("shows a repeated tip once", () => {
    const tips = captureSetupTips(
      packet([recommendation(), recommendation({ explanation: "Again." })])
    );

    expect(tips.map((tip) => tip.explanation)).toEqual(["Two memos named no session."]);
  });

  it("calls any explanation not marked as the drafter's a Lab Tracker check", () => {
    const sources = ["model", "server", "drafter", undefined].map(
      (explanation_source) =>
        captureSetupTips(packet([recommendation({ explanation_source })]))[0].explanationSource
    );

    expect(sources).toEqual(["model", "server", "server", "server"]);
  });

  it("drops an app path outside the app", () => {
    const pathFor = (app_path) =>
      captureSetupTips(
        packet([recommendation({ guide: { ...recommendation().guide, app_path } })])
      )[0].appPath;

    expect(pathFor("https://x")).toBe("");
    expect(pathFor("//x")).toBe("");
    expect(pathFor("/app/../admin")).toBe("");
    expect(pathFor("/appx")).toBe("");
    expect(pathFor(null)).toBe("");
    expect(pathFor("/app/devices")).toBe("/app/devices");
    expect(pathFor(`/app/sessions/${SESSION}`)).toBe(`/app/sessions/${SESSION}`);
  });

  it("shows the session label and command only when they are strings", () => {
    const [tip] = captureSetupTips(
      packet([
        recommendation({
          session_label: "operational session LT-ABC",
          guide: { ...recommendation().guide, command: "lt watch add <folder>" },
        }),
      ])
    );
    const [bare] = captureSetupTips(
      packet([
        recommendation({
          session_label: { code: "LT-ABC" },
          explanation: ["x"],
          guide: { ...recommendation().guide, command: 5, doc: null },
        }),
      ])
    );

    expect(tip.sessionLabel).toBe("operational session LT-ABC");
    expect(tip.command).toBe("lt watch add <folder>");
    expect(bare).toMatchObject({ sessionLabel: "", explanation: "", command: "", doc: "" });
  });

  // graph-drafts.test.jsx renders a six-capture tip and checks it links only
  // the first five, so the cap is pinned by behaviour there.
  it("cites each capture once, whatever the case of its id", () => {
    const lower = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const [tip] = captureSetupTips(
      packet([recommendation({ note_ids: [lower.toUpperCase(), lower, NOTE_A, NOTE_A] })])
    );
    // Repeated ids would also give the page's capture buttons duplicate React keys.
    expect(tip.noteIds).toEqual([lower, NOTE_A]);
  });

  it("drops blank steps, and a tip left with none", () => {
    const { guide } = recommendation();
    const withBlanks = recommendation({ guide: { ...guide, steps: ["  ", "Then dictate.", "\n"] } });
    const allBlank = recommendation({ guide: { ...guide, steps: [" ", "\t"] } });

    expect(captureSetupTips(packet([withBlanks]))[0].steps).toEqual(["Then dictate."]);
    expect(captureSetupTips(packet([allBlank]))).toEqual([]);
  });
});

describe("appPathLabel", () => {
  it("names the page each app path opens", () => {
    expect(appPathLabel("/app")).toBe("Open Home");
    expect(appPathLabel("/app/devices")).toBe("Open Devices");
    expect(appPathLabel(`/app/sessions/${SESSION}`)).toBe("Open the session");
  });

  it("names nothing outside the pages a tip can open", () => {
    expect(appPathLabel("https://x")).toBe("");
    expect(appPathLabel("//x")).toBe("");
    expect(appPathLabel("/app/sessions/not-a-uuid")).toBe("");
    expect(appPathLabel(`/app/sessions/${SESSION}/edit`)).toBe("");
    expect(appPathLabel("/app/users")).toBe("");
    expect(appPathLabel(undefined)).toBe("");
  });
});
