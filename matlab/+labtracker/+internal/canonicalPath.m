function value = canonicalPath(path)
%CANONICALPATH Absolute path with symlinks and . / .. resolved (Python's Path.resolve()).
file = javaObject('java.io.File', labtracker.internal.textOf(path));
value = char(file.getCanonicalPath());
end
