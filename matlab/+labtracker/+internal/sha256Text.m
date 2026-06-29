function digest = sha256Text(text)
%SHA256TEXT Hash UTF-8 text with Java SHA-256 and return lowercase hex.
md = java.security.MessageDigest.getInstance('SHA-256');
bytes = unicode2native(char(string(text)), 'UTF-8');
try
    md.update(bytes);
catch
    md.update(typecast(bytes, 'int8'));
end
raw = typecast(md.digest(), 'uint8');
digest = lower(reshape(dec2hex(raw, 2).', 1, []));
end
