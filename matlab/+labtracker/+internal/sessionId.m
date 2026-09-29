function id = sessionId(reference)
%SESSIONID The session UUID for a UUID or an LT- link code, or '' when it is neither.
%
%   Mirrors the Python client's strict_session_id: a link code is the 26
%   character base32 form of the UUID (optionally prefixed LT-), accepted only
%   when it re-encodes to itself, i.e. it is a code the server printed.
id = '';
text = strtrim(labtracker.internal.textOf(reference));
if isempty(text)
    return
end
hex = lower(regexprep(text, '^(urn:uuid:)|[{}-]', ''));
if numel(hex) == 32 && all(ismember(hex, '0123456789abcdef'))
    id = [hex(1:8), '-', hex(9:12), '-', hex(13:16), '-', hex(17:20), '-', hex(21:32)];
    return
end
code = upper(regexprep(text, '[\s-]+', ''));
if numel(code) == 28 && strncmp(code, 'LT', 2)
    code = code(3:end);
end
alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
[known, positions] = ismember(code, alphabet);
if numel(code) ~= 26 || ~all(known)
    return
end
bits = reshape(dec2bin(positions - 1, 5).', 1, []);
% 26 characters carry 130 bits: 128 for the UUID, then two pad bits that a
% printed code always leaves at zero.
if any(bits(129:130) ~= '0')
    return
end
bytes = bin2dec(reshape(bits(1:128), 8, []).');
hex = lower(reshape(dec2hex(bytes, 2).', 1, []));
id = [hex(1:8), '-', hex(9:12), '-', hex(13:16), '-', hex(17:20), '-', hex(21:32)];
end
