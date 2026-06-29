function digest = sha256File(path)
%SHA256FILE Stream a file through Java SHA-256 and return lowercase hex.
fid = fopen(char(string(path)), 'rb');
if fid < 0
    error('labtracker:io', 'Could not open file for hashing: %s', char(string(path)));
end
cleanup = onCleanup(@() fclose(fid));
md = java.security.MessageDigest.getInstance('SHA-256');
while true
    chunk = fread(fid, [1, 1048576], '*uint8');
    if isempty(chunk)
        break
    end
    try
        md.update(chunk);
    catch
        md.update(typecast(chunk, 'int8'));
    end
end
bytes = typecast(md.digest(), 'uint8');
digest = lower(reshape(dec2hex(bytes, 2).', 1, []));
clear cleanup;
end
