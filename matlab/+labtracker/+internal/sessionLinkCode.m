function code = sessionLinkCode(sessionId)
%SESSIONLINKCODE The 26-character base32 link code the server prints for a session UUID.
hex = lower(strrep(labtracker.internal.textOf(sessionId), '-', ''));
bytes = hex2dec(reshape(hex, 2, []).');
bits = [reshape(dec2bin(bytes, 8).', 1, []), '00'];
alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
code = alphabet(bin2dec(reshape(bits, 5, []).') + 1);
code = code(:).';
end
