function safe = safeFilenamePart(value)
%SAFEFILENAMEPART A safePathPart of at most 48 characters (watch._safe_filename_part).
safe = labtracker.internal.safePathPart(value);
if numel(safe) <= 48
    return
end
digest = labtracker.internal.sha256Text(value);
safe = [safe(1:31), '-', digest(1:16)];
end
