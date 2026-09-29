function value = clientCaptureId(logicalId, contentHash)
%CLIENTCAPTUREID Build the idempotency key used by figure uploads.
if nargin < 2
    contentHash = '';
end
cleaned = strrep(labtracker.internal.textOf(logicalId), '\', '/');
parts = strtrim(strsplit(cleaned, '/'));
parts = parts(~cellfun(@isempty, parts));
if isempty(parts)
    cleaned = 'figure';
else
    cleaned = strjoin(parts, '/');
end
value = ['figure:', cleaned];
hashText = labtracker.internal.textOf(contentHash);
if ~isempty(hashText)
    value = [value, ':', hashText(1:min(12, numel(hashText)))];
end
if numel(value) > 120
    suffix = labtracker.internal.sha256Text(value);
    value = [value(1:104), ':', suffix(1:12)];
end
end
