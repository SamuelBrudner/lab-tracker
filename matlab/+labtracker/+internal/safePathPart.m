function safe = safePathPart(value)
%SAFEPATHPART Letters, digits and -_. kept, anything else '-' (watch._safe_path_part).
safe = labtracker.internal.textOf(value);
keep = isstrprop(safe, 'alphanum') | ismember(safe, '-_.');
safe(~keep) = '-';
safe = regexprep(safe, '^[.-]+|[.-]+$', '');
if isempty(safe)
    safe = 'event';
end
end
