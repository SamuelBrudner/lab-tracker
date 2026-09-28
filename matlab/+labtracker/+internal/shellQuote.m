function quoted = shellQuote(value)
%SHELLQUOTE Quote one argument for the shell system() runs (sh, or cmd on Windows).
text = labtracker.internal.textOf(value);
if ispc
    quoted = ['"', strrep(text, '"', '""'), '"'];
else
    quoted = ['''', strrep(text, '''', '''\'''''), ''''];
end
end
