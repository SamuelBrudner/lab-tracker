function value = readJsonFile(path)
%READJSONFILE Decode a JSON object file, or return [] when it is missing or unreadable.
value = [];
try
    if exist(path, 'file') == 2
        decoded = jsondecode(fileread(path));
        if isstruct(decoded) && isscalar(decoded)
            value = decoded;
        end
    end
catch
    value = [];
end
end
