function value = logicalId(path)
%LOGICALID Return a cwd-relative path when possible, otherwise a filename.
file = java.io.File(char(string(path))).getCanonicalFile();
cwd = java.io.File(pwd).getCanonicalFile();
fullPath = char(file.getPath());
cwdPath = char(cwd.getPath());
prefix = [cwdPath, filesep];
if startsWith(fullPath, prefix)
    value = fullPath(numel(prefix) + 1:end);
else
    value = char(file.getName());
end
value = strrep(value, filesep, '/');
end
