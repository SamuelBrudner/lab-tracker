import * as React from "react";

// Dictation edits the buffer; keeping the proposal remains a separate act.
function DictateText({ disabled, onText, label = "Dictate replacement text" }) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const active = React.useRef(null);
  const callback = React.useRef(onText);
  const [listening, setListening] = React.useState(false);
  const [error, setError] = React.useState("");
  React.useLayoutEffect(() => { callback.current = onText; });
  React.useEffect(() => () => {
    const recognition = active.current;
    active.current = null;
    recognition?.abort();
  }, []);
  React.useEffect(() => {
    if (disabled && active.current) {
      const recognition = active.current;
      active.current = null;
      recognition.abort();
      setListening(false);
    }
  }, [disabled]);
  if (!Recognition) {
    return null;
  }
  function toggle() {
    if (active.current) {
      active.current.stop();
      return;
    }
    setError("");
    const recognition = new Recognition();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = document.documentElement.lang || navigator.language || "en-US";
    active.current = recognition;
    recognition.onresult = (event) => {
      if (active.current !== recognition) {
        return;
      }
      const text = Array.from(event.results)
        .filter((result) => result.isFinal)
        .map((result) => result[0].transcript).join(" ").trim();
      if (text) {
        callback.current(text);
      }
    };
    recognition.onerror = () => {
      if (active.current === recognition) {
        setError("Dictation was unavailable. You can type your correction.");
      }
    };
    recognition.onend = () => {
      if (active.current === recognition) {
        active.current = null;
        setListening(false);
      }
    };
    try {
      recognition.start();
      setListening(true);
    } catch {
      active.current = null;
      setListening(false);
      setError("Dictation was unavailable. You can type your correction.");
    }
  }
  return (
    <div className="triage-dictation">
      <button type="button" className="btn-link" disabled={disabled} aria-pressed={listening} onClick={toggle}>
        {listening ? "Stop dictation" : label}
      </button>
      <span className="subtle">Uses your browser’s speech service.</span>
      {error ? <p role="status">{error}</p> : null}
    </div>
  );
}

export { DictateText };
