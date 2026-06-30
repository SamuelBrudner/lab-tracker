function value = clientCaptureId(logicalId, contentHash)
%CLIENTCAPTUREID Build the idempotency key used by figure uploads.
if nargin < 2
    contentHash = '';
end
cleaned = char(string(logicalId));
cleaned = strrep(cleaned, '\', '/');
parts = split(string(cleaned), '/');
parts = strip(parts);
parts(parts == "") = [];
if isempty(parts)
    cleaned = 'figure';
else
    cleaned = char(join(parts, '/'));
end
value = ['figure:', cleaned];
if strlength(string(contentHash)) > 0
    hashText = char(string(contentHash));
    value = [value, ':', hashText(1:min(12, numel(hashText)))];
end
if numel(value) > 120
    suffix = labtracker.internal.sha256Text(value);
    value = [value(1:104), ':', suffix(1:12)];
end
end
