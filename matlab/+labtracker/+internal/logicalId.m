function value = logicalId(path)
%LOGICALID Return a cwd-relative path when possible, otherwise a filename.
file = javaObject('java.io.File', labtracker.internal.textOf(path)).getCanonicalFile();
cwd = javaObject('java.io.File', pwd).getCanonicalFile();
fullPath = char(file.getPath());
cwdPath = char(cwd.getPath());
prefix = [cwdPath, filesep];
if strncmp(fullPath, prefix, numel(prefix))
    value = fullPath(numel(prefix) + 1:end);
else
    value = char(file.getName());
end
value = strrep(value, filesep, '/');
end
