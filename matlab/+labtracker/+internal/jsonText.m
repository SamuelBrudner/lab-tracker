function text = jsonText(payload, field)
%JSONTEXT The trimmed text of payload.(field) when it is a string, else ''.
text = '';
if isstruct(payload) && isfield(payload, field)
    text = strtrim(labtracker.internal.textOf(payload.(field)));
end
end
