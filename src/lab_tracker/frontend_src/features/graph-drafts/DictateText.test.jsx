import * as React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DictateText } from "./DictateText.jsx";

afterEach(() => { vi.unstubAllGlobals(); });
describe("triage dictation", () => {
  it("edits a buffer only after deliberate recording and drops late results after unmount", () => {
    let recognition;
    class Recognition {
      constructor() { recognition = this; }
      start() {}
      abort() {}
    }
    vi.stubGlobal("SpeechRecognition", Recognition);
    const onText = vi.fn();
    const view = render(<DictateText onText={onText} />);
    expect(onText).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Dictate replacement text" }));
    const result = Object.assign([{ transcript: "The corrected interpretation" }], { isFinal: true });
    act(() => recognition.onresult({ results: [result] }));
    expect(onText).toHaveBeenCalledWith("The corrected interpretation");
    view.unmount();
    recognition.onresult({ results: [result] });
    expect(onText).toHaveBeenCalledTimes(1);
  });
  it("keeps the typed interface available when browser dictation is unsupported", () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    render(<DictateText onText={vi.fn()} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
