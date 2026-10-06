function digest = sha256Text(text)
%SHA256TEXT Hash UTF-8 text with Java SHA-256 and return lowercase hex.
md = javaMethod('getInstance', 'java.security.MessageDigest', 'SHA-256');
bytes = unicode2native(labtracker.internal.textOf(text), 'UTF-8');
if ~isempty(bytes)
    try
        md.update(bytes);
    catch
        md.update(typecast(bytes, 'int8'));
    end
end
raw = typecast(md.digest(), 'uint8');
digest = lower(reshape(dec2hex(raw, 2).', 1, []));
end
