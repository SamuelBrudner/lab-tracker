function uri = fileUri(path)
%FILEURI Return a file:/// URI for a local path, as Python's Path.as_uri() does.
file = javaObject('java.io.File', labtracker.internal.textOf(path));
uri = char(file.getCanonicalFile().toPath().toUri().toString());
% java.nio adds a trailing slash for an existing directory; Python does not.
if numel(uri) > numel('file:///') && uri(end) == '/'
    uri = uri(1:end - 1);
end
end
